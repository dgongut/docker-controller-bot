"""
/schedule: what a task is, its screens and questions, and the scheduler.

The questions are driven the way a user drives them: presses go through the
real dispatcher and typed answers through the real text handler, against the
real manager. Every message the bot sends on the way is checked to be one
Telegram would take, and every button to parse and fit.
"""

import glob
import itertools
import json
import os
import shutil
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import telebot

import harness
import i18n
import callback_registry

dcb, store, _root = harness.load_bot(env={"LANGUAGE": "ES", "BUTTON_COLUMNS": "3"})
import callbacks
import host_registry
import schedules
import test_bot as tb
from config import MUTE_MAX_MINUTES, SHOW_OUTPUT_FAILURES
from schedule_manager import TYPED_LIMITS, ScheduleError, ScheduleManager, check, clean, is_valid_cron

NAME_MAX, COMMAND_MAX = TYPED_LIMITS["name"], TYPED_LIMITS["command"]

ADMIN = int(str(dcb.TELEGRAM_ADMIN).split(",")[0])
manager = dcb.schedule_manager


def text(key, *args):
	return i18n.get_text(key, *args)


def _clear():
	for task in manager.all():
		manager.delete(task["id"])
	schedules.forget_state(ADMIN)


def _task(**fields):
	task = {"name": "copia", "cron": "@daily", "action": "restart", "container": "nginx", "host": "h_local"}
	task.update(fields)
	enabled = task.pop("enabled", True)
	added = manager.add(task)
	return added if enabled else manager.update(added["id"], enabled=False)


class Chat:
	"""
	Telegram, for one admin in a private chat.

	Records what the bot sends, deletes and edits, and answers the way a user
	does: pressing a button of a message on screen, or typing.
	"""

	PATCHED = ("send_message", "delete_message", "edit_message_reply_markup", "send_document",
				"send_message_to_notification_channel", "answer_callback_quietly")

	def __init__(self):
		self.messages = {}
		self.order = []
		self.deleted = []
		self.notices = []
		self.documents = []
		self._ids = itertools.count(1000)

	def __enter__(self):
		self._original = {name: getattr(dcb, name) for name in self.PATCHED}
		dcb.send_message = self._send
		dcb.delete_message = lambda message_id, chat_id=None: self.deleted.append(message_id)
		dcb.edit_message_reply_markup = self._edit
		dcb.send_document = lambda **kw: self.documents.append(kw) or MagicMock()
		dcb.send_message_to_notification_channel = lambda **kw: self.notices.append(kw.get("message"))
		dcb.answer_callback_quietly = lambda *a, **k: None
		return self

	def __exit__(self, *exc):
		for name, value in self._original.items():
			setattr(dcb, name, value)
		schedules.forget_state(ADMIN)

	def _check(self, message, markup):
		tb._assert_telegram_html(message, message[:60])
		for data in harness.keyboard_callbacks(markup) if markup else ():
			assert len(data.encode()) <= 64, data
			callback_registry.parse(data)
		for row in markup.keyboard if markup else ():
			assert len(row) <= 8, row

	def _send(self, chat_id=None, message=None, reply_markup=None, **kw):
		self._check(message, reply_markup)
		message_id = next(self._ids)
		self.messages[message_id] = [message, reply_markup]
		self.order.append(message_id)
		sent = MagicMock(message_id=message_id)
		sent.chat.id = ADMIN
		return sent

	def _edit(self, chat_id, message_id, reply_markup):
		self._check(self.messages[message_id][0], reply_markup)
		self.messages[message_id][1] = reply_markup

	@property
	def last(self):
		return self.messages[self.order[-1]][0]

	def texts(self):
		return [self.messages[message_id][0] for message_id in self.order]

	def buttons(self, message_id=None):
		"""(text, data) of every button on a message, by default the newest that has any."""
		if message_id is None:
			message_id = next(i for i in reversed(self.order) if self.messages[i][1])
		markup = self.messages[message_id][1]
		return [(button.text, button.callback_data) for row in markup.keyboard for button in row] if markup else []

	def data(self, message_id=None):
		return [data for _text, data in self.buttons(message_id)]

	def labelled(self, label):
		"""The data of the newest button whose text starts with `label`."""
		return next(data for _text, data in self.buttons() if _text.startswith(label))

	def holding(self, data):
		"""The newest message with a button carrying `data`."""
		return next(i for i in reversed(self.order) if data in self.data(i))

	def press(self, data, message_id=None, user=ADMIN):
		"""Presses `data` on `message_id`, by default the newest message that has it."""
		# A CallbackQuery as far as isinstance goes: the dispatcher answers in
		# the chat of its message, and that is where questions get bound to.
		call = MagicMock(spec=telebot.types.CallbackQuery)
		call.data, call.id, call.message, call.from_user = data, "q", MagicMock(), MagicMock()
		call.message.id = message_id if message_id is not None else self.holding(data)
		call.message.chat.id, call.message.chat.type = ADMIN, "private"
		call.message.date, call.message.edit_date = int(time.time()), None
		call.from_user.id, call.from_user.username = user, "test"
		dcb.button_controller(call)

	def type(self, typed):
		"""Writes `typed` in the chat, and returns the id of that message."""
		message = MagicMock()
		message.text, message.message_id = typed, next(self._ids)
		message.from_user.id, message.from_user.username = ADMIN, "test"
		message.chat.id, message.chat.type, message.chat.is_forum = ADMIN, "private", False
		message.reply_to_message = None
		dcb.handle_text(message)
		return message.message_id


class Fleet:
	"""
	Hosts, and the containers each one has, for the length of a test.

	`unreachable` hosts fail anything asked of them, and `paused` ones are
	paused.
	"""

	def __init__(self, hosts=None, containers=None, unreachable=(), paused=()):
		self.hosts = hosts if hosts is not None else tb.HOST_FIXTURE
		self.containers = containers if containers is not None else {"h_local": ["nginx"], "h_nas": ["plex"]}
		self.unreachable = unreachable
		self.paused = paused

	def _named(self, owner, name):
		if owner.host_id in self.unreachable:
			raise Exception("no route to host")
		return tb._container(name, "running") if name in self.containers.get(owner.host_id, ()) else None

	def __enter__(self):
		tb._with_hosts(self.hosts, unreachable=[h["alias"] for h in self.hosts if h["id"] in self.unreachable])
		for host_id in self.paused:
			assert host_registry.set_paused(host_id, True)
		self._original = (dcb.DockerManager.list_containers, dcb.DockerManager.container_named)
		dcb.DockerManager.list_containers = lambda owner, comando="": [
			tb._container(name, "running") for name in self.containers.get(owner.host_id, ())]
		dcb.DockerManager.container_named = lambda owner, name: self._named(owner, name)
		return self

	def __exit__(self, *exc):
		dcb.DockerManager.list_containers, dcb.DockerManager.container_named = self._original
		tb._restore_hosts()


# ============================================================================
# WHAT A TASK IS
# ============================================================================

def test_a_task_holds_only_what_its_action_uses():
	"""So it never carries the leftovers of an action it was being made as."""
	mute = check({"name": "silencio", "cron": "@daily", "action": "mute", "minutes": "30",
					"host": "h_nas", "container": "plex", "command": "ls"})
	assert (mute["minutes"], mute["host"], mute["container"], mute["command"]) == (30, None, None, None), mute
	prune = check({"name": "limpia", "cron": "@daily", "action": "PRUNE", "prune_type": "images",
					"host": "h_nas", "container": "plex"})
	assert (prune["action"], prune["container"], prune["show_output"]) == ("prune", None, False), prune
	# A key this version does not know is someone else's, and stays.
	assert check(dict(prune, futuro=1))["futuro"] == 1


def test_every_action_has_somewhere_to_run():
	"""
	The scheduler runs an action that names a container through the core's
	function of the same name. A new one added to the table without it would
	fail on every run.
	"""
	import inspect
	for action, fields in dcb.SCHEDULE_ACTION_FIELDS.items():
		if action in ("mute", "exec", "prune"):
			continue
		assert "container" in fields, action
		assert "from_schedule" in inspect.signature(getattr(dcb, action)).parameters, action
		assert text(f"schedule_action_{action}") != f"schedule_action_{action}", action


def test_every_field_refuses_what_it_cannot_hold():
	refused = {
		"name": ["", "   ", None, 5],
		"cron": ["", "nunca", "0 0 30 2 *", "* * * * * *", None],
		"action": ["", "dance", None, ["run"]],
		"minutes": ["", "diez", 0, -5, True, None],
		"command": ["", "  ", None],
		"prune_type": ["planets", "", None],
		"show_output": ["maybe", "1", 2],
		"container": ["", None],
		"host": ["", None],
	}
	# Only what is typed into the bot: a task already in the file ran before
	# these limits existed.
	typed_only = {"name": "x" * (NAME_MAX + 1), "command": "x" * (COMMAND_MAX + 1), "minutes": MUTE_MAX_MINUTES + 1}
	keys = {"name": "schedule_invalid_name", "cron": "schedule_invalid_cron",
			"minutes": "schedule_invalid_minutes", "command": "schedule_invalid_command"}
	cases = [(field, value, typed) for field, values in refused.items() for value in values for typed in (False, True)]
	cases += [(field, value, True) for field, value in typed_only.items()]
	for field, value, typed in cases:
		try:
			clean(field, value, typed=typed)
		except ScheduleError as e:
			assert (e.field, e.key) == (field, keys.get(field, "error_invalid_selection")), (field, value)
		else:
			raise AssertionError(f"{field} aceptó {value!r}")
	assert clean("name", typed_only["name"]) == typed_only["name"]
	assert clean("minutes", MUTE_MAX_MINUTES * 2) == MUTE_MAX_MINUTES, "recortado, como hace /mute"
	assert clean("name", "  copia  ", typed=True) == "copia"
	assert clean("cron", " 0  3 * *   * ") == "0 3 * * *"
	assert clean("minutes", MUTE_MAX_MINUTES, typed=True) == MUTE_MAX_MINUTES
	assert clean("show_output", None) is False, "las tareas de antes sin el campo decían que no"
	assert clean("show_output", "failures") == SHOW_OUTPUT_FAILURES


def test_a_cron_that_never_fires_is_not_accepted():
	"""`0 0 30 2 *` parses, and fires on the 30th of February. It was saved and never ran."""
	for never in ("0 0 30 2 *", "0 0 31 4 *", "0 0 31 6 *"):
		assert not is_valid_cron(never), never
	for fine in ("0 0 29 2 *", "0 0 L * *", "*/5 * * * *", "@daily", "@reboot", "0 0 * * 1#2"):
		assert is_valid_cron(fine), fine
	for broken in ("", "60 * * * *", "* * * *", "* * * * * *", "*/0 * * * *", "<b>", "@nunca"):
		assert not is_valid_cron(broken), broken


def test_the_manager_hands_out_copies():
	"""The scheduler reads while a press writes: a caller holding the manager's own dict changed it unwritten."""
	_clear()
	task = _task()
	mine = manager.all()
	mine.append({"name": "inyectada"})
	mine[0]["name"] = "cambiada"
	manager.get(task["id"])["cron"] = "nunca"
	assert manager.all() == [task], manager.all()
	_clear()


def test_concurrent_adds_lose_none():
	_clear()
	start = threading.Barrier(8)

	def add(index):
		start.wait()
		_task(name=f"t{index}")

	workers = [threading.Thread(target=add, args=(i,), daemon=True) for i in range(8)]
	for worker in workers:
		worker.start()
	for worker in workers:
		worker.join(10)
	stored = manager.all()
	assert len(stored) == 8 and len({task["id"] for task in stored}) == 8, stored
	_clear()


def _unwritable(path):
	"""
	Makes writing `path` fail for real, for root too: a directory where the
	writer puts its temporary copy. Returns the undo.
	"""
	os.mkdir(f"{path}.tmp")
	return lambda: os.rmdir(f"{path}.tmp")


def test_a_write_that_fails_changes_nothing():
	"""
	The writer logged the failure and returned as if it had saved: a full disk
	or a read-only volume reported a task as added, and it was gone at the
	next start.
	"""
	root = harness.tempdir()
	tasks = ScheduleManager(root, "schedules.json")
	task = tasks.add({"name": "copia", "cron": "@daily", "action": "mute", "minutes": 5})
	undo = _unwritable(tasks.full_path)
	try:
		for change in (lambda: tasks.add({"name": "otra", "cron": "@daily", "action": "mute", "minutes": 5}),
						lambda: tasks.update(task["id"], cron="@hourly"), lambda: tasks.delete(task["id"])):
			try:
				change()
			except OSError:
				pass
			else:
				raise AssertionError("no ha fallado")
			assert tasks.all() == [task], tasks.all()
	finally:
		undo()
	assert [t["name"] for t in ScheduleManager(root, "schedules.json").all()] == ["copia"]
	shutil.rmtree(root, ignore_errors=True)


def test_settings_that_cannot_be_written_still_apply():
	"""The writer raises now; a setting changed from /settings must not stop the bot."""
	import store as real_store
	undo = _unwritable(os.path.join(real_store.root(), real_store.SETTINGS_FILE))
	try:
		real_store.set("bot.button_columns", 4)
		assert real_store.get("bot.button_columns") == 4
	finally:
		undo()
		real_store.set("bot.button_columns", 3)


def test_a_hand_edited_file_loses_nothing():
	"""
	A task that cannot run is left out and said at start-up, and written back
	as it was: a typo must not cost the task. Ids missing or repeated get new
	ones, so a button never names two tasks.
	"""
	root = harness.tempdir()
	path = os.path.join(root, "schedules.json")
	entries = [
		{"id": 3, "name": "buena", "cron": "@daily", "action": "Restart", "container": "nginx", "host": "h_local"},
		{"id": 3, "name": "repetida", "cron": "@daily", "action": "mute", "minutes": 5},
		{"name": "sin id", "cron": "@hourly", "action": "exec", "container": "db", "command": "ls", "host": "h_local"},
		{"id": 9, "name": "rota", "cron": "nunca", "action": "mute", "minutes": 5},
		"no es una tarea",
		{"id": 4, "name": "n" * 80, "cron": "@daily", "action": "mute", "minutes": 10 ** 6},
		{"id": 5, "name": "larga", "cron": "@daily", "action": "exec", "container": "db",
			"command": "x" * 2000, "host": "h_local"},
	]
	with open(path, "w", encoding="utf-8") as handle:
		json.dump({"version": 7, "schedules": entries}, handle)

	loaded = ScheduleManager(root, "schedules.json")
	tasks = loaded.all()
	assert [task["name"] for task in tasks] == ["buena", "repetida", "sin id", "n" * 80, "larga"], tasks
	assert len({task["id"] for task in tasks}) == 5 and all(task["id"] > 9 for task in tasks[1:3]), tasks
	assert tasks[3]["minutes"] == MUTE_MAX_MINUTES, "lo que ya estaba se recorta, no se deja fuera"
	assert tasks[0]["action"] == "restart" and tasks[2]["show_output"] is False, tasks
	assert loaded.ignored == ["rota", "no es una tarea"], loaded.ignored

	loaded.add({"name": "nueva", "cron": "@daily", "action": "mute", "minutes": 1})
	with open(path, encoding="utf-8") as handle:
		written = json.load(handle)
	assert written["version"] == 7, written
	assert entries[3] in written["schedules"] and "no es una tarea" in written["schedules"], written
	shutil.rmtree(root, ignore_errors=True)


def test_names_are_unique_but_a_task_keeps_its_own():
	_clear()
	first = _task(name="una")
	_task(name="otra")
	for attempt in (lambda: _task(name="una"), lambda: manager.update(first["id"], name="otra")):
		try:
			attempt()
		except ScheduleError as e:
			assert (e.key, e.field) == ("schedule_name_exists", "name")
		else:
			raise AssertionError("ha aceptado un nombre repetido")
	assert manager.update(first["id"], name="una", cron="@hourly")["cron"] == "@hourly"
	_clear()

	# Two of the same name, written by hand, can still be changed: switching
	# one off refused it, and so did removing its host or its next firing.
	root = harness.tempdir()
	with open(os.path.join(root, "schedules.json"), "w", encoding="utf-8") as handle:
		json.dump({"schedules": [{"id": 1, "name": "doble", "cron": "@daily", "action": "mute", "minutes": 5},
									{"id": 2, "name": "doble", "cron": "@hourly", "action": "mute", "minutes": 5}]}, handle)
	twins = ScheduleManager(root, "schedules.json")
	assert twins.update(2, enabled=False)["enabled"] is False
	shutil.rmtree(root, ignore_errors=True)


def test_an_id_is_never_given_twice():
	"""
	Worked out from the tasks at each start, the newest one's id went to the
	next task made after deleting it, and its details still on screen acted
	on that one.
	"""
	root = harness.tempdir()
	first = ScheduleManager(root, "schedules.json")
	kept = first.add({"name": "una", "cron": "@daily", "action": "mute", "minutes": 5})
	gone = first.add({"name": "dos", "cron": "@daily", "action": "mute", "minutes": 5})
	first.delete(gone["id"])
	again = ScheduleManager(root, "schedules.json")
	made = again.add({"name": "tres", "cron": "@daily", "action": "mute", "minutes": 5})
	assert made["id"] not in (kept["id"], gone["id"]), made
	shutil.rmtree(root, ignore_errors=True)


def test_removing_a_host_warns_about_its_tasks_and_switches_them_off():
	"""
	Said before the tap, and switched off rather than deleted after it: moving
	one and switching it back on is a couple of taps, while a cron and a
	command typed long ago are gone for good.
	"""
	_clear()
	nas = _task(name="Limpieza nas", action="prune", prune_type="images", host="h_nas")
	_task(name="Reinicio local")
	_task(name="Silencio", action="mute", minutes=60)
	_task(name="Apagada", container="plex", host="h_nas", enabled=False)
	with Fleet(), Chat() as chat:
		assert [task["name"] for task in manager.on_host("h_nas")] == ["Limpieza nas"]
		question, _markup = dcb.build_settings_host_remove("h_nas")
		assert "Limpieza nas" in question and "Reinicio local" not in question and "Silencio" not in question
		original = dcb.render_settings
		dcb.render_settings = lambda *a, **k: None
		try:
			callbacks.cb_settingsHostRemoveConfirm(tb._update_ctx("settingsHostRemoveConfirm", ADMIN, 2, value="h_nas"))
		finally:
			dcb.render_settings = original
		assert any("Limpieza nas" in sent for sent in chat.texts()), chat.texts()
	assert manager.get(nas["id"])["enabled"] is False
	assert len(manager.all()) == 4
	_clear()


def test_the_open_question_lives_in_the_volume():
	"""In a relative ./cache/ it was inside the image: nothing swept it."""
	schedules.save_state(4242, {"asking": "name", "task": {}})
	try:
		loaded = schedules.load_state(4242)
		assert loaded["asking"] == "name" and {"asked_in", "asked_at"} <= set(loaded), loaded
		assert glob.glob(os.path.join(store.root(), "**", "*schedule_state_4242*"), recursive=True)
	finally:
		schedules.forget_state(4242)
	assert schedules.load_state(4242) is None


# ============================================================================
# HOW A TASK READS
# ============================================================================

def _labels_in_order(screen, keys):
	positions = [screen.index(text(f"schedule_label_{key}")) for key in keys]
	return positions == sorted(positions)


def test_every_screen_draws_a_task_the_same_way_round():
	"""The summary, the list and the details each drew a task their own way, and drifted apart."""
	_clear()
	with Fleet(), Chat() as chat:
		prune = _task(name="Limpieza", action="prune", prune_type="images", host="h_nas", show_output=True)
		execute = _task(name="Copia", action="exec", container="plex", host="h_nas", command="ls", show_output=True)
		schedules.send_menu()
		menu = chat.last
		for task, keys in ((prune, ("prune_type", "host", "show_output")),
							(execute, ("container", "host", "command", "show_output"))):
			schedules.send_task(task)
			for where, screen in (("resumen", "\n".join(schedules.task_lines(task))), ("detalle", chat.last),
									("lista", menu.split(f"{task['name']}</b>")[1])):
				assert _labels_in_order(screen, keys), (where, screen)
		assert text("schedule_prune_images") in menu, "el tipo de limpieza, en el idioma del usuario"
	_clear()


def test_the_host_is_only_shown_where_it_says_something():
	"""
	With a single host it says nothing, unless the one a task points at is
	gone: exactly when hiding it made the task look runnable.
	"""
	_clear()
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		local = _task(name="Local")
		gone = _task(name="Huérfana", action="prune", prune_type="images", host="h_nas", enabled=False)
		mute = _task(name="Silencio", action="mute", minutes=5)
		schedules.send_menu()
		schedules.send_task(local)
		schedules.send_task(gone)
		schedules.send_task(mute)
		menu, local_screen, gone_screen, mute_screen = chat.texts()
		assert text("schedule_label_host") not in local_screen and text("schedule_label_host") not in mute_screen
		assert text("schedule_host_missing") in gone_screen and text("schedule_host_missing") in menu
		assert "h_nas" not in menu + gone_screen
	with Fleet(), Chat() as chat:
		schedules.send_task(local)
		schedules.send_task(mute)
		assert "<b>🖥️ Host:</b> casa" in chat.texts()[0], chat.texts()[0]
		assert text("schedule_label_host") not in chat.last, "un silencio no es de ninguna máquina"
	_clear()


def test_what_was_typed_never_breaks_a_screen():
	"""A `mysql < dump.sql`, or a task called "copia <diaria>", made Telegram refuse /schedule for good."""
	_clear()
	with Fleet(containers={"h_local": ["db<x>"]}), Chat() as chat:
		task = _task(name="copia <diaria> & co", action="exec", container="db<x>",
						command="mysql < dump.sql > out", show_output=SHOW_OUTPUT_FAILURES)
		schedules.send_menu()
		schedules.send_pick_list("edit")
		schedules.send_pick_list("delete")
		schedules.send_task(task)
		for field in ("name", "command", "container", "show_output"):
			chat.press(f"schedField|{task['id']}|{field}")
			chat.press("schedCancel")
		chat.press(f"schedDelete|{task['id']}")
		joined = "\n".join(chat.texts())
		assert "copia &lt;diaria&gt; &amp; co" in joined and "mysql &lt; dump.sql" in joined, joined
		assert text("schedule_show_output_failures") in joined
	_clear()


# ============================================================================
# CREATING A TASK
# ============================================================================

def _start(chat, name="copia diaria", cron="0 3 * * *", action=None):
	chat.press("schedAdd", message_id=0)
	chat.type(name)
	chat.type(cron)
	if action:
		chat.press(f"schedAnswer|{action}")


def test_creating_a_task_asks_what_its_action_needs_in_order_and_saves_it():
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat, action="exec")
		assert text("schedule_ask_container") in chat.last
		chat.press(chat.labelled("plex"))
		assert text("schedule_ask_command") in chat.last
		chat.type("pg_dumpall > /backup/all.sql")
		assert text("schedule_ask_show_output") in chat.last
		assert text("schedule_show_output_failures_exec") in chat.last
		chat.press("schedAnswer|failures")
		confirmation = chat.last
		assert confirmation.startswith(text("schedule_confirm_title"))
		assert _labels_in_order(confirmation, ("name", "cron", "action", "container", "host", "command", "show_output"))
		chat.press("schedConfirm")
		assert any(text("schedule_added_success", "copia diaria") == sent for sent in chat.texts())
		assert chat.last.startswith(text("schedule_menu_title"))
	[task] = manager.all()
	assert {key: task[key] for key in ("name", "cron", "action", "container", "host", "command", "show_output", "enabled")} == {
		"name": "copia diaria", "cron": "0 3 * * *", "action": "exec", "container": "plex", "host": "h_nas",
		"command": "pg_dumpall > /backup/all.sql", "show_output": SHOW_OUTPUT_FAILURES, "enabled": True}, task
	_clear()


def test_each_question_shows_what_was_answered_above_it():
	"""So the summary grows downwards as the answers come in, and the last line is the last answer."""
	_clear()
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		_start(chat, name="silencio", action="mute")
		asking = chat.last
		answers = ["silencio", "0 3 * * *", "mute", text("schedule_ask_minutes").split("</b>")[0]]
		positions = [asking.index(answer) for answer in answers]
		assert positions == sorted(positions), asking
	_clear()


def test_the_action_question_offers_exactly_the_actions_it_explains():
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat)
		offered = [data.split("|", 1)[1] for data in chat.data() if data.startswith("schedAnswer|")]
		assert offered == list(dcb.SCHEDULE_ACTIONS), offered
		for action in dcb.SCHEDULE_ACTIONS:
			assert text(f"schedule_action_{action}") in chat.last, action
	_clear()


def test_a_prune_asks_its_host_only_where_there_is_a_choice():
	"""It names no container, so nothing else says where it runs. A question with one answer is not asked."""
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat, action="prune")
		assert {f"schedAnswer|{kind}" for kind in dcb.SCHEDULE_PRUNE_TYPES} <= set(chat.data())
		chat.press("schedAnswer|images")
		assert text("pick_a_host") in chat.last
		assert {"schedAnswer|h_local", "schedAnswer|h_nas"} <= set(chat.data()), chat.data()
		chat.press("schedAnswer|h_nas")
		assert text("schedule_show_output_failures_prune") in chat.last
		chat.press("schedAnswer|no")
		chat.press("schedConfirm")
	assert manager.all()[0]["host"] == "h_nas"
	_clear()
	for fleet in (Fleet(hosts=[tb.HOST_FIXTURE[0]]), Fleet(paused=("h_nas",))):
		with fleet, Chat() as chat:
			_start(chat, action="prune")
			chat.press("schedAnswer|images")
			assert text("schedule_ask_show_output") in chat.last, chat.last
			chat.press("schedAnswer|yes")
			chat.press("schedConfirm")
		assert manager.all()[0]["host"] == "h_local"
		_clear()


def test_a_refused_answer_asks_the_same_question_again_saying_why():
	_clear()
	_task(name="ocupado")
	with Fleet(), Chat() as chat:
		chat.press("schedAdd", message_id=0)
		for typed, key in (("ocupado", "schedule_name_exists"), ("x" * (NAME_MAX + 1), "schedule_invalid_name")):
			prompt = chat.order[-1]
			chat.type(typed)
			assert chat.last.startswith(f"<b>{text(key)}</b>"), chat.last
			assert text("schedule_ask_name").split("</b>")[0] in chat.last
			assert prompt in chat.deleted, "la pregunta de antes se queda en pantalla"
		chat.type("nueva")
		chat.type("0 0 30 2 *")
		assert chat.last.startswith(f"<b>{text('schedule_invalid_cron')}</b>") and "nueva" in chat.last
		chat.type("@daily")
		chat.press("schedAnswer|mute")
		for typed in ("diez", "0", "-5", str(MUTE_MAX_MINUTES + 1)):
			chat.type(typed)
			assert chat.last.startswith(f"<b>{text('schedule_invalid_minutes')}</b>"), (typed, chat.last)
		chat.type("30")
		chat.press("schedConfirm")
	assert [(task["name"], task["minutes"]) for task in manager.all()] == [("ocupado", None), ("nueva", 30)]
	_clear()


def test_only_the_question_on_screen_can_be_answered():
	"""A button of an earlier question, still on screen, answered the one open now."""
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat)
		action_question = chat.holding("schedAnswer|exec")
		chat.press("schedAnswer|mute", message_id=action_question)
		chat.press("schedAnswer|exec", message_id=action_question)
		assert chat.last == text("error_invalid_selection")
		chat.type("15")
		assert schedules.load_state(ADMIN)["task"]["action"] == "mute"
		chat.press("schedConfirm", message_id=action_question)
		assert manager.all() == [], "confirmado desde un mensaje que no era el suyo"
		chat.press("schedCancel")
		chat.press("schedAnswer|exec", message_id=12345)
		assert chat.last == text("error_invalid_selection"), "sin pregunta abierta"
	_clear()


def test_text_while_a_button_is_awaited_is_left_alone():
	"""Deleted before: everything the admin wrote, until they pressed a button."""
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat)
		before = len(chat.order)
		typed = chat.type("hola")
		assert typed not in chat.deleted and len(chat.order) == before
		assert schedules.load_state(ADMIN)["asking"] == "action"
	_clear()


def test_a_typed_answer_is_taken_away_with_its_question():
	_clear()
	with Fleet(), Chat() as chat:
		chat.press("schedAdd", message_id=0)
		question = chat.order[-1]
		typed = chat.type("copia")
		assert {typed, question} <= set(chat.deleted), chat.deleted
	_clear()


def test_cancelling_forgets_the_question():
	"""Its next loose message, hours later, became the command of an exec task."""
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat, action="exec")
		chat.press(chat.labelled("plex"))
		chat.press("schedCancel")
		assert schedules.load_state(ADMIN) is None
		before = len(chat.order)
		chat.type("rm -rf /datos")
		assert len(chat.order) == before and manager.all() == []
	_clear()


def test_the_containers_offered_are_every_hosts_but_never_the_bot():
	with Fleet(containers={"h_local": [dcb.CONTAINER_NAME, "nginx"], "h_nas": ["plex"]}), Chat() as chat:
		assert sorted(schedules.containers()) == [["h_local", "nginx"], ["h_nas", "plex"]]
		_start(chat, action="restart")
		labels = [label for label, data in chat.buttons() if data.startswith("schedAnswer|")]
		assert sorted(labels) == ["nginx · casa", "plex · nas"], labels
	with Fleet(hosts=[tb.HOST_FIXTURE[0]], containers={"h_local": []}), Chat() as chat:
		_start(chat, action="restart")
		assert chat.last == text("error_no_containers_available")
		assert schedules.load_state(ADMIN) is None
	_clear()


def test_a_long_container_list_goes_a_page_at_a_time():
	"""A button for every container went past the hundred Telegram takes, and the question never arrived."""
	_clear()
	names = [f"c{number:03}" for number in range(100)]
	with Fleet(hosts=[tb.HOST_FIXTURE[0]], containers={"h_local": names}), Chat() as chat:
		_start(chat, action="restart")
		question = chat.order[-1]
		assert len(chat.data()) <= 85 and "schedPage|1" in chat.data(), chat.data()
		chat.press("schedPage|1")
		assert chat.order[-1] == question, "el paso de página repinta, no manda otro"
		chat.press(chat.labelled("c085"))
		chat.press("schedConfirm")
	assert manager.all()[0]["container"] == "c085"
	_clear()


def test_a_name_taken_meanwhile_is_asked_again_at_the_end():
	_clear()
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		_start(chat, name="nueva", action="mute")
		chat.type("5")
		_task(name="nueva")
		chat.press("schedConfirm")
		assert chat.last.startswith(f"<b>{text('schedule_name_exists')}</b>"), chat.last
		chat.type("otra")
		assert chat.last.startswith(text("schedule_confirm_title")), "el resto ya estaba contestado"
		chat.press("schedConfirm")
	assert sorted(task["name"] for task in manager.all()) == ["nueva", "otra"]
	_clear()


def test_a_double_tap_saves_the_task_once_and_asks_nothing_more():
	"""
	Presses are handled on several threads. Two taps on Confirm ran together:
	the first saved the task, and the second, finding its name taken, asked
	for another one.
	"""
	_clear()
	original_add = manager.add

	def slow_add(fields):
		time.sleep(0.2)
		return original_add(fields)

	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		_start(chat, action="mute")
		chat.type("5")
		confirm = chat.holding("schedConfirm")
		manager.add = slow_add
		try:
			taps = [threading.Thread(target=chat.press, args=("schedConfirm", confirm)) for _ in range(2)]
			for tap in taps:
				tap.start()
			for tap in taps:
				tap.join(10)
		finally:
			del manager.add
		assert len(manager.all()) == 1, manager.all()
		assert not any(text("schedule_name_exists") in sent for sent in chat.texts()), chat.texts()
	_clear()


def test_another_admins_press_leaves_your_question_alone():
	"""
	The dispatcher took the message away before anyone looked at who pressed
	it: another admin's tap on your question deleted it and left it open, and
	your next loose message in the group was taken as its answer.
	"""
	_clear()
	other = ADMIN + 1
	original = (dcb.is_admin, dcb.is_allowed_origin)
	dcb.is_admin = lambda user: user in (ADMIN, other)
	dcb.is_allowed_origin = lambda chat, user: True
	try:
		with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
			_start(chat)
			question = chat.holding("schedAnswer|mute")
			for data in ("schedAnswer|mute", "schedCancel"):
				chat.press(data, message_id=question, user=other)
				assert chat.last == text("error_invalid_selection")
			assert question not in chat.deleted
			assert schedules.load_state(ADMIN)["asking"] == "action"
			chat.press("schedAnswer|mute", message_id=question)
			assert question in chat.deleted and schedules.load_state(ADMIN)["asking"] == "minutes"
	finally:
		dcb.is_admin, dcb.is_allowed_origin = original
	_clear()


def test_a_question_asked_too_long_ago_goes_with_the_press():
	"""Fifteen minutes on it can no longer be answered, and its buttons should not stay behind."""
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat)
		question = chat.holding("schedAnswer|mute")
		state = schedules.load_state(ADMIN)
		state["asked_at"] -= dcb.PENDING_ANSWER_SECONDS + 1
		dcb.write_cache_item(f"schedule_state_{ADMIN}", state)
		chat.press("schedAnswer|mute", message_id=question)
		assert question in chat.deleted and schedules.load_state(ADMIN) is None
		assert chat.last == text("error_invalid_selection")
	_clear()


def test_a_question_that_could_not_be_sent_is_not_left_open():
	"""Unseen, a typed one went on taking the admin's messages for a quarter of an hour."""
	_clear()
	with Fleet(), Chat() as chat:
		original = dcb.send_message
		dcb.send_message = lambda **kw: None
		try:
			chat.press("schedAdd", message_id=0)
		finally:
			dcb.send_message = original
		assert schedules.load_state(ADMIN) is None
		chat.type("copia")
		assert chat.order == []
	_clear()


def test_starting_a_question_closes_the_one_before():
	_clear()
	task = _task()
	with Fleet(), Chat() as chat:
		_start(chat)
		first = chat.order[-1]
		schedules.send_task(task)
		chat.press(f"schedField|{task['id']}|cron")
		assert first in chat.deleted and schedules.load_state(ADMIN)["task_id"] == task["id"]
	_clear()


def test_a_task_that_cannot_be_written_says_so():
	_clear()
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		_start(chat, action="mute")
		chat.type("5")
		undo = _unwritable(manager.full_path)
		try:
			chat.press("schedConfirm")
		finally:
			undo()
		assert chat.last.startswith(text("error_adding_schedule", "")[:-len("<code></code>")]), chat.last
		assert schedules.load_state(ADMIN) is None and manager.all() == []
	_clear()


# ============================================================================
# CHANGING A TASK
# ============================================================================

def test_editing_a_container_moves_its_host_with_it():
	"""Saving the name alone left the task on its old host, pointing at a namesake or at nothing."""
	_clear()
	task = _task()
	with Fleet(), Chat() as chat:
		schedules.send_task(task)
		chat.press(f"schedField|{task['id']}|container")
		assert f"{text('current_value')}: nginx" in chat.last
		chat.press(chat.labelled("plex"))
		assert text("schedule_updated_success", "copia") in chat.texts()
		assert f"schedField|{task['id']}|container" in chat.data(), "vuelve a los detalles de la tarea"
	edited = manager.get(task["id"])
	assert (edited["container"], edited["host"]) == ("plex", "h_nas"), edited
	_clear()


def test_an_edit_is_checked_like_an_answer():
	_clear()
	task = _task()
	_task(name="otra")
	with Fleet(), Chat() as chat:
		schedules.send_task(task)
		chat.press(f"schedField|{task['id']}|cron")
		chat.type("0 0 30 2 *")
		assert chat.last.startswith(f"<b>{text('schedule_invalid_cron')}</b>") and "@daily" in chat.last
		question = chat.order[-1]
		chat.type("@hourly")
		assert question in chat.deleted, "la pregunta se quedaba en pantalla tras guardar"
		chat.press(f"schedField|{task['id']}|name")
		chat.type("otra")
		assert chat.last.startswith(f"<b>{text('schedule_name_exists')}</b>"), chat.last
		chat.type("copia")
	assert (manager.get(task["id"])["cron"], manager.get(task["id"])["name"]) == ("@hourly", "copia")
	_clear()


def test_show_output_takes_its_three_answers_and_nothing_else():
	_clear()
	task = _task(action="exec", command="check.sh")
	with Fleet(), Chat() as chat:
		for answer, stored in (("failures", SHOW_OUTPUT_FAILURES), ("yes", True), ("no", False)):
			schedules.send_task(manager.get(task["id"]))
			chat.press(f"schedField|{task['id']}|show_output")
			chat.press(f"schedAnswer|{answer}")
			assert manager.get(task["id"])["show_output"] == stored
		schedules.send_task(manager.get(task["id"]))
		chat.press(f"schedField|{task['id']}|show_output")
		chat.press("schedAnswer|maybe", message_id=chat.order[-1])
		assert chat.last.startswith(f"<b>{text('error_invalid_selection')}</b>")
		assert manager.get(task["id"])["show_output"] is False
	_clear()


def test_a_prune_moves_host_on_its_own_and_a_container_task_never_does():
	"""
	A prune names no container, so it has its own host to change. A container
	task moves with its container: its host alone would point at a namesake.
	"""
	_clear()
	prune = _task(name="Limpieza", action="prune", prune_type="images", host="h_nas", enabled=False)
	restart = _task(name="Reinicio", host="h_nas")
	with Fleet(), Chat() as chat:
		schedules.send_task(prune)
		chat.press(f"schedField|{prune['id']}|host")
		assert [d for d in chat.data()] == ["schedAnswer|h_local", "schedAnswer|h_nas", "schedCancel"], chat.data()
		chat.press("schedAnswer|h_local")
		moved = manager.get(prune["id"])
		assert moved["host"] == "h_local" and moved["enabled"] is False, "movida, no activada: eso lo decide el usuario"
		schedules.send_task(restart)
		assert f"schedField|{restart['id']}|host" not in chat.data()
		chat.press(f"schedField|{restart['id']}|host", message_id=1)
		assert chat.last == text("error_invalid_selection") and schedules.load_state(ADMIN) is None
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		schedules.send_task(manager.get(prune["id"]))
		assert f"schedField|{prune['id']}|host" not in chat.data(), "con un host no hay adónde moverla"
		orphan = manager.update(prune["id"], host="h_nas")
		schedules.send_task(orphan)
		assert f"schedField|{prune['id']}|host" in chat.data(), "salvo si el suyo ya no existe"
		schedules.send_task(manager.update(restart["id"], host="h_nas"))
		assert f"schedField|{restart['id']}|host" not in chat.data()
		assert f"schedField|{restart['id']}|container" in chat.data()
	with Fleet(paused=("h_nas",)), Chat() as chat:
		schedules.send_task(manager.update(prune["id"], host="h_local"))
		assert f"schedField|{prune['id']}|host" not in chat.data(), "un host en pausa no es destino"
	_clear()


def test_a_host_gone_since_it_was_offered_does_not_take_the_task():
	_clear()
	prune = _task(name="Limpieza", action="prune", prune_type="images", host="h_local")
	with Fleet(), Chat() as chat:
		schedules.send_task(prune)
		chat.press(f"schedField|{prune['id']}|host")
		store.set("hosts", [tb.HOST_FIXTURE[0]])
		host_registry.reset()
		chat.press("schedAnswer|h_nas")
		assert chat.last.startswith(f"<b>{text('error_invalid_selection')}</b>")
	assert manager.get(prune["id"])["host"] == "h_local"
	_clear()


def test_a_task_deleted_while_being_edited_is_not_brought_back():
	_clear()
	task = _task()
	with Fleet(), Chat() as chat:
		schedules.send_task(task)
		chat.press(f"schedField|{task['id']}|cron")
		manager.delete(task["id"])
		chat.type("@hourly")
		assert chat.last == text("error_invalid_selection")
		assert manager.all() == [] and schedules.load_state(ADMIN) is None
	_clear()


def test_a_task_on_a_removed_host_cannot_be_switched_on():
	"""
	Its next firing would switch it straight back off. It has to be moved
	first, so the refusal says how and the details come back with the button
	that does it.
	"""
	_clear()
	prune = _task(name="Limpieza", action="prune", prune_type="images", host="h_nas", enabled=False)
	restart = _task(name="Reinicio", host="h_nas", enabled=False)
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
		for task, key, fix in ((prune, "schedule_enable_host_missing_prune", "host"),
								(restart, "schedule_enable_host_missing_container", "container")):
			schedules.send_task(task)
			chat.press(f"schedSwitch|{task['id']}|on")
			assert chat.texts()[-2] == text(key, task["name"]), chat.texts()[-2]
			assert f"schedField|{task['id']}|{fix}" in chat.data()
			assert manager.get(task["id"])["enabled"] is False
		manager.update(prune["id"], enabled=True)
		chat.press(f"schedSwitch|{prune['id']}|off", message_id=1)
		assert manager.get(prune["id"])["enabled"] is False, "apagarla nunca se niega"
		assert text("schedule_disabled", "Limpieza") in chat.texts()
		manager.update(prune["id"], host="h_local")
		chat.press(f"schedSwitch|{prune['id']}|on", message_id=1)
		assert manager.get(prune["id"])["enabled"] is True
	_clear()


def test_switching_off_twice_leaves_it_off():
	"""The button flipped whatever the task was: two taps switched it off and on again."""
	_clear()
	task = _task()
	with Fleet(), Chat() as chat:
		schedules.send_task(task)
		details = chat.order[-1]
		switch = next(data for data in chat.data() if data.startswith("schedSwitch|"))
		chat.press(switch, message_id=details)
		chat.press(switch, message_id=details)
	assert manager.get(task["id"])["enabled"] is False
	_clear()


def test_deleting_asks_first():
	_clear()
	task = _task(name="copia <db>")
	with Fleet(), Chat() as chat:
		schedules.send_pick_list("delete")
		chat.press(f"schedDelete|{task['id']}")
		assert chat.last == text("schedule_delete_confirm", "copia &lt;db&gt;")
		assert manager.get(task["id"]), "borrada sin confirmar"
		chat.press(f"schedDeleteYes|{task['id']}")
		assert manager.all() == []
		assert text("schedule_deleted", "copia &lt;db&gt;") in chat.texts()
		assert chat.last.startswith(text("schedule_menu_title"))
		chat.press(f"schedDeleteYes|{task['id']}", message_id=1)
		assert chat.last == text("error_invalid_selection")
	_clear()


def test_the_lists_name_tasks_by_id_and_go_a_page_at_a_time():
	"""By position, a stale list deleted another task; past a hundred buttons it never arrived."""
	_clear()
	tasks = [_task(name=f"t{number:03}") for number in range(85)]
	with Fleet(), Chat() as chat:
		schedules.send_pick_list("edit")
		assert f"schedOpen|{tasks[0]['id']}" in chat.data() and f"schedOpen|{tasks[84]['id']}" not in chat.data()
		chat.press("schedPickPage|edit|1")
		assert f"schedOpen|{tasks[84]['id']}" in chat.data()
		chat.press(f"schedOpen|{tasks[84]['id']}")
		assert chat.last.startswith("<b>t084</b>"), chat.last
	_clear()


# ============================================================================
# SCHEDULER
# ============================================================================

class Recorder:
	"""Stands in for what the scheduler sets off in the core, and records it."""

	NAMES = ("run", "stop", "restart", "execute_command", "mute", "manager")

	def __init__(self):
		self.calls = []

	def __enter__(self):
		self._original = {name: getattr(dcb, name) for name in self.NAMES}
		for name in ("run", "stop", "restart"):
			setattr(dcb, name, lambda ref, container, from_schedule=False, name=name:
					self.calls.append((name, dcb.ref_host(ref), container, from_schedule)))
		dcb.execute_command = lambda ref, container, command, show_output=True, wait=True, on_result=None: \
			self.calls.append(("exec", dcb.ref_host(ref), container, command, show_output, wait))
		dcb.mute = lambda minutes, asked=False, extend_only=False: self.calls.append(("mute", minutes, extend_only))
		manager_of = self._original["manager"]
		dcb.manager = lambda host_id=None: self.calls.append(("manager", host_id)) or manager_of(host_id)
		return self

	def __exit__(self, *exc):
		for name, value in self._original.items():
			setattr(dcb, name, value)

	def actions(self):
		return [call for call in self.calls if call[0] != "manager"]


def test_a_task_acts_on_its_container_on_its_own_host_and_nowhere_else():
	"""Acting on a namesake on another machine would be silent and, for stop or exec, destructive."""
	_clear()
	scheduler = schedules.Scheduler()
	with Fleet(containers={"h_local": ["plex"], "h_nas": ["plex"]}), Chat(), Recorder() as recorder:
		for action in ("run", "stop", "restart"):
			assert scheduler.run_task(_task(name=action, action=action, container="plex", host="h_nas")) == "ok"
		assert scheduler.run_task(_task(name="exec", action="exec", container="plex", host="h_nas",
										command="ls", show_output=SHOW_OUTPUT_FAILURES)) == "running"
		assert scheduler.run_task(_task(name="mute", action="mute", minutes=30)) == "ok"
	assert recorder.actions() == [
		("run", "h_nas", "plex", True), ("stop", "h_nas", "plex", True), ("restart", "h_nas", "plex", True),
		("exec", "h_nas", "plex", "ls", SHOW_OUTPUT_FAILURES, False), ("mute", 30, True)], recorder.actions()
	_clear()


def test_a_paused_host_is_skipped_quietly():
	"""It is coming back on purpose: neither switched off nor announced on every run."""
	_clear()
	task = _task(container="plex", host="h_nas")
	with Fleet(paused=("h_nas",)), Chat() as chat, Recorder() as recorder:
		assert schedules.Scheduler().run_task(task) == "skipped"
		assert recorder.calls == [] and chat.order == [] and chat.notices == []
	assert manager.get(task["id"])["enabled"] is True
	_clear()


def test_a_host_that_does_not_answer_skips_its_tasks_and_says_so_once():
	"""An ssh machine rebooting during the nightly restart used to cost the task for good."""
	_clear()
	scheduler = schedules.Scheduler()
	with Fleet(unreachable=("h_nas", "h_local")), Chat() as chat, Recorder() as recorder:
		for action in ("run", "stop", "restart", "exec"):
			assert scheduler.run_task(_task(name=f"{action} nas", action=action, container="plex",
											host="h_nas", command="ls")) == "skipped"
		assert chat.notices == [text("schedule_skipped_host_down", "run nas", "nas")], chat.notices
		assert recorder.actions() == []
	assert all(task["enabled"] for task in manager.all())
	_clear()


def test_a_host_known_to_be_down_is_not_asked_again_by_every_task():
	"""
	Each of its tasks waited out the timeout in turn, holding up every task
	behind them. It is asked once a minute whether it is back, and its tasks
	run again as soon as it is.
	"""
	_clear()
	scheduler = schedules.Scheduler()
	tasks = [_task(name=f"t{number}", container="plex", host="h_nas") for number in range(3)]
	original = host_registry.status_snapshot
	with Fleet(unreachable=("h_nas",)) as fleet, Chat(), Recorder() as recorder:
		for task in tasks:
			assert scheduler.run_task(task) == "skipped"
		assert recorder.calls.count(("manager", "h_nas")) == 1, recorder.calls
		fleet.unreachable = ()
		host_registry.status_snapshot = lambda entries=None, **kw: {e["id"]: (True, "") for e in entries}
		try:
			scheduler._tick(datetime(2026, 10, 2, 3, 0))
		finally:
			host_registry.status_snapshot = original
		assert scheduler.run_task(tasks[0]) == "ok"
	_clear()


def test_a_missing_container_is_skipped_and_said_once_until_it_is_back():
	"""
	A container being recreated when its task came round switched the task off
	for good. It is skipped instead, said once, and said again when it is back.
	"""
	_clear()
	scheduler = schedules.Scheduler()
	containers = {"h_local": []}
	task = _task(name="Reinicio")
	with Fleet(hosts=[tb.HOST_FIXTURE[0]], containers=containers), Chat() as chat, Recorder() as recorder:
		assert scheduler.run_task(task) == "skipped"
		assert scheduler.run_task(task) == "skipped"
		assert chat.notices == [text("schedule_skipped_container_missing", "Reinicio", "nginx")], chat.notices
		containers["h_local"].append("nginx")
		assert scheduler.run_task(task) == "ok"
		assert scheduler.run_task(task) == "ok"
		assert chat.notices[1:] == [text("schedule_container_back", "Reinicio", "nginx")], chat.notices
		assert [call[0] for call in recorder.actions()] == ["restart", "restart"]

		# Pointed at another container that is not there either: said afresh.
		chat.notices.clear()
		moved = manager.update(task["id"], container="web")
		scheduler._missing &= {(moved["id"], moved["host"], moved["container"])}
		assert scheduler.run_task(moved) == "skipped"
		assert chat.notices == [text("schedule_skipped_container_missing", "Reinicio", "web")]

		# And quiet while muted, like every notice of the scheduler.
		chat.notices.clear()
		original = dcb.is_muted
		dcb.is_muted = lambda: True
		try:
			scheduler._missing.clear()
			scheduler.run_task(moved)
		finally:
			dcb.is_muted = original
		assert chat.notices == []
	assert manager.get(task["id"])["enabled"] is True
	_clear()


def test_a_task_on_a_removed_host_is_switched_off_saying_why():
	"""Not paused nor down: gone, and not coming back. It has to be moved."""
	_clear()
	scheduler = schedules.Scheduler()
	tasks = [_task(name="Limpieza", action="prune", prune_type="images", host="h_nas"),
				_task(name="Reinicio", container="plex", host="h_nas")]
	with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat, Recorder() as recorder:
		for task in tasks:
			assert scheduler.run_task(task) == "failed"
		original = dcb.is_muted
		dcb.is_muted = lambda: True
		try:
			assert scheduler.run_task(manager.get(tasks[0]["id"])) == "failed"
		finally:
			dcb.is_muted = original
		assert chat.notices == [text("schedule_disabled_host_missing", task["name"]) for task in tasks + tasks[:1]], \
			"lo dice aunque esté silenciado: la tarea se queda apagada"
		assert recorder.calls == [], "se preguntó a un host que ya no existe"
		assert scheduler._down_hosts == {}
	assert [task["enabled"] for task in manager.all()] == [False, False]

	# Unless settings.json cannot be read: every host looks gone then.
	manager.update(tasks[0]["id"], enabled=True)
	original = store.settings_unreadable
	store.settings_unreadable = lambda: "Expecting ',' delimiter"
	try:
		with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat():
			assert schedules.host_missing(manager.get(tasks[0]["id"])) is False
			assert scheduler.run_task(manager.get(tasks[0]["id"])) == "skipped"
	finally:
		store.settings_unreadable = original
	assert manager.get(tasks[0]["id"])["enabled"] is True
	_clear()


def test_a_scheduled_prune_does_not_hold_up_the_scheduler():
	"""
	A prune can take minutes. It goes to a worker of its own, which still runs
	them one at a time and in order: a containers prune frees images for the
	images one due with it.
	"""
	_clear()
	release = threading.Event()
	ran = []
	owner = MagicMock()

	def pruning(kind):
		def prune():
			release.wait(5)
			ran.append(kind)
			return (f"ok {kind}", None, False)
		return prune

	owner.prune_containers.side_effect = pruning("containers")
	owner.prune_images.side_effect = pruning("images")
	original = dcb.manager
	dcb.manager = lambda host_id=None: owner
	try:
		with Fleet(hosts=[tb.HOST_FIXTURE[0]]), Chat() as chat:
			scheduler = schedules.Scheduler()
			started = time.time()
			for kind in ("containers", "images"):
				assert scheduler.run_task(_task(name=kind, action="prune", prune_type=kind, show_output=True)) == "running"
			assert time.time() - started < 1 and ran == [], "el bucle esperó al prune"
			release.set()
			deadline = time.time() + 5
			while len(chat.order) < 2 and time.time() < deadline:
				time.sleep(0.01)
			assert ran == ["containers", "images"] and chat.texts() == ["ok containers", "ok images"], chat.texts()
	finally:
		release.set()
		dcb.manager = original
	_clear()


def _prune(show_output):
	return {"id": 999, "name": "limpia", "action": "prune", "prune_type": "images", "show_output": show_output}


def test_a_scheduled_prune_set_to_failures_only_speaks_when_it_removed_something():
	"""A prune with nothing to remove is the usual case, and is not news."""
	owner = MagicMock()
	scheduler = schedules.Scheduler()
	with Chat() as chat:
		owner.prune_images.return_value = ("nada", "{'ImagesDeleted': None}", False)
		scheduler._run_prune(owner, _prune(SHOW_OUTPUT_FAILURES))
		scheduler._run_prune(owner, _prune(False))
		assert chat.order == [] and chat.documents == []
		owner.prune_images.return_value = ("1 GB", "{'ImagesDeleted': [...]}", True)
		scheduler._run_prune(owner, _prune(SHOW_OUTPUT_FAILURES))
		assert [document["caption"] for document in chat.documents] == ["1 GB"]
		owner.prune_images.return_value = ("falló", None, False)
		scheduler._run_prune(owner, _prune(SHOW_OUTPUT_FAILURES))
		assert chat.last == "falló"
		chat.documents.clear()
		owner.prune_images.return_value = ("nada", "{}", False)
		scheduler._run_prune(owner, _prune(True))
		assert len(chat.documents) == 1, "con «Sí» se dice también la que no ha borrado nada"


def test_a_scheduled_prune_says_which_host_it_cleaned():
	owner = MagicMock(host_id="h_nas")
	owner.prune_images.return_value = ("1 GB", "{'ImagesDeleted': [...]}", True)
	with Fleet(), Chat() as chat:
		schedules.Scheduler()._run_prune(owner, _prune(True))
		owner.prune_images.return_value = ("falló", None, False)
		schedules.Scheduler()._run_prune(owner, _prune(True))
		label = dcb.host_label("h_nas")
		assert label and chat.documents[0]["caption"] == f"{label}1 GB", chat.documents
		assert chat.last == f"{label}falló"


# ============================================================================
# WHEN IT RUNS, AND HOW IT WENT
# ============================================================================

def test_a_moment_reads_as_people_say_it():
	import formatting
	now = datetime(2026, 10, 26, 12, 0)
	assert formatting.day_and_time(datetime(2026, 10, 26, 4, 5), now) == text("day_today", "04:05")
	assert formatting.day_and_time(datetime(2026, 10, 27, 4, 5), now) == text("day_tomorrow", "04:05")
	assert formatting.day_and_time(datetime(2026, 10, 25, 23, 59), now) == text("day_yesterday", "23:59")
	assert formatting.day_and_time(datetime(2026, 11, 1, 4, 5), now) == "2026-11-01 04:05"


def test_the_next_run_shows_beside_the_cron_from_the_moment_it_is_typed():
	"""A cron that means something other than intended is the usual mistake: this is where it shows."""
	from croniter import croniter
	_clear()
	with Fleet(), Chat() as chat:
		_start(chat, cron="30 4 * * *")
		import formatting
		expected = formatting.day_and_time(croniter("30 4 * * *", datetime.now()).get_next(datetime))
		assert f"<code>30 4 * * *</code> ({text('schedule_next', expected)})" in chat.last, chat.last
		on_reboot = _task(name="al arrancar", cron="@reboot")
		off = _task(name="apagada", cron="30 4 * * *", enabled=False)
		schedules.send_task(on_reboot)
		assert f"({text('schedule_next_reboot')})" in chat.last
		schedules.send_task(off)
		assert text("schedule_next", "").split(":")[0] not in chat.last, "una tarea apagada no tiene próxima"
	_clear()


def test_run_now_runs_it_as_its_cron_would_and_shows_how_it_went():
	"""Testing a task meant setting its cron to every minute and waiting."""
	_clear()
	task = _task(name="Reinicio", enabled=False)
	with Fleet(), Chat() as chat, Recorder() as recorder:
		schedules.send_task(task)
		assert f"schedRun|{task['id']}" in chat.data()
		chat.press(f"schedRun|{task['id']}")
		assert recorder.actions() == [("restart", "h_local", "nginx", True)], recorder.actions()
		assert f"<b>{text('schedule_label_last')}:</b>" in chat.last and "✅" in chat.last, chat.last
		assert f"schedRun|{task['id']}" in chat.data(), "vuelve al detalle de la tarea"
	assert manager.get(task["id"])["enabled"] is False, "probarla no la activa"
	_clear()


def test_the_last_run_says_how_it_went():
	_clear()
	scheduler = schedules.Scheduler()
	with Fleet(containers={"h_local": ["db"]}), Chat() as chat:
		missing = _task(name="Falta", container="web")
		scheduler.run_task(missing)
		schedules.send_task(manager.get(missing["id"]))
		assert f"⏭️ {text('schedule_reason_container_missing')}" in chat.last, chat.last

		# A command: running while it runs, and then how it ended.
		release = threading.Event()
		original = dcb.DockerManager.execute_command
		dcb.DockerManager.execute_command = lambda owner, **kw: release.wait(5) and ("", 3)
		try:
			command = _task(name="Copia", action="exec", container="db", command="false", show_output=False)
			assert scheduler.run_task(command) == "running"
			schedules.send_task(manager.get(command["id"]))
			assert "⏳" in chat.last, chat.last
			release.set()
			deadline = time.time() + 5
			while "❌" not in (schedules.last_run_text(command) or "") and time.time() < deadline:
				time.sleep(0.01)
			assert schedules.last_run_text(command).endswith(f"❌ {text('schedule_reason_exit_code', '3')}")
		finally:
			release.set()
			dcb.DockerManager.execute_command = original

		# In the list as well, under each task.
		schedules.send_menu()
		assert text("schedule_reason_container_missing") in chat.last and text("schedule_label_last") in chat.last

		# A prune, once its worker is done with it.
		owner = MagicMock(host_id="h_local")
		owner.prune_images.return_value = ("falló", None, False)
		scheduler._run_prune(owner, _prune(False))
		assert schedules.last_run_text({"id": 999}).endswith(f"❌ {text('schedule_reason_error')}")
		schedules.forget_run(999)
	_clear()


def test_the_last_run_goes_with_its_task_and_survives_a_hand_edit():
	_clear()
	task = _task()
	schedules.record_run(task["id"], "ok")
	with Fleet(), Chat() as chat:
		schedules.send_pick_list("delete")
		chat.press(f"schedDelete|{task['id']}")
		chat.press(f"schedDeleteYes|{task['id']}")
	assert str(task["id"]) not in (store.state_get("schedule_runs") or {})

	again = _task(name="otra")
	store.state_set("schedule_runs", {str(again["id"]): {"at": "ayer", "outcome": "raro"}})
	try:
		assert schedules.last_run_text(again) is None
		with Fleet(), Chat() as chat:
			schedules.send_task(again)
			assert text("schedule_label_last") not in chat.last
	finally:
		store.state_set("schedule_runs", {})
	_clear()


def test_the_scheduler_runs_every_minute_exactly_once():
	"""
	It slept 60 s after the work and looked at whatever minute it landed in:
	sooner or later a lap jumped from :00:55 to :02:05, and :01 never ran.
	"""
	_clear()
	_task(name="a la 1", cron="1 3 * * *", action="mute", minutes=1)
	_task(name="cada 5", cron="*/5 * * * *", action="mute", minutes=1)
	_task(name="apagada", cron="* * * * *", action="mute", minutes=1, enabled=False)
	_task(name="al arrancar", cron="@reboot", action="mute", minutes=1)
	ran = []
	scheduler = schedules.Scheduler()
	scheduler.run_task = lambda task: ran.append(task["name"])
	scheduler._tick(datetime(2026, 10, 2, 3, 0, 55))
	scheduler._tick(datetime(2026, 10, 2, 3, 2, 5))     # se ha saltado las 3:01
	assert ran == ["cada 5", "a la 1"], ran
	ran.clear()
	scheduler._tick(datetime(2026, 10, 2, 3, 2, 40))    # el mismo minuto otra vez
	assert ran == []
	scheduler._tick(datetime(2026, 10, 2, 3, 6, 1))     # de 3:03 a 3:06: una vez, no una por minuto
	assert ran == ["cada 5"], ran
	ran.clear()
	scheduler._tick(datetime(2026, 10, 2, 3, 5, 0))     # el reloj atrasa: nada dos veces
	assert ran == []
	scheduler._tick(datetime(2026, 10, 2, 9, 10, 0))    # un hueco largo no se repite
	assert ran == ["cada 5"], ran
	_clear()


def test_reboot_tasks_run_when_the_scheduler_starts_not_when_it_is_built():
	"""Built on the main thread, an exec that hung there held up polling."""
	_clear()
	_task(name="al arrancar", cron="@reboot", action="mute", minutes=1)
	_task(name="apagada", cron="@reboot", action="mute", minutes=1, enabled=False)
	ran = []
	scheduler = schedules.Scheduler()
	scheduler.run_task = lambda task: ran.append(task["name"])
	assert ran == []
	scheduler._run_reboot_tasks()
	assert ran == ["al arrancar"], ran
	_clear()


def test_the_user_hears_when_a_host_whose_tasks_were_skipped_is_back():
	"""
	Checked every minute: a daily task would say so a day late. Said here
	unless HostAvailability announced the outage, which then announces the
	return too: a remote host back within its two minutes was never said by
	either, after the task's notice promised it would be.
	"""
	_clear()
	original = host_registry.status_snapshot
	long_ago = time.monotonic() - dcb.HostAvailability.DOWN_AFTER_SECONDS - 1
	with Fleet(hosts=tb.HOST_FIXTURE + [{"id": "h_pi", "alias": "pi", "url": "tcp://pi:2375"}]), Chat() as chat:
		scheduler = schedules.Scheduler()
		scheduler._down_hosts = {"h_local": long_ago, "h_nas": long_ago, "h_pi": time.monotonic()}
		try:
			host_registry.status_snapshot = lambda entries=None, **kw: {e["id"]: (False, "down") for e in entries}
			scheduler._tick(datetime(2026, 10, 2, 3, 0))
			assert chat.notices == [] and set(scheduler._down_hosts) == {"h_local", "h_nas", "h_pi"}
			host_registry.status_snapshot = lambda entries=None, **kw: {e["id"]: (True, "") for e in entries}
			scheduler._tick(datetime(2026, 10, 2, 3, 1))
			scheduler._tick(datetime(2026, 10, 2, 3, 2))
		finally:
			host_registry.status_snapshot = original
		assert sorted(chat.notices) == sorted(text("schedule_host_back", alias) for alias in ("casa", "pi")), chat.notices
		assert scheduler._down_hosts == {}


def test_the_spring_clock_change_does_not_skip_the_lost_hour():
	"""From 01:59 to 03:00 in one real minute, taken for an hour the bot was off."""
	import datetime as dt
	scheduler = schedules.Scheduler()

	# The wall clock jumps an hour in a minute of real time, which is what
	# timestamp() measures: faked so the test does not depend on the time
	# zone of the machine running it.
	class Clock(dt.datetime):
		def timestamp(self):
			return 60.0 if (self.hour, self.minute) == (3, 0) else 0.0

	scheduler._last_minute = Clock(2026, 1, 10, 1, 59)
	assert len(scheduler._minutes_due(Clock(2026, 1, 10, 3, 0))) == 61
	scheduler._last_minute = dt.datetime(2026, 1, 10, 1, 59)
	assert scheduler._minutes_due(dt.datetime(2026, 1, 10, 3, 0)) == [dt.datetime(2026, 1, 10, 3, 0)], \
		"una hora de verdad sigue siendo un hueco que no se repite"
