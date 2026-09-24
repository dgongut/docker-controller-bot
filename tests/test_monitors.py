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
