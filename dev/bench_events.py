#!/usr/bin/env python3
"""
What the event monitor costs, measured against a real daemon.

	python3 dev/bench_events.py tcp://dind:2375 [seconds per run]

Runs the bot's own DockerEventMonitor against the daemon for a while in each
of these, one after the other, and prints the CPU the process spent and the
events that reached it:

	unfiltered   the stream as it was before 5.4.0, every event of the daemon
	filtered     the stream asking only for MONITORED_EVENTS
	levels       filtered, with a notification level set for every container
	labels       filtered, the bench containers labelled DCB-Notifications

Meant for a daemon of its own, a dind, with containers whose healthchecks run
often: each run of one is three exec events. Run it in a container with its
CPU limited to see a slow server. It never removes anything it did not create.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import harness  # noqa: E402

dcb, store, _root = harness.load_bot(real_docker=True)
import host_registry  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "unix:///var/run/docker.sock"
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
BENCH_LABEL = "dcb-bench"


def run(mode):
	store.set("hosts", [{"id": "h_local", "alias": "bench", "url": URL, "local": True}])
	host_registry.reset()
	client = host_registry.client("h_local")
	# Sparse, one request: docker-py then has no .name, only the list's Names.
	names = [c.attrs["Names"][0].lstrip("/")
			 for c in client.containers.list(all=True, sparse=True, filters={"label": BENCH_LABEL})]
	assert names, f"no containers labelled {BENCH_LABEL} on {URL}"
	store.forget_host_containers("h_local")
	if mode == "levels":
		for name in names:
			store.set_container_notification_level("h_local", name, "problems")

	monitor = dcb.DockerEventMonitor("h_local")
	announced = []
	monitor._announce = lambda message, detail=None: announced.append(message)
	received = [0]
	handle = monitor._handle_event

	def counted(event):
		received[0] += 1
		handle(event)
	monitor._handle_event = counted

	if mode == "unfiltered":
		events = client.events
		client.events = lambda *a, **kw: events(*a, **{k: v for k, v in kw.items() if k != "filters"})
		original_client = host_registry.client
		host_registry.client = lambda host_id: client

	thread = threading.Thread(target=monitor.detectar_eventos_contenedores, daemon=True)
	thread.start()
	time.sleep(2)  # connected, and past the first burst
	received[0] = 0
	announced.clear()
	cpu, wall = time.process_time(), time.monotonic()
	time.sleep(SECONDS)
	cpu, wall = time.process_time() - cpu, time.monotonic() - wall
	got, said = received[0], len(announced)
	monitor.stop()
	thread.join(10)
	if mode == "unfiltered":
		host_registry.client = original_client
	store.forget_host_containers("h_local")
	return cpu, wall, got, said


def handler(count=100_000):
	"""
	The cost of one event in _handle_event alone, without a daemon: what a
	level or a label adds to each event, apart from everything else the
	process does. Events that are not news, which is what nearly all are.
	"""
	store.set("hosts", [{"id": "h_local", "alias": "bench", "url": URL, "local": True}])
	store.forget_host_containers("h_local")
	for i in range(200):
		store.set_container_notification_level("h_local", f"set{i}", "problems")
	monitor = dcb.DockerEventMonitor("h_local")
	monitor._announce = lambda message, detail=None: None
	monitor._schedule = lambda delay, fn: threading.Timer(3600, fn)
	print(f"{'per event, start':<28}{'µs':>8}")
	for title, name, labels in (("no setting", "plain", {}),
								("setting: problems", "set7", {}),
								("label: problems", "plain", {dcb.LABEL_NOTIFICATIONS: "problems"}),
								("label: none", "plain", {dcb.LABEL_NOTIFICATIONS: "none"})):
		events = [{"Type": "container", "Action": "start", "timeNano": n + 1,
				   "Actor": {"ID": "f" * 64, "Attributes": dict(labels, name=name, image="alpine")}}
				  for n in range(count)]
		monitor._last_event_ns = monitor._replay_until = None
		start = time.process_time()
		for event in events:
			monitor._handle_event(event)
		print(f"{title:<28}{(time.process_time() - start) / count * 1e6:>8.1f}", flush=True)
	store.forget_host_containers("h_local")


def main():
	if sys.argv[3:] == ["handler"]:
		handler()
		return
	print(f"{URL}, {SECONDS} s per run, CPU of this process")
	print(f"{'mode':<12}{'cpu s':>8}{'cpu %':>8}{'events':>9}{'ev/s':>8}{'µs/ev':>8}{'sent':>6}")
	for mode in sys.argv[3:] or ("unfiltered", "filtered", "levels", "labels"):
		cpu, wall, got, said = run(mode)
		per_event = cpu / got * 1e6 if got else 0
		print(f"{mode:<12}{cpu:>8.2f}{cpu / wall * 100:>7.1f}%{got:>9}{got / wall:>8.1f}{per_event:>8.0f}{said:>6}",
			  flush=True)


if __name__ == "__main__":
	main()
