"""
The background monitors.

What matters here is what happens when a host misbehaves: one machine being
unreachable must not silence the others, and a host that comes back must be
picked up without restarting the bot.
"""

import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

import host_registry

dcb, store, _root = harness.load_bot()

TWO_HOSTS = [
	{"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
	{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
]
ONE_HOST = [TWO_HOSTS[0]]


def test_a_single_host_is_never_named_in_a_message():
	"""
	With one host the bot has to read exactly as it did before hosts existed,
	so the label is empty rather than saying "local" on every notification.
	"""
	store.set("hosts", ONE_HOST)
	assert dcb.host_label("h_local") == ""

	store.set("hosts", TWO_HOSTS)
	assert "casa" in dcb.host_label("h_local")
	assert "nas" in dcb.host_label("h_nas")
	store.set("hosts", ONE_HOST)


def test_an_event_notification_names_its_host_at_the_end():
	"""
	These are the messages the user sees most: the bot telling them a
	container went down. They read as one statement, so the machine goes where
	a person would put it — at the end — and not in front like a log line.
	"""
	import i18n

	store.set("hosts", TWO_HOSTS)
	dcb.host_registry.reset()
	sent = []
	original = dcb.send_message_to_notification_channel
	dcb.send_message_to_notification_channel = lambda message="", **kw: sent.append(message)
	monitor = dcb.DockerEventMonitor("h_nas")
	events = [{"Type": "container", "Action": "die",
				"Actor": {"Attributes": {"name": "plex"}}}]

	class Stream:
		def events(self, decode=True):
			return iter(events)

	original_client = dcb.host_registry.client
    # noqa
	dcb.host_registry.client = lambda host_id: Stream()
	try:
		monitor.detectar_eventos_contenedores()
		assert len(sent) == 1, sent
		message = sent[0]
		assert "plex" in message and "nas" in message, message
		assert message.startswith(i18n.get_text("stopped_container", "plex")), message
		assert message.endswith(dcb.host_suffix("h_nas")), message
	finally:
		dcb.send_message_to_notification_channel = original
		dcb.host_registry.client = original_client
		store.set("hosts", ONE_HOST)
		dcb.host_registry.reset()


def test_a_container_in_an_update_batch_is_not_announced():
	"""
	An update batch reports its outcome in one summary, so the stop and start
	of what it is recreating are held back. Only those: another container
	going down meanwhile, or the same name on another host, still is.
	"""
	store.set("hosts", TWO_HOSTS)
	dcb.host_registry.reset()
	sent = []
	original = dcb.send_message_to_notification_channel
	dcb.send_message_to_notification_channel = lambda message="", **kw: sent.append(message)
	events = [{"Type": "container", "Action": action, "Actor": {"Attributes": {"name": name}}}
				for action, name in (("die", "plex"), ("start", "plex"), ("die", "sonarr"))]

	class Stream:
		def events(self, decode=True):
			return iter(events)

	original_client = dcb.host_registry.client
	dcb.host_registry.client = lambda host_id: Stream()
	dcb.hold_container_events("h_nas", ["plex"])
	try:
		dcb.DockerEventMonitor("h_nas").detectar_eventos_contenedores()
		assert len(sent) == 1 and "sonarr" in sent[0], sent

		sent.clear()
		dcb.DockerEventMonitor("h_local").detectar_eventos_contenedores()
		assert len(sent) == 3, "el mismo nombre en otro host se ha callado"
	finally:
		dcb.release_container_events("h_nas", ["plex"])
		dcb._held_events.clear()
		dcb.send_message_to_notification_channel = original
		dcb.host_registry.client = original_client
		store.set("hosts", ONE_HOST)
		dcb.host_registry.reset()


def test_the_supervisor_runs_one_monitor_per_host():
	store.set("hosts", TWO_HOSTS)
	started = []
	original = dcb.DockerEventMonitor.demonio_event
	dcb.DockerEventMonitor.demonio_event = lambda self: started.append(self.host_id)
	try:
		supervisor = dcb.EventMonitorSupervisor()
		supervisor.reconcile()
		assert sorted(started) == ["h_local", "h_nas"]
		assert sorted(supervisor._monitors) == ["h_local", "h_nas"]

		# Reconciling again must not start a second stream for the same host.
		started.clear()
		supervisor.reconcile()
		assert started == []
	finally:
		dcb.DockerEventMonitor.demonio_event = original
		store.set("hosts", ONE_HOST)


def test_adding_and_removing_a_host_starts_and_stops_its_monitor():
	"""
	Hosts can be added from /settings while the bot runs, so something has to
	notice without a restart.
	"""
	store.set("hosts", ONE_HOST)
	stopped = []
	original_start = dcb.DockerEventMonitor.demonio_event
	original_stop = dcb.DockerEventMonitor.stop
	dcb.DockerEventMonitor.demonio_event = lambda self: None
	dcb.DockerEventMonitor.stop = lambda self: stopped.append(self.host_id)
	try:
		supervisor = dcb.EventMonitorSupervisor()
		supervisor.reconcile()
		assert list(supervisor._monitors) == ["h_local"]

		store.set("hosts", TWO_HOSTS)
		supervisor.reconcile()
		assert sorted(supervisor._monitors) == ["h_local", "h_nas"]

		store.set("hosts", ONE_HOST)
		supervisor.reconcile()
		assert list(supervisor._monitors) == ["h_local"]
		assert stopped == ["h_nas"]
	finally:
		dcb.DockerEventMonitor.demonio_event = original_start
		dcb.DockerEventMonitor.stop = original_stop
		store.set("hosts", ONE_HOST)


def test_pausing_a_host_stops_its_event_monitor():
	"""
	A paused host that kept its event stream open would still be reconnecting
	to a machine nobody is asking about, and would still be announcing its
	containers starting and stopping. The supervisor reads the hosts in use, so
	the pause reaches it without it knowing what a pause is.
	"""
	import copy

	# Deep-copied: store.set keeps the object it is given, and pausing writes
	# into it — the shared fixture would come out of here with a paused host.
	store.set("hosts", copy.deepcopy(TWO_HOSTS))
	stopped = []
	original_start = dcb.DockerEventMonitor.demonio_event
	original_stop = dcb.DockerEventMonitor.stop
	dcb.DockerEventMonitor.demonio_event = lambda self: None
	dcb.DockerEventMonitor.stop = lambda self: stopped.append(self.host_id)
	try:
		supervisor = dcb.EventMonitorSupervisor()
		supervisor.reconcile()
		assert sorted(supervisor._monitors) == ["h_local", "h_nas"]

		assert host_registry.set_paused("h_nas", True) is True
		supervisor.reconcile()
		assert list(supervisor._monitors) == ["h_local"]
		assert stopped == ["h_nas"]

		# And it comes back on its own when the host is resumed.
		assert host_registry.set_paused("h_nas", False) is True
		supervisor.reconcile()
		assert sorted(supervisor._monitors) == ["h_local", "h_nas"]
	finally:
		dcb.DockerEventMonitor.demonio_event = original_start
		dcb.DockerEventMonitor.stop = original_stop
		store.set("hosts", ONE_HOST)


def test_the_stream_keeps_being_retried():
	"""
	4.x gave up after five failures. On a remote host that is briefly
	unreachable that would leave its events silent until the bot restarts.
	"""
	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	monitor.MAX_BACKOFF_SECONDS = 0.01
	attempts = []

	def failing():
		attempts.append(1)
		if len(attempts) >= 8:
			monitor.stop()
		raise Exception("stream broke")

	monitor.detectar_eventos_contenedores = failing
	thread = threading.Thread(target=monitor._event_loop_with_retry, daemon=True)
	thread.start()
	thread.join(timeout=10)

	assert not thread.is_alive(), "el bucle no terminó"
	assert len(attempts) >= 8, f"se rindió tras {len(attempts)} intentos"


def test_stopping_drops_the_client_so_the_stream_unblocks():
	"""
	The blocking events() call cannot be interrupted, so stop() also drops the
	cached client: the stream then fails and the loop sees the flag.
	"""
	store.set("hosts", ONE_HOST)
	host_registry.reset()
	host_registry.client("h_local")
	assert "h_local" in host_registry._clients

	monitor = dcb.DockerEventMonitor("h_local")
	monitor.stop()
	assert monitor._stop.is_set()
	assert "h_local" not in host_registry._clients


def test_a_broken_host_does_not_stop_the_supervisor():
	"""One host failing to start must not prevent the others from running."""
	store.set("hosts", TWO_HOSTS)
	started = []
	original = dcb.DockerEventMonitor.demonio_event

	def flaky(self):
		if self.host_id == "h_nas":
			raise Exception("cannot start")
		started.append(self.host_id)

	dcb.DockerEventMonitor.demonio_event = flaky
	try:
		supervisor = dcb.EventMonitorSupervisor()
		try:
			supervisor.reconcile()
		except Exception:
			pass
		# The supervisor loop swallows the error and retries on the next pass,
		# so at worst one host is late, never all of them.
		dcb.DockerEventMonitor.demonio_event = lambda self: started.append(self.host_id)
		supervisor.reconcile()
		assert "h_local" in started
	finally:
		dcb.DockerEventMonitor.demonio_event = original
		store.set("hosts", ONE_HOST)


class _Clock:
	"""A clock the test moves by hand, and timers that only fire when told."""

	def __init__(self):
		self.now = 1000.0
		self.timers = []

	def __call__(self):
		return self.now

	def schedule(self, delay, fn):
		clock = self

		class Timer:
			cancelled = False

			def cancel(self):
				self.cancelled = True

		timer = Timer()
		timer.due, timer.fn = self.now + delay, fn
		clock.timers.append(timer)
		return timer

	def advance(self, seconds):
		self.now += seconds
		for timer in [t for t in self.timers if not t.cancelled and t.due <= self.now]:
			self.timers.remove(timer)
			timer.fn()


def _tracker():
	clock = _Clock()
	said = []
	tracker = dcb.RestartLoopTracker(lambda key, name: said.append((key, name)),
									clock=clock, schedule=clock.schedule)
	return tracker, clock, said


def _crash(tracker, clock, name="app", times=1, every=10):
	"""A container going down and straight back up, `times` times."""
	shown = []
	for _ in range(times):
		shown.append(tracker.should_announce("die", name))
		clock.advance(1)
		shown.append(tracker.should_announce("start", name))
		clock.advance(every)
	return shown


def test_a_restart_loop_is_announced_once_instead_of_every_restart():
	tracker, clock, said = _tracker()
	shown = _crash(tracker, clock, times=10)
	# The first two stops and starts are announced as usual; the third stop
	# reveals the loop, and from there on nothing.
	assert shown[:4] == [True, True, True, True], shown
	assert not any(shown[4:]), shown
	assert said == [("restart_loop", "app")], said


def test_a_loop_that_stays_up_is_announced_as_stable():
	tracker, clock, said = _tracker()
	_crash(tracker, clock, times=4)
	clock.advance(dcb.RestartLoopTracker.STABLE_SECONDS)
	assert said == [("restart_loop", "app"), ("restart_loop_stable", "app")], said

	# And it starts from scratch: a single stop afterwards is just a stop.
	assert tracker.should_announce("die", "app") is True


def test_a_loop_that_ends_stopped_says_it_stopped():
	"""Stopped by hand or given up on by on-failure: no start follows, and the user must still hear it."""
	tracker, clock, said = _tracker()
	_crash(tracker, clock, times=4)
	tracker.should_announce("die", "app")
	clock.advance(dcb.RestartLoopTracker.GONE_SECONDS)
	assert said == [("restart_loop", "app"), ("stopped_container", "app")], said


def test_stops_spread_out_are_not_a_loop():
	tracker, clock, said = _tracker()
	shown = _crash(tracker, clock, times=5, every=dcb.RestartLoopTracker.WINDOW_SECONDS)
	assert all(shown), shown
	assert said == []


def test_a_loop_is_about_one_container():
	tracker, clock, said = _tracker()
	_crash(tracker, clock, times=4)
	assert tracker.should_announce("die", "other") is True
	assert tracker.should_announce("start", "other") is True


def test_the_monitor_sends_the_loop_messages_with_its_host():
	store.set("hosts", TWO_HOSTS)
	dcb.host_registry.reset()
	sent = []
	original = dcb.send_message_to_notification_channel
	dcb.send_message_to_notification_channel = lambda message="", **kw: sent.append(message)
	events = [{"Type": "container", "Action": action, "Actor": {"Attributes": {"name": "app"}}}
				for _ in range(6) for action in ("die", "start")]

	class Stream:
		def events(self, decode=True):
			return iter(events)

	original_client = dcb.host_registry.client
	dcb.host_registry.client = lambda host_id: Stream()
	try:
		monitor = dcb.DockerEventMonitor("h_nas")
		monitor.detectar_eventos_contenedores()
		loop = dcb.get_text("restart_loop", "app") + dcb.host_suffix("h_nas")
		assert sent.count(loop) == 1, sent
		assert len(sent) == 5, sent  # two stops, two starts, the loop
	finally:
		for timer in list(monitor.restart_loops._looping.values()):
			timer.cancel()
		dcb.send_message_to_notification_channel = original
		dcb.host_registry.client = original_client
		store.set("hosts", ONE_HOST)
		dcb.host_registry.reset()


def test_stopping_closes_the_stream_it_is_blocked_on():
	"""
	Closing the client only empties its pool of idle connections, and the one
	the stream reads from is never idle: stop() left the thread, and over ssh
	its process, alive until the next event arrived.
	"""
	import threading
	from unittest.mock import MagicMock

	store.set("hosts", ONE_HOST)
	host_registry.reset()
	closed = threading.Event()

	class Stream:
		def __iter__(self):
			closed.wait(10)
			return iter(())
		def close(self):
			closed.set()

	client = MagicMock()
	client.events.return_value = Stream()
	original = host_registry.client
	host_registry.client = lambda host_id: client
	try:
		monitor = dcb.DockerEventMonitor("h_local")
		thread = threading.Thread(target=monitor.detectar_eventos_contenedores, daemon=True)
		thread.start()
		for _ in range(100):
			if monitor.listening:
				break
			threading.Event().wait(0.02)
		assert monitor.listening
		monitor.stop()
		thread.join(5)
		assert closed.is_set() and not thread.is_alive()
	finally:
		host_registry.client = original


def test_a_reconnection_asks_for_what_it_missed_and_skips_what_it_saw():
	"""Events during a dropped connection were lost; replaying them must not repeat any."""
	from unittest.mock import MagicMock
	import time

	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	announced = []
	monitor._announce = announced.append
	now = int(time.time())
	seen = {"Type": "container", "Action": "start", "timeNano": now * 10**9,
			"Actor": {"Attributes": {"name": "plex"}}}
	monitor._handle_event(seen)
	client = MagicMock()
	later = dict(seen, Action="die", timeNano=(now + 5) * 10**9)
	client.events.return_value = iter([seen, later])
	original = host_registry.client
	host_registry.client = lambda host_id: client
	try:
		monitor.detectar_eventos_contenedores()
		assert client.events.call_args.kwargs.get("since") == now, client.events.call_args
		assert len(announced) == 2, announced   # el start una vez, y el die
	finally:
		host_registry.client = original


def test_the_supervisor_brings_back_a_dead_monitor_and_a_deaf_stream():
	from unittest.mock import MagicMock

	store.set("hosts", TWO_HOSTS)
	started = []
	original = (dcb.DockerEventMonitor.demonio_event, host_registry.status_snapshot)

	def demonio(self):
		started.append(self.host_id)
		self.thread = MagicMock()
		self.thread.is_alive.return_value = True

	dcb.DockerEventMonitor.demonio_event = demonio
	host_registry.status_snapshot = lambda entries=None, **kw: {
		e["id"]: (e["id"] != "h_nas", "" if e["id"] != "h_nas" else "timed out") for e in entries}
	try:
		supervisor = dcb.EventMonitorSupervisor()
		supervisor.reconcile()
		assert sorted(started) == ["h_local", "h_nas"], started

		supervisor._monitors["h_local"].thread.is_alive.return_value = False
		resets = []
		nas = supervisor._monitors["h_nas"]
		nas._stream = MagicMock()
		nas._stream.close.side_effect = lambda: resets.append("h_nas")
		supervisor.reconcile()
		assert started.count("h_local") == 2, started
		assert resets == ["h_nas"], resets
	finally:
		dcb.DockerEventMonitor.demonio_event, host_registry.status_snapshot = original
		for monitor in supervisor._monitors.values():
			monitor._stop.set()


def test_restarts_that_were_asked_for_are_not_a_loop():
	"""
	Every stop counted, so three manual restarts in five minutes —or a task
	restarting a container every minute— read as a restart loop, and its
	notices were silenced from then on. Docker sends "kill" first when a stop
	is asked for, and never when a container falls over by itself.
	"""
	tracker, clock, said = _tracker()
	shown = []
	for _ in range(10):
		assert tracker.should_announce("kill", "app") is True
		shown.append(tracker.should_announce("die", "app"))
		clock.advance(1)
		shown.append(tracker.should_announce("start", "app"))
		clock.advance(30)
	assert all(shown) and said == [], (shown, said)

	# A kill long ago does not excuse a crash now.
	tracker.should_announce("kill", "db")
	clock.advance(dcb.RestartLoopTracker.KILL_SECONDS + 1)
	shown = _crash(tracker, clock, name="db", times=3)
	assert said == [("restart_loop", "db")], said


def test_the_tracker_forgets_containers_that_went_quiet():
	tracker, clock, said = _tracker()
	for name in ("a", "b", "c"):
		tracker.should_announce("die", name)
		tracker.should_announce("kill", name + "-k")
	clock.advance(dcb.RestartLoopTracker.WINDOW_SECONDS + 1)
	tracker.should_announce("die", "nuevo")
	assert set(tracker._stops) == {"nuevo"}, tracker._stops
	assert tracker._killed == {}, tracker._killed


def test_the_monitor_hands_kills_to_the_tracker_and_announces_nothing():
	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	announced = []
	monitor._announce = announced.append
	event = {"Type": "container", "Actor": {"Attributes": {"name": "app"}}}
	for _ in range(5):
		monitor._handle_event(dict(event, Action="kill"))
		monitor._handle_event(dict(event, Action="die"))
		monitor._handle_event(dict(event, Action="start"))
	assert len(announced) == 10, announced
	assert not any("bucle" in m.lower() or "loop" in m.lower() for m in announced), announced


def test_live_events_slightly_out_of_order_are_all_reported():
	"""
	Every event was compared with the newest one seen, so one that arrived a
	little late —a compose project starting services in parallel— was taken
	for a replay and dropped. Only what a reconnection replays can repeat.
	"""
	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	announced = []
	monitor._announce = announced.append
	base = {"Type": "container", "Action": "start"}
	for name, stamp in (("web", 1_000_000_002), ("db", 1_000_000_001), ("cache", 1_000_000_002)):
		monitor._handle_event(dict(base, timeNano=stamp, Actor={"Attributes": {"name": name}}))
	assert len(announced) == 3, announced
	assert monitor._last_event_ns == 1_000_000_002


def test_an_ssh_stream_is_closed_through_its_transport():
	"""
	docker-py cannot cancel a stream over ssh: close() fails looking for a
	socket. The stream stayed open, with its ssh process, until the host sent
	another event.
	"""
	from types import SimpleNamespace
	from unittest.mock import MagicMock

	channel = MagicMock()
	reader = SimpleNamespace(channel=channel, raw=object())
	stream = MagicMock()
	stream.close.side_effect = UnboundLocalError("sock")
	stream._response = SimpleNamespace(raw=SimpleNamespace(_fp=SimpleNamespace(fp=reader)))
	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	monitor._stream = stream
	monitor.reset_stream()
	assert channel.close.called


def test_a_tcp_event_stream_asks_for_keepalive():
	"""A host that reboots between two supervisor passes left a dead stream that looked quiet."""
	import socket
	from types import SimpleNamespace
	from unittest.mock import MagicMock

	sock = MagicMock(family=socket.AF_INET)
	stream = SimpleNamespace(_response=SimpleNamespace(raw=SimpleNamespace(
		_fp=SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock))))))
	dcb._keep_alive(stream)
	options = [call.args[:2] for call in sock.setsockopt.call_args_list]
	assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE) in options, options
	unix = MagicMock(family=socket.AF_UNIX)
	stream._response.raw._fp.fp.raw._sock = unix
	dcb._keep_alive(stream)
	assert not unix.setsockopt.called


# --- Why a container stopped, and whether it is healthy ---------------------
#
# The sequences below are the ones Docker 29 sends, captured with a stream
# open while each thing was done to a real container.

def _monitor_with_logs(logs="", health_output=""):
	store.set("hosts", ONE_HOST)
	monitor = dcb.DockerEventMonitor("h_local")
	announced = []
	monitor._announce = lambda message, detail=None: announced.append((message, detail))
	monitor._last_logs = lambda container_id: dcb.log_excerpt(logs)
	monitor._last_health_output = lambda container_id: dcb.log_excerpt(health_output)
	return monitor, announced


def _event(action, name="app", **attributes):
	return {"Type": "container", "Action": action, "id": "f" * 64,
			"Actor": {"ID": "f" * 64, "Attributes": dict(attributes, name=name)}}


def test_a_container_that_finishes_on_its_own_says_it_finished():
	monitor, announced = _monitor_with_logs()
	monitor._handle_event(_event("die", exitCode="0"))
	assert announced == [(dcb.get_text("container_finished", "app"), None)], announced


def test_a_container_that_fails_says_its_code_and_what_it_last_wrote():
	monitor, announced = _monitor_with_logs("conectando…\n\x1b[31mERROR: <sin conexión>\x1b[0m\n")
	monitor._handle_event(_event("die", exitCode="3"))
	message, detail = announced[-1]
	assert message == dcb.get_text("container_failed", "app", "3"), message
	assert detail == "<pre>conectando…\nERROR: &lt;sin conexión&gt;</pre>", detail


def test_a_signal_exit_code_is_named():
	monitor, announced = _monitor_with_logs()
	monitor._handle_event(_event("die", exitCode="139"))
	assert "139 (SIGSEGV)" in announced[-1][0], announced


def test_running_out_of_memory_is_told_apart():
	"""`oom` arrives just before the stop it causes, which reads 137 like any kill."""
	monitor, announced = _monitor_with_logs()
	monitor._handle_event(_event("oom"))
	monitor._handle_event(_event("die", exitCode="137"))
	assert announced == [(dcb.get_text("container_oom", "app"), None)], announced


def test_a_stop_that_was_asked_for_is_still_just_a_stop():
	"""`docker stop`: kill with SIGTERM, kill with SIGKILL, stop, then die 137."""
	monitor, announced = _monitor_with_logs("nada que ver")
	for event in (_event("kill", signal="15"), _event("kill", signal="9"), _event("stop"),
					_event("die", exitCode="137")):
		monitor._handle_event(event)
	assert announced == [(dcb.get_text("stopped_container", "app"), None)], announced


def test_a_failing_healthcheck_is_announced_once_and_its_recovery_too():
	"""
	Docker sends health_status on changes only: healthy right after starting —
	not news —, unhealthy, and healthy again, which is.
	"""
	monitor, announced = _monitor_with_logs(health_output="curl: (7) Failed to connect\n")
	for action in ("health_status: healthy", "health_status: unhealthy",
					"health_status: unhealthy", "health_status: healthy", "health_status: healthy"):
		monitor._handle_event(_event(action))
	assert announced == [
		(dcb.get_text("container_unhealthy", "app"), "<pre>curl: (7) Failed to connect</pre>"),
		(dcb.get_text("container_healthy_again", "app"), None),
	], announced


def test_what_is_remembered_about_a_container_goes_with_it():
	monitor, announced = _monitor_with_logs()
	monitor._handle_event(_event("health_status: unhealthy"))
	monitor._handle_event(_event("oom"))
	monitor._handle_event(_event("destroy"))
	assert monitor._unhealthy == set() and monitor._oom == {}


def test_a_log_excerpt_keeps_the_end_and_fits_in_a_message():
	text = "\n".join(f"línea {i} " + "x" * 400 for i in range(50))
	excerpt = dcb.log_excerpt(text)
	assert excerpt.startswith("<pre>") and excerpt.endswith("</pre>")
	assert "línea 49" in excerpt and "línea 30" not in excerpt
	assert len(excerpt) < 1600, len(excerpt)
	assert dcb.log_excerpt("\n  \n") is None and dcb.log_excerpt(None) is None
