"""
/schedule: the tasks the bot runs on its own.

What a task is lives in schedule_manager and config.SCHEDULE_ACTION_FIELDS.
This module is everything built on them, in three parts:

- the screens: the list, a task's details, and its deletion. A task is drawn
  by one function, so every screen shows it alike;
- the questions: creating a task asks, in order, each field its action needs;
  editing one asks a single field. Both go through the same question, the same
  check and the same answer;
- the scheduler: a thread that wakes every minute and runs what is due.

It reaches the core as `core.`, like callbacks.py, so a test can stand in for
what it calls. The core does not import it while loading, only where it hands
over to it, so the two do not import each other in a circle.
"""

import functools
import html
import queue
import threading
import time
from datetime import datetime, timedelta

from croniter import croniter
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

import core
import host_registry
import store
from callback_registry import callback
from config import SCHEDULE_ACTION_FIELDS, SCHEDULE_ACTIONS, SCHEDULE_PRUNE_TYPES, SHOW_OUTPUT_FAILURES
from formatting import day_and_time
from i18n import get_text
from logger import debug, error, warning
from schedule_manager import ScheduleError, clean, fields_of

# ============================================================================
# HOW A TASK READS
# ============================================================================

# The buttons that answer "show output?", by the value each one stores.
SHOW_OUTPUT_CHOICES = {"yes": True, "no": False, "failures": SHOW_OUTPUT_FAILURES}


def fields_in_order(action):
	"""A task's fields in the order they are asked and shown: what it is, when, what it does, then what its action needs."""
	return ("name", "cron", "action") + fields_of(action)


def host_missing(task):
	"""
	Whether a task acts on a host the bot no longer has.

	Only a removed one: a paused or unreachable host is still registered and
	comes back. Never while settings.json cannot be read either: the host list
	is then only the defaults, every task's host looks gone, and answering yes
	switched every task off.
	"""
	host = task.get("host")
	return bool(host) and not store.settings_unreadable() and host_registry.host(host) is None


def host_worth_showing(task):
	"""Whether a task's host says anything: with several hosts, or when it is gone."""
	return not host_registry.is_single_host() or host_missing(task)


def show_output_text(value):
	"""How a task's show_output reads on screen."""
	if value == SHOW_OUTPUT_FAILURES:
		return get_text("schedule_show_output_failures")
	return get_text("schedule_yes") if value else get_text("schedule_no")


def show_output_question(action):
	"""The question that asks for show_output, with what its third answer means for `action`."""
	hint = "schedule_show_output_failures_prune" if action == "prune" else "schedule_show_output_failures_exec"
	return f"{get_text('schedule_ask_show_output')}\n<i>{get_text(hint)}</i>"


def field_text(field, task):
	"""How `field` of `task` reads on screen. Escaped: names and commands are typed by hand."""
	value = task.get(field)
	if field == "host":
		return get_text("schedule_host_missing") if host_missing(task) else core.host_alias(value)
	if field == "cron" and task.get("enabled", True):
		# Said beside it, from the moment it is typed: a cron that means
		# something else than intended is the usual mistake, and this is
		# where it shows.
		return f"<code>{html.escape(value)}</code> ({next_run_text(value)})"
	if field in ("cron", "command"):
		return f"<code>{html.escape(str(value))}</code>"
	if field == "prune_type":
		return get_text(f"schedule_prune_{value}")
	if field == "show_output":
		return show_output_text(value)
	return html.escape(str(value))


def next_run_text(cron):
	"""When a cron fires next."""
	if cron == "@reboot":
		return get_text("schedule_next_reboot")
	return get_text("schedule_next", day_and_time(croniter(cron, datetime.now()).get_next(datetime)))


# How a run went, as its details show it, and why when it did not go well.
OUTCOME_ICONS = {"ok": "✅", "running": "⏳", "skipped": "⏭️", "failed": "❌"}

_runs_lock = threading.Lock()


def record_run(task_id, outcome, reason=None, *args):
	"""
	Notes how a task's last run went, for its details to say.

	In the bot's state rather than with the tasks: it is what happened, not
	what was asked for. The reason is a text key and its arguments, so it
	reads in whatever language the bot speaks when it is shown.
	"""
	with _runs_lock:
		runs = dict(store.state_get("schedule_runs") or {})
		runs[str(task_id)] = {"at": time.time(), "outcome": outcome, "reason": reason,
								"args": [str(arg) for arg in args]}
		store.state_set("schedule_runs", runs)


def forget_run(task_id):
	with _runs_lock:
		runs = dict(store.state_get("schedule_runs") or {})
		if runs.pop(str(task_id), None) is not None:
			store.state_set("schedule_runs", runs)


def record_exec(task_id, result):
	"""Notes how a scheduled command ended, once it has: `result` is what execute_command hands back."""
	if result is None:
		record_run(task_id, "failed", "schedule_reason_error")
	elif result[1] in (0, None):
		record_run(task_id, "ok")
	else:
		record_run(task_id, "failed", "schedule_reason_exit_code", result[1])


def last_run_text(task):
	"""When the task last ran and how it went, or None when it has not run yet."""
	run = (store.state_get("schedule_runs") or {}).get(str(task["id"]))
	try:
		text = f"{day_and_time(datetime.fromtimestamp(run['at']))} · {OUTCOME_ICONS[run['outcome']]}"
		if run.get("reason"):
			text += f" {get_text(run['reason'], *run.get('args') or [])}"
		return text
	except (TypeError, KeyError, ValueError, OverflowError, OSError):
		# Not run yet, or a state file edited into something else.
		return None


def state_lines(task):
	"""Whether it is on, and how it went the last time."""
	lines = [status_line(task)]
	last = last_run_text(task)
	if last:
		lines.append(f"<b>{get_text('schedule_label_last')}:</b> {last}")
	return lines


def task_lines(task, skip=()):
	"""
	One line per field the task has, in the order they are asked.

	The one drawing of a task: the questions, the list and the details used to
	draw it each their own way, and a change reached one of them and not the
	others. A half-made task draws what it has so far. The host is left out
	with a single host, where it says nothing, unless it no longer exists.
	"""
	lines = []
	for field in fields_in_order(task.get("action")):
		if field in skip or task.get(field) is None:
			continue
		if field == "host" and not host_worth_showing(task):
			continue
		lines.append(f"<b>{get_text(f'schedule_label_{field}')}:</b> {field_text(field, task)}")
	return lines


def status_line(task):
	key, icon = ("schedule_status_enabled", "🟢") if task["enabled"] else ("schedule_status_disabled", "🔴")
	return f"<b>{get_text('schedule_label_status')}:</b> {icon} {get_text(key)}"


def editable_fields(task):
	"""What the details screen offers to change."""
	fields = ["name", "cron"]
	for field in SCHEDULE_ACTION_FIELDS[task["action"]]:
		# A prune's own host, only where there is somewhere to move it. A
		# container task moves host with its container instead: the name only
		# means something on one machine.
		if field == "host" and not host_worth_showing(task):
			continue
		fields.append(field)
	return fields


# ============================================================================
# SCREENS
# ============================================================================

def button(text_key, data):
	return InlineKeyboardButton(get_text(text_key), callback_data=data)


def add_page(markup, buttons, page, page_data, row_width=1):
	"""
	Adds one page of `buttons` to `markup`, and the arrows to the others.

	Telegram refuses a keyboard of more than a hundred buttons, and with it
	the whole message: a list too long for one page was the only way into the
	tasks, or the only way to pick a container, and it never arrived.
	`page_data(n)` is what the arrow to page n sends.
	"""
	page, pages = core.update_keyboard_page(buttons, page)
	size = core.UPDATE_KEYBOARD_PAGE_SIZE
	shown = buttons[page * size:(page + 1) * size]
	for start in range(0, len(shown), row_width):
		markup.row(*shown[start:start + row_width])
	if pages > 1:
		arrows = []
		if page > 0:
			arrows.append(InlineKeyboardButton("◀️", callback_data=page_data(page - 1)))
		arrows.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=page_data(page)))
		if page < pages - 1:
			arrows.append(InlineKeyboardButton("▶️", callback_data=page_data(page + 1)))
		markup.row(*arrows)


def send_menu():
	"""The /schedule menu: every task, and what can be done with them."""
	tasks = core.schedule_manager.all()
	parts = [get_text("schedule_menu_title")]
	if tasks:
		parts.append(f"<b>{get_text('schedule_current_schedules')}</b>")
		for number, task in enumerate(tasks, 1):
			lines = state_lines(task) + task_lines(task, skip=("name",))
			parts.append(f"<b>{number}. {html.escape(task['name'])}</b>\n" + "\n".join(f"  {line}" for line in lines))
	else:
		parts.append(get_text("schedule_no_schedules"))

	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(button("schedule_button_add", "schedAdd"),
				button("schedule_button_edit", "schedPick|edit"),
				button("schedule_button_delete", "schedPick|delete"),
				button("button_close", "cerrar"))
	# Split like any long listing: past 4096 characters, about twenty tasks,
	# Telegram refused the menu and with it the only way to manage them.
	core.send_split_message("\n\n".join(parts), reply_markup=markup)


# Where a press in each list takes the task it names.
PICK_TARGETS = {"edit": "schedOpen", "delete": "schedDelete"}


def pick_markup(purpose, page=0):
	markup = InlineKeyboardMarkup()
	numbered = [InlineKeyboardButton(str(number), callback_data=f"{PICK_TARGETS[purpose]}|{task['id']}")
				for number, task in enumerate(core.schedule_manager.all(), 1)]
	add_page(markup, numbered, page, lambda n: f"schedPickPage|{purpose}|{n}", row_width=5)
	markup.row(button("button_close", "cerrar"))
	return markup


def send_pick_list(purpose):
	"""The numbered list to choose a task from, to edit or to delete it."""
	tasks = core.schedule_manager.all()
	if not tasks:
		core.send_message(message=get_text("schedule_no_schedules"))
		return
	header = get_text("schedule_select_to_edit" if purpose == "edit" else "schedule_select_to_delete")
	names = "\n".join(f"{number}. <code>{html.escape(task['name'])}</code>" for number, task in enumerate(tasks, 1))
	core.send_split_message(f"{header}\n\n{names}", reply_markup=pick_markup(purpose))


def send_task(task):
	"""A task's details, with a button for each thing about it that can change."""
	lines = [f"<b>{html.escape(task['name'])}</b>", ""] + state_lines(task) + task_lines(task, skip=("name",))
	markup = InlineKeyboardMarkup(row_width=1)
	for field in editable_fields(task):
		markup.add(button(f"schedule_edit_{field}", f"schedField|{task['id']}|{field}"))
	# To try it without waiting for its cron, the way its cron would run it.
	markup.add(button("schedule_button_run_now", f"schedRun|{task['id']}"))
	# Saying what it asks for, not "the other one": two taps on a button that
	# flipped the task switched it off and back on.
	if task["enabled"]:
		markup.add(button("schedule_button_disable", f"schedSwitch|{task['id']}|off"))
	else:
		markup.add(button("schedule_button_enable", f"schedSwitch|{task['id']}|on"))
	markup.add(button("button_close", "cerrar"))
	core.send_message(message="\n".join(lines) + f"\n\n{get_text('schedule_edit_what')}", reply_markup=markup)


def switch(task, enable):
	"""Switches a task on or off, and shows it again."""
	if enable and host_missing(task):
		# Its next firing would only switch it off again. It has to be moved
		# first, so its details come back with the button that does it.
		key = ("schedule_enable_host_missing_prune" if task["action"] == "prune"
				else "schedule_enable_host_missing_container")
		core.send_message(message=get_text(key, html.escape(task["name"])))
		send_task(task)
		return
	task = core.schedule_manager.update(task["id"], enabled=enable)
	if task is None:
		core.send_message(message=get_text("error_invalid_selection"))
		return
	core.send_message(message=get_text("schedule_enabled" if enable else "schedule_disabled", html.escape(task["name"])))
	send_task(task)


def ask_delete(task):
	"""Asks before deleting: a cron and a command typed long ago are not easily written again."""
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(button("button_confirm", f"schedDeleteYes|{task['id']}"), button("button_cancel", "cerrar"))
	core.send_message(message=get_text("schedule_delete_confirm", html.escape(task["name"])), reply_markup=markup)


def delete(task_id):
	removed = core.schedule_manager.delete(task_id)
	if removed is None:
		core.send_message(message=get_text("error_invalid_selection"))
		return
	forget_run(task_id)
	core.send_message(message=get_text("schedule_deleted", html.escape(removed["name"])))
	send_menu()


# ============================================================================
# QUESTIONS
# ============================================================================
#
# One question is open per user at a time, in the session cache:
#
#	task_id   the task being edited, or None while creating one
#	task      the answers so far, while creating
#	asking    the field asked for, or "confirm" once a new task is complete
#	prompt    the message that asks it: only a press on that one answers it
#	choices   the containers on offer, as [host, name], and the page shown
#
# A container travels as its place in `choices`: its host and its name would
# not fit in the 64 bytes of a button.

# One answer at a time per user. Presses are handled on several threads, and
# a double tap ran two answers to the same question together: the second
# answered a question that was no longer open —Confirm saved the task, and
# then asked for another name because that one was taken. Per user, so one
# admin's question never waits on another's.
_answering = {}


def answering(user_id):
	"""The lock `user_id`'s questions are answered under."""
	return _answering.setdefault(user_id, threading.Lock())


def one_at_a_time(handler):
	"""Runs a button's handler holding the lock of whoever pressed it."""
	@functools.wraps(handler)
	def locked(ctx):
		with answering(ctx.userId):
			return handler(ctx)
	return locked


# The questions answered by typing, and what asks each.
TYPED = {
	"name": "schedule_ask_name",
	"cron": "schedule_ask_cron",
	"minutes": "schedule_ask_minutes",
	"command": "schedule_ask_command",
}


def load_state(user_id):
	"""The question open for `user_id`, or None."""
	state = core.read_cache_item(f"schedule_state_{user_id}")
	return state if isinstance(state, dict) and state.get("asking") else None


def save_state(user_id, state):
	"""Keeps the question, with where and when it was asked: only there, and for a while, can it be answered."""
	core.write_cache_item(f"schedule_state_{user_id}", core.asked_here(state))


def forget_state(user_id):
	core.delete_cache_item(f"schedule_state_{user_id}")


def close(user_id, state):
	"""
	Ends a question: forgets it and takes its message away.

	Every way a question ends goes through here. An edit answered by typing
	used to forget the question and leave it on screen, with a Cancel that no
	longer cancelled anything.
	"""
	forget_state(user_id)
	if state.get("prompt"):
		core.delete_message(state["prompt"])


def answer_button(text, value):
	return InlineKeyboardButton(text, callback_data=f"schedAnswer|{value}")


def containers():
	"""The containers a task can act on, on every host that answers, as [host, name]. Never the bot's own."""
	return [[entry["id"], container.name]
			for entry, _owner, listed in core.hosts_with_containers()
			for container in listed
			if not core.is_own_container(entry["id"], container.id, container.name)]


def container_markup(state):
	markup = InlineKeyboardMarkup()
	single = host_registry.is_single_host()
	choices = [answer_button(name if single else f"{name} · {host_registry.alias(host)}", index)
				for index, (host, name) in enumerate(state["choices"])]
	add_page(markup, choices, state.get("page"), lambda n: f"schedPage|{n}")
	markup.row(button("button_cancel", "schedCancel"))
	return markup


def question(field, task, state):
	"""What asks for `field`, and the buttons that answer it."""
	if field == "container":
		return get_text("schedule_ask_container"), container_markup(state)
	markup = InlineKeyboardMarkup(row_width=2)
	if field in TYPED:
		text = core.typed_prompt(TYPED[field])
	elif field == "action":
		# The token on the button, because it is the vocabulary of the feature,
		# and what each one does, in the user's language, beside it.
		text = get_text("schedule_ask_action") + "\n\n" + "\n".join(
			f"<code>{action}</code> · {get_text(f'schedule_action_{action}')}" for action in SCHEDULE_ACTIONS)
		markup.add(*[answer_button(action, action) for action in SCHEDULE_ACTIONS])
	elif field == "prune_type":
		text = get_text("schedule_ask_prune_type")
		markup.add(*[answer_button(get_text(f"schedule_prune_{kind}"), kind) for kind in SCHEDULE_PRUNE_TYPES])
	elif field == "host":
		text = get_text("pick_a_host")
		for entry in host_registry.hosts():
			markup.row(answer_button(f"🖥️ {entry.get('alias') or entry['id']}", entry["id"]))
	elif field == "show_output":
		text = show_output_question(task.get("action"))
		markup.add(button("button_yes", "schedAnswer|yes"), button("button_no", "schedAnswer|no"))
		markup.row(button("button_show_output_failures", "schedAnswer|failures"))
	markup.row(button("button_cancel", "schedCancel"))
	return text, markup


def task_of(state):
	"""The task a question is about: the one being made, or the stored one being edited."""
	if state.get("task_id") is None:
		return state["task"]
	return core.schedule_manager.get(state["task_id"])


def ask(user_id, state, error_key=None):
	"""
	Sends the question `state` is at, in place of the one before it, with why
	the last answer was refused when it was.
	"""
	task = task_of(state)
	field = state["asking"]
	if task is None:
		# Deleted while it was being edited.
		close(user_id, state)
		core.send_message(message=get_text("error_invalid_selection"))
		return
	if field == "container" and not state.get("choices"):
		state["choices"], state["page"] = containers(), 0
		if not state["choices"]:
			close(user_id, state)
			core.send_message(message=get_text("error_no_containers_available"))
			return

	if field == "confirm":
		text = get_text("schedule_confirm_title") + "\n\n" + "\n".join(task_lines(task))
		markup = InlineKeyboardMarkup(row_width=2)
		markup.add(button("button_confirm", "schedConfirm"), button("button_cancel", "schedCancel"))
	else:
		asked, markup = question(field, task, state)
		if state.get("task_id") is None:
			# What has been answered so far, above the question, growing
			# downwards as the answers come in.
			text = "\n\n".join(part for part in ("\n".join(task_lines(task)), asked) if part)
		else:
			text = (f"<b>{get_text(f'schedule_edit_{field}')}</b>\n\n{asked}\n\n"
					f"<i>{get_text('current_value')}: {field_text(field, task)}</i>")
	if error_key:
		text = f"<b>{get_text(error_key)}</b>\n\n{text}"

	if state.get("prompt"):
		core.delete_message(state["prompt"])
	sent = core.send_message(message=text, reply_markup=markup)
	if sent:
		state["prompt"] = sent.message_id
		save_state(user_id, state)
	else:
		# Nobody can see a question that was never sent, and a typed one would
		# go on taking the admin's messages for a quarter of an hour.
		forget_state(user_id)


def ask_next(user_id, state, error_key=None):
	"""Asks the first thing the new task lacks, or to confirm it once it has everything."""
	task = state["task"]
	for field in fields_in_order(task.get("action")):
		if field in task:
			continue
		if field == "host":
			# Only a prune gets here: a container brings its host. A question
			# with a single answer is not asked.
			hosts = host_registry.hosts()
			if len(hosts) == 1:
				task["host"] = hosts[0]["id"]
				continue
		state["asking"] = field
		ask(user_id, state, error_key)
		return
	state["asking"] = "confirm"
	ask(user_id, state, error_key)


def start(user_id, state):
	"""Opens a question, closing the one this user had open: one at a time."""
	previous = load_state(user_id)
	if previous:
		close(user_id, previous)
	if state["asking"]:
		ask(user_id, state)
	else:
		ask_next(user_id, state)


def changes_for(field, raw, state):
	"""What answering `raw` to `field` sets on the task, checked. Raises ScheduleError."""
	if field == "container":
		choices = state.get("choices") or []
		index = int(raw) if str(raw).isdecimal() else -1
		if not 0 <= index < len(choices):
			raise ScheduleError("error_invalid_selection", field)
		host, name = choices[index]
		return {"host": clean("host", host), "container": clean("container", name)}
	if field == "show_output":
		if raw not in SHOW_OUTPUT_CHOICES:
			raise ScheduleError("error_invalid_selection", field)
		return {"show_output": SHOW_OUTPUT_CHOICES[raw]}
	if field == "host" and host_registry.host(raw) is None:
		# Removed since the list was drawn: the task would go where it cannot run.
		raise ScheduleError("error_invalid_selection", field)
	return {field: clean(field, raw, typed=True)}


def answer(user_id, state, raw):
	"""Takes an answer to the open question: asks the next one, saves the edit, or asks again saying why not."""
	field = state["asking"]
	try:
		changes = changes_for(field, raw, state)
		if state.get("task_id") is None:
			if field == "name" and core.schedule_manager.name_taken(changes["name"]):
				raise ScheduleError("schedule_name_exists", "name")
			state["task"].update(changes)
			ask_next(user_id, state)
			return
		task = core.schedule_manager.update(state["task_id"], **changes)
	except ScheduleError as e:
		ask(user_id, state, e.key)
		return
	except OSError as e:
		close(user_id, state)
		core.send_message(message=get_text("error_adding_schedule", html.escape(str(e))))
		return
	close(user_id, state)
	if task is None:
		core.send_message(message=get_text("error_invalid_selection"))
		return
	core.send_message(message=get_text("schedule_updated_success", html.escape(task["name"])))
	send_task(task)


def confirm(user_id, state):
	"""Saves the new task the open question has been building."""
	task = state["task"]
	try:
		added = core.schedule_manager.add(task)
	except ScheduleError as e:
		# Another task took the name meanwhile: that question, again. A
		# container and its host are one answer, and go together.
		if e.field in task:
			for field in (("container", "host") if e.field in ("container", "host") else (e.field,)):
				task.pop(field, None)
			ask_next(user_id, state, e.key)
		else:
			close(user_id, state)
			core.send_message(message=get_text(e.key))
		return
	except OSError as e:
		close(user_id, state)
		core.send_message(message=get_text("error_adding_schedule", html.escape(str(e))))
		return
	close(user_id, state)
	core.send_message(message=get_text("schedule_added_success", html.escape(added["name"])))
	send_menu()


def handle_text(user_id, message):
	"""
	A typed answer to the question open for `user_id`.

	Only when that question is answered by typing. While a button is awaited
	the text was not meant for the bot, and is left alone: deleting it, as
	before, swallowed everything the admin wrote until they pressed one.
	"""
	# Looked at before taking the lock: every loose message an admin writes
	# comes through here.
	if not core.answerable(load_state(user_id), message.chat.id):
		return
	with answering(user_id):
		state = core.answerable(load_state(user_id), message.chat.id)
		if not state or state["asking"] not in TYPED:
			return
		core.delete_message(message.message_id, message.chat.id)
		answer(user_id, state, message.text.strip())


# ============================================================================
# BUTTONS
# ============================================================================
#
# The ones that answer a question keep their message: the dispatcher would
# take it away before anyone looked at who pressed it. Another admin's press
# on it, or one on an earlier question, is refused and leaves it on screen
# for whoever it belongs to; the question takes its own message away when it
# is really over.

def pressed_task(ctx):
	"""The task a button names, or None after saying it is gone."""
	task_id = int(ctx.scheduleId) if str(ctx.scheduleId).isdecimal() else None
	task = core.schedule_manager.get(task_id)
	if task is None:
		core.send_message(message=get_text("error_invalid_selection"))
	return task


def pressed_state(ctx):
	"""
	The question a press answers, or None after saying it cannot.

	Only a press on the message that asks it, by whoever it asks: a button of
	an earlier question, or of someone else's, would otherwise answer the one
	open now. One asked too long ago is over, and goes with the press.
	"""
	state = load_state(ctx.userId)
	if state is None or state.get("prompt") != ctx.messageId:
		core.send_message(message=get_text("error_invalid_selection"))
		return None
	if core.answerable(state, ctx.chatId) is None:
		close(ctx.userId, state)
		core.send_message(message=get_text("error_invalid_selection"))
		return None
	return state


@callback(name="schedAdd")
@one_at_a_time
def cb_schedAdd(ctx):
	start(ctx.userId, {"task_id": None, "task": {}, "asking": None})


@callback(name="schedPick", params=("field",))
def cb_schedPick(ctx):
	if ctx.field not in PICK_TARGETS:
		core.send_message(message=get_text("error_invalid_selection"))
		return
	send_pick_list(ctx.field)


@callback(name="schedPickPage", params=("field", "value"), keeps_message=True)
def cb_schedPickPage(ctx):
	if ctx.field in PICK_TARGETS:
		core.edit_message_reply_markup(ctx.chatId, ctx.messageId, pick_markup(ctx.field, ctx.value))


@callback(name="schedOpen", params=("scheduleId",))
def cb_schedOpen(ctx):
	task = pressed_task(ctx)
	if task:
		send_task(task)


@callback(name="schedField", params=("scheduleId", "field"))
@one_at_a_time
def cb_schedField(ctx):
	task = pressed_task(ctx)
	if not task:
		return
	if ctx.field not in editable_fields(task):
		core.send_message(message=get_text("error_invalid_selection"))
		return
	start(ctx.userId, {"task_id": task["id"], "asking": ctx.field})


@callback(name="schedSwitch", params=("scheduleId", "value"))
def cb_schedSwitch(ctx):
	task = pressed_task(ctx)
	if task and ctx.value in ("on", "off"):
		switch(task, ctx.value == "on")


@callback(name="schedRun", params=("scheduleId",))
def cb_schedRun(ctx):
	task = pressed_task(ctx)
	if not task:
		return
	scheduler.run_task(task)
	# Shown again with how it went. A command or a prune is still running:
	# its result comes when it ends, in its details next time they are opened.
	task = core.schedule_manager.get(task["id"])
	if task:
		send_task(task)


@callback(name="schedDelete", params=("scheduleId",))
def cb_schedDelete(ctx):
	task = pressed_task(ctx)
	if task:
		ask_delete(task)


@callback(name="schedDeleteYes", params=("scheduleId",))
def cb_schedDeleteYes(ctx):
	task = pressed_task(ctx)
	if task:
		delete(task["id"])


@callback(name="schedAnswer", params=("value",), keeps_message=True)
@one_at_a_time
def cb_schedAnswer(ctx):
	state = pressed_state(ctx)
	if state is None:
		return
	if state["asking"] in TYPED or state["asking"] == "confirm":
		core.send_message(message=get_text("error_invalid_selection"))
		return
	answer(ctx.userId, state, ctx.value)


@callback(name="schedPage", params=("value",), keeps_message=True)
@one_at_a_time
def cb_schedPage(ctx):
	state = pressed_state(ctx)
	if state is None or state["asking"] != "container":
		return
	state["page"] = ctx.value
	save_state(ctx.userId, state)
	core.edit_message_reply_markup(ctx.chatId, ctx.messageId, container_markup(state))


@callback(name="schedConfirm", keeps_message=True)
@one_at_a_time
def cb_schedConfirm(ctx):
	state = pressed_state(ctx)
	if state is not None and state["asking"] == "confirm":
		confirm(ctx.userId, state)


@callback(name="schedCancel", keeps_message=True)
@one_at_a_time
def cb_schedCancel(ctx):
	"""
	Cancels creating or editing a task, closing its message.

	Its own button rather than `cerrar`, which leaves the question open:
	after a cancel, the admin's next loose message was taken as the answer.
	"""
	state = pressed_state(ctx)
	if state is not None:
		close(ctx.userId, state)


# ============================================================================
# SCHEDULER
# ============================================================================

def fires(cron, minute):
	"""Whether `cron` fires in `minute`."""
	try:
		return croniter.match(cron, minute)
	except Exception as e:
		debug(f"Cannot match cron '{cron}': {e}")
		return False


class Scheduler:
	# How many minutes the loop makes up for when it falls behind —a slow
	# action, a busy machine— before it gives up on them. Past that the gap is
	# a suspended host or a clock that jumped, and replaying it would fire a
	# burst of tasks for times long gone.
	MAX_CATCH_UP_MINUTES = 5

	def __init__(self):
		# The last minute whose tasks were run. Each minute is looked at once,
		# which is what keeps a task from running twice in it.
		self._last_minute = None
		# Hosts whose tasks are being skipped because they do not answer, and
		# since when. The user hears once when it starts and once when it ends,
		# not on every skipped run.
		self._down_hosts = {}
		# The same for a container that is not there, as (task, host, name),
		# so that moving the task to another container starts afresh.
		self._missing = set()
		# Scheduled prunes, run by a worker of their own in the order they came
		# due. Started on the first one: most installations never have any.
		self._prunes = queue.Queue()
		self._prune_lock = threading.Lock()
		self._prune_worker = None
		self._thread = None
		# For what is noted about hosts and containers: Run now runs a task
		# from a button's thread, beside this one's.
		self._lock = threading.Lock()

	def start(self):
		"""Starts the scheduler's thread, once: two would run every task twice."""
		if self._thread is None:
			self._thread = threading.Thread(target=self.run, name="scheduler", daemon=True)
			self._thread.start()

	def run(self):
		"""
		Runs the @reboot tasks, then every minute the tasks due in it.

		It wakes at the start of each minute and makes up for any it missed.
		Sleeping 60 s after doing the work pushed every lap later, until one
		jumped from :00:55 to :02:05 and the tasks due at :01 never ran.

		The @reboot tasks run here and not on the main thread, where an exec
		that hangs held up the start of polling with it.
		"""
		self._run_reboot_tasks()
		while True:
			try:
				self._tick(datetime.now())
			except Exception as e:
				error(f"Error running the schedules: [{e}]")
			time.sleep(60.5 - time.time() % 60)

	def _run_reboot_tasks(self):
		try:
			for task in core.schedule_manager.all():
				if task["enabled"] and task["cron"] == "@reboot":
					self.run_task(task)
		except Exception as e:
			error(f"Error running the @reboot schedules: [{e}]")

	def _tick(self, now):
		"""Runs every task due in the minutes not yet looked at."""
		try:
			self._check_down_hosts()
		except Exception as e:
			debug(f"Could not check the hosts whose schedules were skipped: {e}")
		due = self._minutes_due(now)
		if not due:
			return
		tasks = core.schedule_manager.all()
		with self._lock:
			# Forgotten once the task is deleted or points elsewhere.
			self._missing &= {(task["id"], task["host"], task["container"]) for task in tasks}
		for task in tasks:
			# Once even if it matches more than one of the minutes made up for:
			# catching up is about not losing a run, not about bunching them.
			if task["enabled"] and task["cron"] != "@reboot" and any(fires(task["cron"], minute) for minute in due):
				self.run_task(task)

	def _minutes_due(self, now):
		"""The minutes to look at now, oldest first, and marks them as done."""
		current = now.replace(second=0, microsecond=0)
		last = self._last_minute
		# The gap in real time, which a change of the clock's hour does not
		# move. In spring the wall clock goes from 01:59 to 03:00 in a minute:
		# taken for an hour the bot was not running, the tasks of 02:xx were
		# skipped that day. That is a gap of a minute, and those minutes are
		# replayed —each task once, like any catch-up— as cron does.
		long_gap = last is not None and current - last > timedelta(minutes=self.MAX_CATCH_UP_MINUTES) \
			and current.timestamp() - last.timestamp() > self.MAX_CATCH_UP_MINUTES * 60
		if last is None or long_gap or last - current > timedelta(hours=2):
			# First lap, a gap too long to replay, or the clock set well back:
			# start over from here.
			if last is not None and current > last:
				warning(f"Schedules: skipping {int((current - last).total_seconds() // 60) - 1} "
						f"minute(s) the bot was not running for")
			due = [current]
		elif current <= last:
			# The same minute again, or the clock went back a little —the
			# autumn change—: those minutes were already looked at.
			due = []
		else:
			due = []
			minute = last + timedelta(minutes=1)
			while minute <= current:
				due.append(minute)
				minute += timedelta(minutes=1)
		if due:
			self._last_minute = due[-1]
		return due

	def run_task(self, task):
		"""
		Runs one task now, notes how it went for its details, and returns that:
		"ok", "skipped", "failed", or "running" for a command or a prune, whose
		end is noted when it comes.

		Only a host removed from the bot switches a task off: it is not coming
		back, and the task has to be moved. A paused host, one that does not
		answer and a container that is not there are skipped, the last two
		saying so once: a container being recreated, or a machine rebooting,
		used to cost the task for good.
		"""
		outcome, reason, *args = self._attempt(task)
		if outcome != "running":
			record_run(task["id"], outcome, reason, *args)
		return outcome

	def _attempt(self, task):
		action = task["action"]
		name = html.escape(task["name"])
		try:
			if action == "mute":
				core.count_usage("sched_mute")
				core.mute(task["minutes"], extend_only=True)
				return "ok", None
			host = task["host"]
			if host_registry.host(host) is None:
				if store.settings_unreadable():
					# Then the host list is only the defaults, and every task's
					# host looks gone: the task waits for the file to be fixed.
					warning(f"Schedule {task['name']} skipped: settings.json is unreadable")
					return "skipped", "schedule_reason_error"
				error(f"Host {host} of schedule {task['name']} no longer exists")
				core.schedule_manager.update(task["id"], enabled=False)
				# Said even while muted, unlike the scheduler's other notices:
				# the task stays off until someone moves it.
				core.send_message_to_notification_channel(message=get_text("schedule_disabled_host_missing", name))
				return "failed", "schedule_reason_host_missing"
			if host_registry.is_paused(host):
				debug(f"Schedule {task['name']} skipped: host {host_registry.alias(host)} is paused")
				return "skipped", "schedule_reason_host_paused"
			if host in self._down_hosts:
				# Asked once a minute whether it is back, in _check_down_hosts.
				# Asked by every task too, each one waited out the timeout in
				# turn and held up every task behind it.
				debug(f"Schedule {task['name']} skipped: host {host_registry.alias(host)} is not answering")
				return "skipped", "schedule_reason_host_down"
			core.count_usage(f"sched_{action}")
			if action == "prune":
				# Handed to its own worker: a prune can take minutes, and the
				# tasks of the minutes after it would wait, or be skipped. Noted
				# as running first, so its end cannot be overwritten by it.
				owner = core.manager(host)
				record_run(task["id"], "running")
				self._queue_prune(owner, task)
				return "running", None
			reference = self._container(task)
			if reference is None:
				return "skipped", "schedule_reason_container_missing"
			if action == "exec":
				# Not waited for: a long or endless command would hold up every
				# task after it. Its output, if asked for, and its result arrive
				# when it ends.
				record_run(task["id"], "running")
				core.execute_command(reference, task["container"], task["command"], task["show_output"], wait=False,
									on_result=lambda result: record_exec(task["id"], result))
				return "running", None
			# run, stop and restart are the core's functions of those names,
			# which answer None when it went well.
			failure = getattr(core, action)(reference, task["container"], from_schedule=True)
			return ("failed", "schedule_reason_error") if failure else ("ok", None)
		except host_registry.HostUnavailable as e:
			# Skipped for this run only, and said once per outage: a task every
			# minute against a machine down for an hour would be sixty messages.
			warning(f"Schedule {task['name']} skipped: {e}")
			with self._lock:
				first = e.host_id not in self._down_hosts
				if first:
					self._down_hosts[e.host_id] = time.monotonic()
			if first:
				self._notify(get_text("schedule_skipped_host_down", name, core.host_alias(e.host_id)))
			return "skipped", "schedule_reason_host_down"
		except Exception as e:
			error(f"Error running schedule {task['name']} [{action}]: [{e}]")
			return "failed", "schedule_reason_error"

	def _container(self, task):
		"""
		The task's container, looked for on its own host and nowhere else:
		acting on a namesake on another machine would be silent and, for stop
		or exec, destructive. None when it is not there.

		Raises HostUnavailable when the host cannot be asked, which says
		nothing about the container.
		"""
		host, container_name = task["host"], task["container"]
		owner = core.manager(host)
		try:
			container = owner.container_named(container_name)
		except Exception as e:
			raise host_registry.HostUnavailable(host, str(e))
		key = (task["id"], host, container_name)
		texts = (html.escape(task["name"]), html.escape(container_name))
		missing = container is None
		# Under the lock only what is noted, so Run now and the scheduler cannot
		# both say it: what is asked of Docker above can take its timeout.
		with self._lock:
			changed = missing != (key in self._missing)
			if changed:
				(self._missing.add if missing else self._missing.discard)(key)
		if changed:
			notice = "schedule_skipped_container_missing" if missing else "schedule_container_back"
			self._notify(core.host_label(host) + get_text(notice, *texts))
		if missing:
			warning(f"Schedule {task['name']} skipped: container {container_name} is not on {host}")
			return None
		return core.container_ref(host, container)

	def _queue_prune(self, owner, task):
		"""Leaves a scheduled prune to the prune worker, starting it if needed."""
		self._prunes.put((owner, task))
		with self._prune_lock:
			if self._prune_worker is None or not self._prune_worker.is_alive():
				self._prune_worker = threading.Thread(target=self._prune_loop, name="scheduled-prunes", daemon=True)
				self._prune_worker.start()

	def _prune_loop(self):
		"""
		Runs the queued prunes one after another.

		One at a time and in order: a containers prune due with an images one
		frees images the second can then remove, and two at once on the same
		host only get Docker's "already running".
		"""
		while True:
			owner, task = self._prunes.get()
			try:
				self._run_prune(owner, task)
			except Exception as e:
				error(f"Error running scheduled prune {task['name']}: [{e}]")
				record_run(task["id"], "failed", "schedule_reason_error")

	def _run_prune(self, owner, task):
		prune_type, show_output = task["prune_type"], task["show_output"]
		message, details, cleaned = getattr(owner, f"prune_{prune_type}")()
		message = core.host_label(owner.host_id) + message
		failed = details is None
		record_run(task["id"], *(("failed", "schedule_reason_error") if failed else ("ok",)))
		# Only failures or what was removed: a prune with nothing to remove is
		# the usual case, and not news.
		if not show_output or (show_output == SHOW_OUTPUT_FAILURES and not failed and not cleaned):
			debug(f"Scheduled prune of {prune_type}: {message}")
			return
		if failed:
			core.send_message(message=message)
			return
		# The same as a /prune: the message, with what went in a file.
		loading = core.send_message(message=get_text("loading_file"))
		core.send_document(document=core.get_temporal_file(details, get_text(f"button_{prune_type}")),
							reply_markup=core.create_simple_keyboard("button_delete"), caption=message)
		if loading:
			core.delete_message(loading.message_id)

	def _notify(self, message):
		"""One of the scheduler's own notices, kept quiet while muted."""
		if core.is_muted():
			debug(f"Message [{message}] omitted because muted")
			return
		core.send_message_to_notification_channel(message=message)

	def _check_down_hosts(self):
		"""
		Says when a host whose tasks were being skipped answers again.

		Asked every minute rather than left for its next task to find out: a
		daily task would otherwise announce the host back up to a day late.
		Only the hosts that are down are asked, so a healthy fleet costs
		nothing here.

		A remote host down long enough for HostAvailability to announce it has
		its return announced there too, and saying it here as well would be
		the same news twice. Any other —the local one, which that does not
		watch, or one back before it was announced— is said here: the task's
		notice promised it would be.
		"""
		with self._lock:
			down = dict(self._down_hosts)
		if not down:
			return
		# Asked without the lock: a host that does not answer takes its timeout,
		# and Run now would wait it out.
		entries = {host_id: host_registry.host(host_id) for host_id in down}
		statuses = host_registry.status_snapshot(entries=[entry for entry in entries.values() if entry])
		with self._lock:
			for host_id, entry in entries.items():
				if entry is None:
					# Removed meanwhile: nothing will come back.
					self._down_hosts.pop(host_id, None)
			back = {host_id: self._down_hosts.pop(host_id) for host_id, (ok, _reason) in statuses.items()
					if ok and host_id in self._down_hosts}
		for host_id, since in back.items():
			announced_there = (core.HostAvailability.watched(entries[host_id])
								and time.monotonic() - since >= core.HostAvailability.DOWN_AFTER_SECONDS)
			if not announced_there:
				self._notify(get_text("schedule_host_back", core.host_alias(host_id)))


scheduler = Scheduler()
