"""
Which of a container's events are announced: all of them, only problems, or
none, from the DCB-Notifications label or from /info and /settings.

It came back after being taken out in 2.5.1. 2.3.0 read its label by listing
every container, with an inspect of each, and then inspecting the one the
event was about, on every event: on a slow server that was enough CPU to drop
the feature. So half of this file is about it working and the other half
about it costing nothing: no request to the daemon and no file read for an
event, and a stream that only carries the events the monitor reads.
"""

import builtins
import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

import callback_registry

dcb, store, _root = harness.load_bot()
import callbacks  # noqa: E402  (registers on import, after the bot)

LOCAL = [{"id": "h_local", "alias": "casa", "url": "unix:///var/run/docker.sock", "local": True}]


def _event(action, name="app", **attributes):
	"""As Docker 29 sends them: the container's labels among the attributes."""
	return {"Type": "container", "Action": action, "id": "f" * 64, "timeNano": time.time_ns(),
			"Actor": {"ID": "f" * 64, "Attributes": dict(attributes, name=name)}}


class _Clock:
	"""Timers that fire when told to, for the stops held waiting for an oom."""

	def __init__(self):
		self.now = 0
		self.timers = []

	def schedule(self, delay, fn):
		timer = MagicMock()
		timer.cancelled = False
		timer.cancel = lambda: setattr(timer, "cancelled", True)
		self.timers.append((self.now + delay, fn, timer))
		return timer

	def advance(self, seconds):
		self.now += seconds
		due = [t for t in self.timers if t[0] <= self.now]
		self.timers = [t for t in self.timers if t[0] > self.now]
		for _when, fn, timer in due:
			if not timer.cancelled:
				fn()


def _monitor():
	"""
	A monitor whose client records every request, and whose announcements are
	collected. Logs and health output come from the client, as in production,
	so asking for them counts as a request.
	"""
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	client = MagicMock()
	client.containers.get.return_value.logs.return_value = b"ERROR\n"
	client.containers.get.return_value.attrs = {"State": {"Health": {"Log": [{"Output": "down"}]}}}
	monitor = dcb.DockerEventMonitor("h_local")
	monitor._schedule = _Clock().schedule
	announced = []
	monitor._announce = lambda message, detail=None: announced.append(message)
	original = dcb.host_registry.client
	dcb.host_registry.client = lambda host_id: client
	return monitor, announced, client, lambda: setattr(dcb.host_registry, "client", original)


def _level(name, level):
	assert store.set_container_notification_level("h_local", name, level)


def _cleanup():
	store.forget_host_containers("h_local")


# --- What each level lets through -------------------------------------------

def test_without_a_setting_everything_is_announced_as_before():
	monitor, announced, _client, restore = _monitor()
	try:
		monitor._handle_event(_event("start"))
		monitor._handle_event(_event("kill"))
		monitor._handle_event(_event("die", exitCode="0"))
		assert announced == [dcb.get_text("started_container", "app"),
							 dcb.get_text("stopped_container", "app")], announced
	finally:
		restore()


def test_none_announces_nothing_at_all():
	monitor, announced, _client, restore = _monitor()
	_level("app", "none")
	try:
		for event in (_event("start"), _event("die", exitCode="1"), _event("oom"), _event("die", exitCode="137"),
					  _event("health_status: unhealthy"), _event("health_status: healthy")):
			monitor._handle_event(event)
		assert announced == [], announced
	finally:
		restore()
		_cleanup()


def test_problems_leaves_out_what_was_expected():
	"""A backup stopping and starting it again says nothing."""
	monitor, announced, _client, restore = _monitor()
	_level("app", "problems")
	store.set("bot.extended_messages", True)
	try:
		monitor._handle_event(_event("create"))
		monitor._handle_event(_event("start"))
		# docker stop: a kill, then the stop, with whatever code the signal left.
		monitor._handle_event(_event("kill", signal="15"))
		monitor._handle_event(_event("die", exitCode="143"))
		# A process that finished by itself, fine.
		monitor._handle_event(_event("die", exitCode="0"))
		assert announced == [], announced
	finally:
		store.set("bot.extended_messages", False)
		restore()
		_cleanup()


def test_problems_still_tells_failures_memory_and_health():
	monitor, announced, _client, restore = _monitor()
	_level("app", "problems")
	try:
		monitor._handle_event(_event("die", exitCode="1"))
		assert announced[-1] == dcb.get_text("container_failed", "app", dcb.describe_exit_code("1")), announced
		monitor._handle_event(_event("oom"))
		monitor._handle_event(_event("die", exitCode="137"))
		assert announced[-1] == dcb.get_text("container_oom", "app"), announced
		monitor._handle_event(_event("health_status: unhealthy"))
		assert announced[-1] == dcb.get_text("container_unhealthy", "app"), announced
		monitor._handle_event(_event("health_status: healthy"))
		assert announced[-1] == dcb.get_text("container_healthy_again", "app"), announced
		assert len(announced) == 4, announced
	finally:
		restore()
		_cleanup()


def test_problems_still_tells_a_restart_loop():
	"""The tracker sees every stop and start, the quiet ones too, or it never finds the loop."""
	monitor, announced, _client, restore = _monitor()
	_level("app", "problems")
	try:
		for _ in range(dcb.RestartLoopTracker.THRESHOLD):
			monitor._handle_event(_event("die", exitCode="1"))
			monitor._handle_event(_event("start"))
		assert dcb.get_text("restart_loop", "app") in announced, announced
		assert dcb.get_text("started_container", "app") not in announced, announced
	finally:
		restore()
		_cleanup()


def test_the_label_in_the_event_decides_and_wins_over_the_setting():
	monitor, announced, _client, restore = _monitor()
	_level("app", "none")
	try:
		monitor._handle_event(_event("start", **{dcb.LABEL_NOTIFICATIONS: "problems"}))
		monitor._handle_event(_event("die", exitCode="2", **{dcb.LABEL_NOTIFICATIONS: "problems"}))
		assert announced == [dcb.get_text("container_failed", "app", dcb.describe_exit_code("2"))], announced
		# A value it does not know is everything, and still the label's.
		announced.clear()
		monitor._handle_event(_event("start", **{dcb.LABEL_NOTIFICATIONS: "loud"}))
		assert announced == [dcb.get_text("started_container", "app")], announced
		assert dcb.notification_level("h_local", "app", {dcb.LABEL_NOTIFICATIONS: " NONE "}) == ("none", dcb.LABEL_NOTIFICATIONS)
	finally:
		restore()
		_cleanup()


def test_the_level_is_per_container_and_per_host():
	_level("app", "none")
	try:
		assert dcb.notification_level("h_local", "app", {}) == ("none", None)
		assert dcb.notification_level("h_local", "db", {}) == (None, None)
		assert dcb.notification_level("h_nas", "app", {}) == (None, None)
	finally:
		_cleanup()


def test_the_setting_lives_next_to_the_update_mode_and_leaves_when_back_to_default():
	try:
		assert store.set_container_update_mode("h_local", "app", "auto")
		_level("app", "problems")
		assert store.container_settings()["h_local"]["app"] == {"updates": "auto", "notifications": "problems"}
		_level("app", None)
		assert store.container_settings()["h_local"]["app"] == {"updates": "auto"}
		assert store.set_container_update_mode("h_local", "app", None)
		assert "h_local" not in store.container_settings()
		# Only the two levels are stored; anything else is the default.
		_level("app", "whatever")
		assert "h_local" not in store.container_settings()
	finally:
		_cleanup()


def test_a_manual_stop_is_confirmed_when_the_monitor_will_not_say_it():
	"""Otherwise a /stop of a quiet container answers nothing."""
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	sent = []
	original = dcb.send_message
	dcb.send_message = lambda message="", **kw: sent.append(message)
	manager = dcb.manager("h_local")
	container = MagicMock()
	container.labels = {}
	manager.client = MagicMock()
	manager.client.containers.get.return_value = container
	original_own = dcb.is_own_container
	dcb.is_own_container = lambda *a: False
	try:
		manager.start_container("abc12", "app")
		assert sent == [], sent
		_level("app", "problems")
		manager.start_container("abc12", "app")
		assert sent == [dcb.get_text("started_container", "app")], sent
		sent.clear()
		_cleanup()
		container.labels = {dcb.LABEL_NOTIFICATIONS: "none"}
		manager.stop_container("abc12", "app")
		assert sent == [dcb.get_text("stopped_container", "app")], sent
		# A scheduled one never was, and is not.
		sent.clear()
		manager.restart_container("abc12", "app", from_schedule=True)
		assert sent == [], sent
	finally:
		dcb.send_message = original
		dcb.is_own_container = original_own
		_cleanup()


# --- /info and /settings ----------------------------------------------------

def test_info_goes_round_the_three_levels_and_refuses_under_a_label():
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	app = MagicMock()
	app.name, app.status, app.labels = "app", "running", {}
	app.attrs = {"Config": {"Image": "app:latest"}}
	owner = dcb.manager("h_local")
	ref = dcb.make_ref("h_local", "abc12")
	repainted, answered = [], []
	originals = (dcb.find_container, dcb.edit_message_reply_markup_sync, dcb.answer_callback_quietly)
	dcb.find_container = lambda reference: (owner, app)
	dcb.edit_message_reply_markup_sync = lambda chat_id, message_id, reply_markup=None: repainted.append(reply_markup)
	dcb.answer_callback_quietly = lambda call_id, text=None, show_alert=False: answered.append((text, show_alert))

	def tap(value=None):
		if value is None:
			buttons = harness.keyboard_callbacks(repainted[-1] if repainted else dcb.live_info_markup(ref))
			value = next(b for b in buttons if b.startswith("infoSetNotifications|")).split("|")[1]
		callbacks.cb_infoSetNotifications(callback_registry.Context(
			call=MagicMock(id="q"), chatId=1, messageId=2, containerId=ref, containerName="app", value=value))

	try:
		seen = []
		for _ in range(3):
			tap()
			seen.append(store.container_notification_level("h_local", "app"))
		assert seen == ["problems", "none", None], seen
		labels = harness.keyboard_labels(repainted[-1])
		assert any(label.startswith("🤖") and dcb.get_text("button_notifications_all") in label for label in labels), labels

		app.labels = {dcb.LABEL_NOTIFICATIONS: "problems"}
		tap("q")
		assert answered[-1][1] is True and store.container_notification_level("h_local", "app") is None, answered
		labels = harness.keyboard_labels(repainted[-1])
		assert any(label.startswith("🏷️") and dcb.get_text("button_notifications_problems") in label
				   for label in labels), labels
	finally:
		dcb.find_container, dcb.edit_message_reply_markup_sync, dcb.answer_callback_quietly = originals
		_cleanup()


def test_the_settings_list_of_notifications_sets_levels_and_goes_back_to_settings():
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	app = MagicMock()
	app.name, app.labels = "app", {}
	app.attrs = {"Config": {"Image": "app:latest"}}
	labelled = MagicMock()
	labelled.name, labelled.labels = "db", {dcb.LABEL_NOTIFICATIONS: "none", "other": "x"}
	labelled.attrs = {"Config": {"Image": "db:latest"}}
	data = {"host": "h_local", "kind": "notifications", "page": 0, "several_hosts": False,
			"answered": None, "items": dcb.container_list_items([app, labelled])}
	assert data["items"][1][1] == {dcb.LABEL_NOTIFICATIONS: "none"}
	text, markup = dcb.build_settings_containers_list(data)
	assert dcb.get_text("settings_notifications_title") in text
	assert harness.keyboard_callbacks(markup)[:2] == ["ctrToggle|h_local|0|p", "ctrToggle|h_local|1|e"]
	assert harness.keyboard_labels(markup)[:2] == ["🔔 app", "🏷️🔕 db"]
	assert "settings" in harness.keyboard_callbacks(markup)
	assert dcb.container_list_codes(data) is dcb.NOTIFICATION_LEVEL_CODES

	original = dcb.manager
	live = {"app": app, "db": labelled}
	dcb.manager = lambda host_id: MagicMock(container_named=lambda name: live[name])
	try:
		assert dcb.toggle_in_container_list(data, 0, "problems") is None
		assert store.container_notification_level("h_local", "app") == "problems"
		assert dcb.toggle_in_container_list(data, 1, None) == dcb.get_text("container_updates_by_label", dcb.LABEL_NOTIFICATIONS)
		# The update list is untouched by it.
		assert store.container_update_mode("h_local", "app") is None
		data["kind"] = "updates"
		assert harness.keyboard_callbacks(dcb.build_settings_containers_list(data)[1])[0] == "ctrToggle|h_local|0|a"
	finally:
		dcb.manager = original
		_cleanup()


def test_a_host_picker_carries_the_kind_of_list():
	entry = {"id": "h_nas", "alias": "nas"}
	_text, markup = dcb.build_settings_containers_hosts([(entry, None, [MagicMock()])], ["h_nas"], "notifications")
	assert harness.keyboard_callbacks(markup)[0] == "ctrHost|notifications|h_nas"
	_text, markup = dcb.build_settings_containers_hosts([(entry, None, [MagicMock()])], ["h_nas"])
	assert harness.keyboard_callbacks(markup)[0] == "ctrHost|updates|h_nas"


def test_settings_has_its_row():
	_text, markup = dcb.build_settings()
	assert "settingsNotifications" in harness.keyboard_callbacks(markup)


def test_a_label_added_later_takes_the_setting_away():
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	_level("app", "problems")
	app = MagicMock()
	app.name, app.labels = "app", {dcb.LABEL_NOTIFICATIONS: "none"}
	try:
		dcb.forget_gone_container_settings([(LOCAL[0], None, [app])])
		assert store.container_notification_level("h_local", "app") is None
	finally:
		_cleanup()


# --- What it costs ----------------------------------------------------------
#
# What made 2.3.0 slow was asking the daemon on every event. These hold the
# monitor to asking it nothing for an event that is not news, at any level,
# and to reading the settings from memory.

# A day of an ordinary container: created, started, stopped on purpose,
# finished, healthchecks passing, removed. None of it is a failure, so none
# of it may need anything from the daemon.
ROUTINE = ("create", "start", "health_status: healthy", "kill", "die", "start", "die", "destroy")


def _routine(name, **labels):
	codes = {"die": "0"}
	return [_event(action, name=name, **dict(labels, **({"exitCode": codes[action]} if action in codes else {}))
				   ) for action in ROUTINE]


def test_an_event_that_is_not_a_failure_asks_the_daemon_nothing_at_any_level():
	monitor, _announced, client, restore = _monitor()
	store.set("bot.extended_messages", True)
	try:
		_level("set_problems", "problems")
		_level("set_none", "none")
		for name, labels in (("plain", {}), ("set_problems", {}), ("set_none", {}),
							 ("label_problems", {dcb.LABEL_NOTIFICATIONS: "problems"}),
							 ("label_none", {dcb.LABEL_NOTIFICATIONS: "none"})):
			for event in _routine(name, **labels):
				monitor._handle_event(event)
		assert client.mock_calls == [], client.mock_calls
	finally:
		store.set("bot.extended_messages", False)
		restore()
		_cleanup()


def test_a_quiet_container_asks_nothing_even_when_it_fails():
	"""With none, not even the logs or the health output a failure would show."""
	monitor, announced, client, restore = _monitor()
	_level("app", "none")
	try:
		for action, attributes in (("die", {"exitCode": "1"}), ("health_status: unhealthy", {}),
								   ("oom", {}), ("die", {"exitCode": "137"})):
			monitor._handle_event(_event(action, **attributes))
		assert client.mock_calls == [] and announced == [], (client.mock_calls, announced)
	finally:
		restore()
		_cleanup()


def test_the_level_is_read_from_memory_not_from_disk():
	"""Every event reads it: a file opened per event would be the 2.3.0 cost in another place."""
	monitor, _announced, _client, restore = _monitor()
	_level("app", "problems")
	opened = []
	original_open = builtins.open
	original_flush = store._flush
	builtins.open = lambda *a, **kw: opened.append(a) or original_open(*a, **kw)
	store._flush = lambda *a, **kw: opened.append(("flush",) + a)
	try:
		for event in _routine("app") * 50:
			monitor._handle_event(event)
	finally:
		builtins.open = original_open
		store._flush = original_flush
		restore()
		_cleanup()
	assert opened == [], opened[:5]


def test_the_stream_only_asks_for_what_the_monitor_reads():
	"""
	Each healthcheck run is three exec events, and image, network and volume
	events are of no use: the daemon filters them out instead of the bot
	receiving and decoding them only to drop them.
	"""
	monitor, _announced, client, restore = _monitor()
	client.events.return_value = iter([])
	try:
		monitor.detectar_eventos_contenedores()
		filters = client.events.call_args.kwargs["filters"]
		assert filters == {"type": ["container"], "event": list(dcb.MONITORED_EVENTS)}, filters
		# A reconnection asks for the same.
		monitor._last_event_ns = time.time_ns()
		client.events.return_value = iter([])
		monitor.detectar_eventos_contenedores()
		assert client.events.call_args.kwargs["filters"] == filters
	finally:
		restore()


def test_every_action_the_monitor_reads_is_in_the_filter():
	"""
	An action the monitor handles but the filter leaves out would never
	arrive, and nothing would say so. Each one is fed in and must do
	something; anything outside the filter must do nothing.
	"""
	monitor, announced, _client, restore = _monitor()
	store.set("bot.extended_messages", True)
	try:
		handled = set()
		for action in ("create", "start", "kill", "die", "oom", "destroy", "health_status: unhealthy",
					   "exec_create", "exec_start", "exec_die", "attach", "pause", "unpause", "rename",
					   "update", "resize", "top", "commit", "copy", "export", "restart", "stop"):
			name = f"c_{action.split(':')[0]}"
			if action == "destroy":
				# What a destroy does is forget it, so there has to be something.
				monitor._unhealthy.add(name)
			before = (len(announced), dict(monitor._oom), set(monitor._unhealthy), dict(monitor.restart_loops._killed))
			monitor._handle_event(_event(action, name=name, exitCode="1"))
			after = (len(announced), dict(monitor._oom), set(monitor._unhealthy), dict(monitor.restart_loops._killed))
			if before != after:
				handled.add(action.split(":")[0])
		assert handled == set(dcb.MONITORED_EVENTS), handled ^ set(dcb.MONITORED_EVENTS)
	finally:
		store.set("bot.extended_messages", False)
		restore()


def test_deleting_says_its_notification_setting_goes_too():
	store.set("hosts", LOCAL)
	dcb.host_registry.reset()
	sent = []
	original = dcb.send_message
	dcb.send_message = lambda message="", **kw: sent.append(message)
	try:
		dcb.confirm_delete(dcb.make_ref("h_local", "abc12"), "app")
		assert dcb.get_text("confirm_delete_settings") not in sent[-1]
		_level("app", "problems")
		dcb.confirm_delete(dcb.make_ref("h_local", "abc12"), "app")
		assert dcb.get_text("confirm_delete_settings") in sent[-1]
	finally:
		dcb.send_message = original
		_cleanup()


def test_a_tap_meant_for_one_kind_of_list_never_lands_on_the_other():
	"""Both lists are drawn in the same message, one replacing the other."""
	assert not set(dcb.UPDATE_MODE_CODES) & set(dcb.NOTIFICATION_LEVEL_CODES)
