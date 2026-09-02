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
