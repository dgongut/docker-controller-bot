"""
Container listings in one request, and the ssh connection that stays open.

Over ssh:// every request to the daemon was a new ssh process, and the SDK's
containers.list() is a request per container on top of the list itself:
fourteen containers, fifteen handshakes. These check that the bot reads the
list alone where the list is enough, asks individually only where it is not,
still goes to the daemon for what has to be current, and keeps the ssh
connection between requests.

The daemon is a double that answers the list and the inspect and counts each
request, with an optional delay per request standing in for the round-trip
of ssh://. The SDK's own ContainerCollection sits on top of it, so what the
SDK does with the double is what it does with a daemon.
"""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

if harness.REPO not in sys.path:
	sys.path.insert(0, harness.REPO)

import docker.errors
from docker.models.containers import Container, ContainerCollection

import container_listing
import host_registry


def entry(number, name, state="running", status=None, image="nginx:1.27", labels=None, moved=False):
	"""One container as the daemon lists it (GET /containers/json)."""
	image_id = "sha256:" + f"{number:02x}" * 32
	return {
		"Id": f"{number:02x}" * 32,
		"Names": [f"/{name}"],
		# Once the tag has moved on the daemon prints the image id here.
		"Image": image_id if moved else image,
		"ImageID": image_id,
		"Command": "sh",
		"Created": 1700000000 + number,
		"Ports": [{"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"}] if state == "running" else [],
		"Labels": labels or {},
		"State": state,
		"Status": status or ("Up 2 hours" if state == "running" else "Exited (0) 3 hours ago"),
		"HostConfig": {"NetworkMode": "bridge"},
		"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2"}}},
		"Mounts": [],
		"_image": image,
	}


def inspect_of(listed):
	"""The same container as the daemon inspects it (GET /containers/{id}/json)."""
	health = container_listing.health_from_status(listed["Status"])
	state = {"Status": listed["State"], "Running": listed["State"] == "running"}
	if health:
		state["Health"] = {"Status": health, "Log": []}
	return {
		"Id": listed["Id"],
		"Name": listed["Names"][0],
		"Image": listed["ImageID"],
		"State": state,
		"Config": {"Image": listed["_image"], "Labels": dict(listed["Labels"]), "Healthcheck": {"Test": ["CMD", "true"]} if health else None},
		"HostConfig": {"NetworkMode": "bridge", "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}, "IpcMode": "private"},
		"ImageManifestDescriptor": {"platform": {"os": "linux", "architecture": "arm64"}},
		"Mounts": [],
		"NetworkSettings": listed["NetworkSettings"],
	}


class FakeDaemon:
	"""
	Answers the list and the inspect like a daemon, counting every request.

	`latency` is slept per request: zero for the checks, a few hundredths for
	the comparison, where it stands in for the round-trip of ssh://.
	"""

	def __init__(self, entries, latency=0.0, gone=()):
		self.entries = entries
		self.latency = latency
		self.gone = set(gone)
		self.calls = []
		# What a listed object's actions reach.
		self.start = MagicMock(name="start")
		self.stop = MagicMock(name="stop")
		self.logs = MagicMock(name="logs", return_value=b"hello")
		self.remove_container = MagicMock(name="remove_container")

	def containers(self, all=False, filters=None, **kwargs):
		self.calls.append(("list", all, filters))
		time.sleep(self.latency)
		statuses = (filters or {}).get("status")
		name = (filters or {}).get("name")
		labels = (filters or {}).get("label")
		labels = [labels] if isinstance(labels, str) else list(labels or [])
		listed = []
		for data in self.entries:
			if statuses and data["State"] not in statuses:
				continue
			if name and name not in data["Names"][0]:
				continue
			if any(data["Labels"].get(key) != value if value else key not in data["Labels"]
					for key, _, value in (label.partition("=") for label in labels)):
				continue
			if not all and not statuses and data["State"] != "running":
				continue
			listed.append({k: v for k, v in data.items() if k != "_image"})
		return listed

	def inspect_container(self, container_id):
		self.calls.append(("inspect", container_id))
		time.sleep(self.latency)
		for data in self.entries:
			if data["Id"].startswith(container_id) or data["Names"][0] == f"/{container_id}":
				if data["Id"] in self.gone:
					break
				return inspect_of(data)
		raise docker.errors.NotFound(f"No such container: {container_id}")

	def inspects(self):
		return [call for call in self.calls if call[0] == "inspect"]


class FakeClient:
	"""A DockerClient with the SDK's real container collection on a fake daemon."""

	def __init__(self, daemon):
		self.api = daemon
		self.containers = ContainerCollection(client=self)


def fourteen(moved=0):
	"""Fourteen containers, like the host that showed the problem; `moved` of them with a pending update."""
	entries = []
	for number in range(1, 15):
		state = "running" if number <= 10 else "exited"
		labels = {"com.docker.compose.project": "media", "com.docker.compose.service": f"svc{number}"} if 3 <= number <= 6 else {}
		status = "Up 2 hours (healthy)" if number == 7 else ("Up 2 hours (unhealthy)" if number == 8 else None)
		entries.append(entry(number, f"c{number:02d}", state=state, status=status, labels=labels, moved=number <= moved))
	return entries


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

def test_the_sdk_list_is_a_request_per_container_on_top_of_the_list():
	"""The baseline: what containers.list() costs, and why ssh:// paid for it."""
	daemon = FakeDaemon(fourteen())
	client = FakeClient(daemon)
	containers = client.containers.list(all=True)
	assert len(containers) == 14
	assert daemon.calls[0] == ("list", True, None)
	assert len(daemon.inspects()) == 14, daemon.calls


def test_the_list_is_one_request_and_reads_like_an_inspect():
	daemon = FakeDaemon(fourteen())
	client = FakeClient(daemon)
	containers = container_listing.list_containers(client, all=True, where="nas")
	assert daemon.calls == [("list", True, None)], daemon.calls
	assert len(containers) == 14
	by_name = {c.name: c for c in containers}
	assert sorted(by_name) == [f"c{n:02d}" for n in range(1, 15)]
	for container in containers:
		assert isinstance(container, Container)
		assert container.listed is True
		assert container.id == f"{int(container.name[1:]):02x}" * 32
		assert container.short_id == container.id[:12]
	assert by_name["c01"].status == "running" and by_name["c11"].status == "exited"
	assert by_name["c03"].labels == {"com.docker.compose.project": "media", "com.docker.compose.service": "svc3"}
	assert by_name["c01"].labels == {}
	# What the update cache is keyed by, and what /info and the version read.
	assert by_name["c01"].attrs["Config"]["Image"] == "nginx:1.27"
	# What the menus' emoji reads.
	assert by_name["c07"].attrs["State"]["Health"]["Status"] == "healthy"
	assert by_name["c08"].attrs["State"]["Health"]["Status"] == "unhealthy"
	assert "Health" not in by_name["c01"].attrs["State"]
	# What the SDK's `image` property reads, laid out as an inspect lays it out.
	assert by_name["c01"].attrs["ImageID"] == by_name["c01"].attrs["Image"] == "sha256:" + "01" * 32
	# What the list carries is still there, untouched.
	assert by_name["c01"].attrs["HostConfig"] == {"NetworkMode": "bridge"}
	assert by_name["c01"].attrs["Names"] == ["/c01"]


def test_health_is_read_from_the_status_the_list_prints():
	assert container_listing.health_from_status("Up 3 hours (healthy)") == "healthy"
	assert container_listing.health_from_status("Up 3 hours (unhealthy)") == "unhealthy"
	assert container_listing.health_from_status("Up 2 seconds (health: starting)") == "starting"
	assert container_listing.health_from_status("Up 3 hours (Paused)") is None
	assert container_listing.health_from_status("Up 3 hours") is None
	assert container_listing.health_from_status("Exited (1) 2 days ago") is None
	assert container_listing.health_from_status(None) is None


def test_a_container_without_a_name_is_named_by_its_id():
	data = entry(1, "x")
	data["Names"] = []
	attrs = container_listing.inspect_like(data)
	assert attrs["Name"] == "/" + "01" * 6


def test_a_linked_container_is_named_by_its_own_name_not_the_link():
	data = entry(1, "db")
	data["Names"] = ["/web/db", "/db"]
	assert container_listing.inspect_like(data)["Name"] == "/db"


def test_only_a_container_whose_tag_moved_on_is_inspected():
	"""
	The list prints the image id instead of the reference once the tag points
	at a newer image: exactly a container with an update pending. Its
	Config.Image, which the update cache is keyed by, is only in the inspect.
	"""
	daemon = FakeDaemon(fourteen(moved=2))
	client = FakeClient(daemon)
	containers = container_listing.list_containers(client, all=True, where="nas")
	assert len(containers) == 14
	assert daemon.inspects() == [("inspect", "01" * 32), ("inspect", "02" * 32)], daemon.calls
	by_name = {c.name: c for c in containers}
	for name in ("c01", "c02"):
		assert not getattr(by_name[name], "listed", False)
		assert by_name[name].attrs["Config"]["Image"] == "nginx:1.27"
		assert "PortBindings" in by_name[name].attrs["HostConfig"]
	assert by_name["c03"].listed is True


def test_the_moved_tag_is_told_apart_from_a_reference():
	moved = container_listing.image_reference_moved
	image_id = "sha256:" + "ab" * 32
	assert moved({"Image": image_id, "ImageID": image_id})
	assert moved({"Image": "ab" * 6, "ImageID": image_id}), "older daemons print the short id"
	assert moved({"Image": "ab" * 32, "ImageID": image_id})
	assert not moved({"Image": "nginx:1.27", "ImageID": image_id})
	assert not moved({"Image": "nginx", "ImageID": image_id})
	assert not moved({"Image": "ghcr.io/dgongut/docker-controller-bot:5.0.0", "ImageID": image_id})
	assert not moved({"Image": "nginx@sha256:" + "cd" * 32, "ImageID": image_id})
	assert not moved({"Image": "deadbeef", "ImageID": image_id}), "shorter than an id: a repository called that"
	assert not moved({"Image": "", "ImageID": image_id})


def test_a_container_removed_while_listing_is_left_out():
	"""The SDK's list() raises NotFound here and the whole listing fails with it."""
	daemon = FakeDaemon(fourteen(moved=2), gone=("01" * 32,))
	client = FakeClient(daemon)
	containers = container_listing.list_containers(client, all=True, where="nas")
	assert [c.name for c in containers if c.name in ("c01", "c02")] == ["c02"]
	assert len(containers) == 13


def test_reload_fetches_the_inspect_and_the_object_stops_being_listed():
	daemon = FakeDaemon(fourteen())
	client = FakeClient(daemon)
	container = container_listing.list_containers(client, all=True)[0]
	assert "PortBindings" not in container.attrs["HostConfig"]
	container.reload()
	assert daemon.inspects() == [("inspect", container.id)]
	assert container.listed is False
	assert container.attrs["HostConfig"]["PortBindings"] == {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}
	assert container.attrs["ImageManifestDescriptor"]["platform"]["architecture"] == "arm64"
	assert container.name == "c01" and container.status == "running"


def test_a_listed_container_still_acts_on_the_daemon():
	"""start, stop, logs, remove: what needs the daemon keeps going to it, with the id."""
	daemon = FakeDaemon(fourteen())
	client = FakeClient(daemon)
	container = container_listing.list_containers(client, all=True)[0]
	container.start()
	container.stop(timeout=5)
	assert container.logs(tail=50) == b"hello"
	container.remove(force=True)
	daemon.start.assert_called_once_with(container.id)
	daemon.stop.assert_called_once_with(container.id, timeout=5)
	daemon.logs.assert_called_once()
	assert daemon.logs.call_args[0][0] == container.id
	assert daemon.remove_container.call_count == 1
	assert daemon.remove_container.call_args[0][0] == container.id
	assert daemon.remove_container.call_args[1]["force"] is True


def test_an_empty_list_is_an_empty_list():
	daemon = FakeDaemon([])
	assert container_listing.list_containers(FakeClient(daemon), all=True) == []
	assert daemon.calls == [("list", True, None)]


# ---------------------------------------------------------------------------
# Through the bot
# ---------------------------------------------------------------------------

def test_the_manager_lists_in_one_request_and_filters_as_before():
	dcb, _store, _root = harness.load_bot()
	daemon = FakeDaemon(fourteen())
	owner = dcb.DockerManager("h_local", client=FakeClient(daemon))

	containers = owner.list_containers()
	assert daemon.calls == [("list", True, None)], daemon.calls
	# Running first, then by name: what the menus have always shown.
	assert [c.name for c in containers] == [f"c{n:02d}" for n in range(1, 11)] + [f"c{n:02d}" for n in range(11, 15)]

	daemon.calls.clear()
	assert [c.name for c in owner.list_containers("/run")] == ["c11", "c12", "c13", "c14"]
	assert daemon.calls == [("list", False, {"status": ["paused", "exited", "created", "dead"]})]
	daemon.calls.clear()
	assert len(owner.list_containers("/stop@bot")) == 10
	assert daemon.calls == [("list", False, {"status": ["running", "restarting"]})]
	daemon.calls.clear()
	assert len(owner.list_containers("/exec")) == 10
	assert daemon.calls == [("list", False, {"status": ["running"]})]

	# /ports reads PortBindings, which only the inspect carries, and asks for it.
	daemon.calls.clear()
	inspected = owner.list_containers(inspect=True)
	assert len(daemon.inspects()) == 14
	assert all(not getattr(c, "listed", False) for c in inspected)
	assert all("PortBindings" in c.attrs["HostConfig"] for c in inspected)


def test_the_menu_and_the_list_read_the_listed_objects():
	"""What /list and the keyboards draw —status, health, update, project— comes out the same."""
	dcb, _store, _root = harness.load_bot()
	daemon = FakeDaemon(fourteen(moved=1))
	owner = dcb.DockerManager("h_local", client=FakeClient(daemon))
	containers = owner.list_containers()
	by_name = {c.name: c for c in containers}
	assert dcb.get_status_emoji("running", "c07", by_name["c07"], "h_local") == "💚"
	assert dcb.get_status_emoji("running", "c08", by_name["c08"], "h_local") == "🟢 (💔)"
	assert dcb.get_status_emoji("running", "c01", by_name["c01"], "h_local") == "🟢"
	dcb.save_container_update_status("nginx:1.27", "c01", True, "h_local")
	try:
		assert dcb.update_available(by_name["c01"], "h_local") is True
		assert dcb.update_available(by_name["c02"], "h_local") is False
		text = dcb.display_containers(containers, "h_local")
	finally:
		_store.forget_update_status("h_local", "c01")
	assert "media" in text and "c01" in text
	# One inspect in all of that: the container whose tag moved on.
	assert daemon.inspects() == [("inspect", "01" * 32)], daemon.calls


def test_container_named_matches_exactly_from_the_list():
	dcb, _store, _root = harness.load_bot()
	daemon = FakeDaemon([entry(1, "plex"), entry(2, "plex-meta"), entry(3, "tautulli-plex")])
	owner = dcb.DockerManager("h_local", client=FakeClient(daemon))
	found = owner.container_named("plex")
	assert found is not None and found.id == "01" * 32
	assert daemon.calls == [("list", True, {"name": "plex"})], daemon.calls
	assert owner.container_named("sonarr") is None
	original = dcb.manager
	dcb.manager = lambda host_id=None: owner
	try:
		assert dcb.find_container_id_on_host("h_local", "plex-meta") == ("02" * 32)[:dcb.CONTAINER_ID_LENGTH]
	finally:
		dcb.manager = original
	assert not daemon.inspects()


def test_the_update_emoji_of_a_service_is_one_request():
	dcb, _store, _root = harness.load_bot()
	daemon = FakeDaemon([entry(1, "plex"), entry(2, "sonarr", moved=True)])
	original = dcb.manager
	dcb.manager = lambda host_id=None: dcb.DockerManager("h_local", client=FakeClient(daemon))
	dcb.save_container_update_status("nginx:1.27", "sonarr", True, "h_local")
	try:
		assert dcb.get_update_emoji("plex", "h_local") == "✅"
		assert daemon.calls == [("list", True, {"name": "plex"})], daemon.calls
		daemon.calls.clear()
		# Pending, and its tag moved on: the list plus the one inspect that says the image.
		assert dcb.get_update_emoji("sonarr", "h_local") == "⬆️"
		assert daemon.calls == [("list", True, {"name": "sonarr"}), ("inspect", "02" * 32)], daemon.calls
		daemon.calls.clear()
		assert dcb.get_update_emoji("radarr", "h_local") == "✅"
		assert not daemon.inspects()
	finally:
		dcb.manager = original
		_store.forget_update_status("h_local", "sonarr")


def test_compose_projects_come_from_one_request():
	dcb, _store, _root = harness.load_bot()
	daemon = FakeDaemon(fourteen())
	owner = dcb.DockerManager("h_local", client=FakeClient(daemon))
	projects = owner.get_compose_projects()
	assert daemon.calls == [("list", True, None)], daemon.calls
	assert list(projects) == ["media"]
	assert projects["media"].get_service_names() == ["svc3", "svc4", "svc5", "svc6"]
	daemon.calls.clear()
	info = owner.get_project_info("media")
	assert daemon.calls == [("list", True, {"label": "com.docker.compose.project=media"})], daemon.calls
	assert info.get_container_count() == 4
	assert "svc3" in owner.get_project_info_formatted("media")
	assert not daemon.inspects()


def test_the_update_check_inspects_what_it_is_about_to_pull():
	"""
	The platform a pull has to ask for is only in the inspect, so a listed
	container is reloaded before its image is checked — and not the ones the
	list already rules out.
	"""
	dcb, _store, _root = harness.load_bot()
	entries = [entry(1, "plex"), entry(2, "sonarr", state="exited"), entry(3, "radarr", labels={"DCB-Ignore-Check-Updates": "true"})]
	daemon = FakeDaemon(entries)
	client = FakeClient(daemon)
	client.images = MagicMock()
	client.images.get.return_value = MagicMock(id="sha256:" + "01" * 32, attrs={"Config": {}})
	client.images.pull.return_value = MagicMock(id="sha256:" + "01" * 32, attrs={"Config": {}})
	owner = dcb.DockerManager("h_local", client=client)
	containers = owner.list_containers()
	assert not daemon.inspects()
	# Through the loop itself, with the pull standing in: a copy of its rules
	# here went on passing while the loop reloaded everything it listed.
	previous = (_store.get("bot.check_update_stopped_containers"), dcb.pull_image)
	_store.set("bot.check_update_stopped_containers", False)
	pulled = []

	def pull(connection, image, container):
		pulled.append(container.name)
		return MagicMock(id="sha256:" + "01" * 32)
	dcb.pull_image = pull
	try:
		dcb.DockerUpdateMonitor()._check_host({"id": "h_local", "alias": "casa"}, owner, True, containers)
	finally:
		_store.set("bot.check_update_stopped_containers", previous[0])
		dcb.pull_image = previous[1]
		for name in ("plex", "sonarr", "radarr"):
			dcb.save_container_update_status("x", name, None, "h_local")
	assert pulled == ["plex"], pulled
	assert daemon.inspects() == [("inspect", "01" * 32)], daemon.calls


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def test_the_listing_no_longer_grows_with_the_number_of_containers():
	"""
	With a round-trip of 20 ms per request —ssh:// over a LAN; a VPN is ten
	times that— fourteen containers were fifteen round-trips. Timed, and
	printed, so the gain can be seen; asserted on the requests, which is what
	the time is made of.
	"""
	latency = 0.02
	daemon = FakeDaemon(fourteen(moved=1), latency=latency)
	client = FakeClient(daemon)

	started = time.perf_counter()
	data = client.api.containers(all=True)
	api_list = time.perf_counter() - started

	started = time.perf_counter()
	client.containers.list(all=True)
	sdk_list = time.perf_counter() - started

	started = time.perf_counter()
	[client.containers.get(x["Id"]) for x in data]
	gets = time.perf_counter() - started

	daemon.calls.clear()
	started = time.perf_counter()
	container_listing.list_containers(client, all=True, where="bench")
	listing = time.perf_counter() - started
	requests = len(daemon.calls)

	print(f"\n    14 containers, {int(latency * 1000)} ms per request:"
			f"\n      api.containers(all=True)      {api_list:.3f}s"
			f"\n      containers.list(all=True)     {sdk_list:.3f}s  (SDK: 1 list + 14 inspects)"
			f"\n      [containers.get(Id) ...]      {gets:.3f}s"
			f"\n      container_listing             {listing:.3f}s  ({requests} requests: 1 list + {requests - 1} inspect for a moved tag)")
	assert requests == 2, daemon.calls
	assert sdk_list >= 15 * latency
	assert listing < sdk_list / 4, (listing, sdk_list)


# ---------------------------------------------------------------------------
# The ssh connection
# ---------------------------------------------------------------------------

def with_paramiko():
	"""
	The SDK imports paramiko to load its ssh transport at all, even when the
	connection is handed to the ssh binary. The image ships it; a laptop may
	not, so it is stood in for. So is the `ssh` binary, which a plain python
	image does not have either: the suite passed on a laptop and failed there.
	Returns what to hand back to without_paramiko().
	"""
	import types
	installed = "paramiko" in sys.modules
	if not installed:
		try:
			import paramiko  # noqa: F401
			installed = True
		except ImportError:
			sys.modules["paramiko"] = types.ModuleType("paramiko")
	original_which = host_registry.shutil.which
	if not original_which("ssh"):
		host_registry.shutil.which = lambda name: "/usr/bin/ssh" if name == "ssh" else original_which(name)
	return installed, original_which


def without_paramiko(state):
	installed, original_which = state
	if not installed:
		sys.modules.pop("paramiko", None)
	host_registry.shutil.which = original_which


def _ssh_adapter():
	"""The SDK's adapter as use_ssh_client=True builds it; nothing is dialled."""
	from docker.transport.sshconn import SSHHTTPAdapter
	return SSHHTTPAdapter("ssh://root@nas", timeout=30, shell_out=True)


def test_the_sdk_builds_a_new_ssh_pool_for_every_request():
	"""The SDK as shipped: what every request over ssh:// paid a handshake for."""
	state = with_paramiko()
	try:
		adapter = _ssh_adapter()
		first = adapter.get_connection("http+docker://ssh/v1.45/containers/json")
		second = adapter.get_connection("http+docker://ssh/v1.45/containers/json")
		assert first is not second
		assert len(adapter.pools) == 0
	finally:
		without_paramiko(state)


def test_an_ssh_client_keeps_one_pool_and_the_adapter_closes_it():
	state = with_paramiko()
	try:
		from docker.transport.sshconn import SSHConnectionPool
		adapter = _ssh_adapter()
		client = MagicMock()
		client.api._custom_adapter = adapter
		assert host_registry._reuse_ssh_connections(client, "h_nas") is True
		pool = adapter.get_connection("http+docker://ssh/v1.45/containers/json")
		assert isinstance(pool, SSHConnectionPool)
		assert adapter.get_connection("http+docker://ssh/v1.45/version") is pool
		assert adapter.get_connection_with_tls_context(MagicMock(url="http+docker://ssh/v1.45/version"), True) is pool
		assert pool.timeout == 30 and pool.ssh_host == "root@nas"
		assert list(adapter.pools.keys()) == ["root@nas"]
		adapter.close()
		assert pool.pool is None, "closing the adapter has to close the pool, and the ssh process with it"
	finally:
		without_paramiko(state)


def test_a_connection_whose_ssh_process_ended_is_replaced():
	state = with_paramiko()
	try:
		adapter = _ssh_adapter()
		client = MagicMock()
		client.api._custom_adapter = adapter
		host_registry._reuse_ssh_connections(client, "h_nas")
		pool = adapter.get_connection("http+docker://ssh/v1.45/version")

		# The pool starts full of empty slots; take one, as a request would,
		# before handing a connection back.
		pool.pool.get(block=False)
		alive = MagicMock(name="alive")
		alive.sock.proc.poll.return_value = None
		pool._put_conn(alive)
		assert pool._get_conn(timeout=None) is alive

		dead = MagicMock(name="dead")
		dead.sock.proc.poll.return_value = 255
		pool._put_conn(dead)
		replacement = pool._get_conn(timeout=None)
		assert replacement is not dead
		dead.close.assert_called_once()
		assert replacement.sock is None, "a fresh connection, dialled on first use"
	finally:
		without_paramiko(state)


def test_a_client_that_dials_with_paramiko_is_left_alone():
	state = with_paramiko()
	try:
		adapter = _ssh_adapter()
		adapter.ssh_client = object()  # what paramiko mode sets
		client = MagicMock()
		client.api._custom_adapter = adapter
		assert host_registry._reuse_ssh_connections(client, "h_nas") is False
		assert not isinstance(adapter.__dict__.get("get_connection"), type(lambda: None))
	finally:
		without_paramiko(state)
	# Not an ssh client at all: nothing to do, nothing raised.
	assert host_registry._reuse_ssh_connections(MagicMock(), "h_local") is False


def test_building_an_ssh_host_keeps_its_connection():
	"""Through _build_client, which is the one place clients are made."""
	import docker
	state = with_paramiko()
	original = docker.DockerClient
	built = {}

	def sdk(**kwargs):
		built.update(kwargs)
		client = MagicMock()
		client.api._custom_adapter = _ssh_adapter()
		return client

	docker.DockerClient = sdk
	try:
		client = host_registry._build_client({"id": "h_nas", "alias": "nas", "url": "ssh://root@nas"})
		assert built["use_ssh_client"] is True
		adapter = client.api._custom_adapter
		assert adapter.get_connection("http+docker://ssh/a") is adapter.get_connection("http+docker://ssh/b")
		local = host_registry._build_client({"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True})
		assert "get_connection" not in local.api._custom_adapter.__dict__
	finally:
		docker.DockerClient = original
		without_paramiko(state)


def test_a_closed_ssh_pool_is_opened_again_on_the_next_request():
	"""
	drop() closes a failing host's clients, but whoever already holds one goes
	on using it. With the one pool fixed for good, every request after that
	said "Pool is closed"; tcp:// and unix:// simply open a new one.
	"""
	import threading
	state = with_paramiko()
	try:
		adapter = _ssh_adapter()
		client = MagicMock()
		client.api._custom_adapter = adapter
		host_registry._reuse_ssh_connections(client, "h_nas")
		first = adapter.get_connection("http+docker://ssh/v1.45/version")
		adapter.close()
		assert first.pool is None

		reopened = adapter.get_connection("http+docker://ssh/v1.45/version")
		assert reopened is not first and reopened.pool is not None
		assert adapter.get_connection("http+docker://ssh/v1.45/containers/json") is reopened
		# Registered again, so the adapter can still close it.
		assert list(adapter.pools.keys()) == ["root@nas"] and adapter.pools["root@nas"] is reopened
		adapter.close()
		assert reopened.pool is None

		# Threads that find it closed at the same time share the one they open.
		barrier = threading.Barrier(8, timeout=10)
		seen = []

		def ask():
			barrier.wait()
			seen.append(adapter.get_connection("http+docker://ssh/v1.45/version"))

		threads = [threading.Thread(target=ask) for _ in range(8)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(10)
		assert len(seen) == 8 and len({id(pool) for pool in seen}) == 1, seen
		assert seen[0] is not reopened and seen[0].pool is not None
	finally:
		without_paramiko(state)


def test_the_ssh_pipe_waits_only_when_nothing_is_buffered():
	"""
	The reader under every ssh:// response: it gives up at the timeout as a
	socket would, but only when it has to go to the pipe. What is already
	buffered comes back at once however short the timeout, None waits for as
	long as it takes, and the end of the pipe is the end, not a timeout.
	"""
	import io
	import socket
	import threading

	read_end, write_end = os.pipe()
	stdout = os.fdopen(read_end, "rb")
	timeout = [0.2]
	reader = io.BufferedReader(host_registry._TimedPipe(stdout, lambda: timeout[0]))
	try:
		started = time.monotonic()
		try:
			reader.read(1)
			raise AssertionError("debería agotar el plazo")
		except socket.timeout:
			pass
		assert 0.15 < time.monotonic() - started < 2, time.monotonic() - started

		os.write(write_end, b"HTTP/1.1 200 OK\r\nrest")
		assert reader.readline() == b"HTTP/1.1 200 OK\r\n"
		timeout[0] = 0
		assert reader.read(4) == b"rest", "lo ya leído no debe esperar a la tubería"

		timeout[0] = None
		threading.Timer(0.3, lambda: os.write(write_end, b"late")).start()
		assert reader.read(4) == b"late"

		timeout[0] = 5
		os.close(write_end)
		write_end = None
		assert reader.read(1) == b""

		# The descriptor is the process's: closing the reader leaves it open.
		reader.close()
		os.fstat(read_end)
	finally:
		stdout.close()
		if write_end is not None:
			os.close(write_end)


# A stand-in for `ssh … docker system dial-stdio`. Whatever host it is asked
# for, it speaks just enough HTTP on stdin and stdout for the SDK and answers
# each path the way the tests need. Every process writes its pid down, so a
# test can tell a reused connection from a new one.
FAKE_SSH = r'''
import os
import sys
import time

with open(os.environ["FAKE_SSH_LOG"], "a") as log:
	log.write(f"{os.getpid()}\n")
delay = float(os.environ["FAKE_SSH_DELAY"])
incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer


def answer(body, content_type=b"text/plain"):
	outgoing.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type
				   + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
	outgoing.flush()


while True:
	line = incoming.readline()
	if not line:
		break
	if not line.strip():
		# What the SDK writes before ending the process.
		continue
	path = line.split(b" ")[1]
	length = 0
	while True:
		header = incoming.readline()
		if header in (b"\r\n", b"\n", b""):
			break
		if header.lower().startswith(b"content-length:"):
			length = int(header.split(b":", 1)[1])
	incoming.read(length)
	if path.endswith(b"/late"):
		time.sleep(delay)
		answer(b"late")
	elif path.endswith(b"/stream"):
		# An exec's raw stream, upgraded as Docker does it: part of it with the
		# headers, the rest after a pause, and then the end.
		outgoing.write(b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
					   b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\nearly")
		outgoing.flush()
		time.sleep(delay)
		outgoing.write(b"late!")
		outgoing.flush()
		break
	else:
		answer(b"OK")
'''


class FakeSsh:
	"""FAKE_SSH first on the PATH as `ssh`, while the block lasts."""

	def __init__(self, delay):
		self.delay = delay

	def __enter__(self):
		import tempfile
		self.root = tempfile.mkdtemp()
		script = os.path.join(self.root, "fake_ssh.py")
		with open(script, "w") as f:
			f.write(FAKE_SSH)
		ssh = os.path.join(self.root, "ssh")
		with open(ssh, "w") as f:
			f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
		os.chmod(ssh, 0o755)
		self.log = os.path.join(self.root, "pids")
		self.saved = {name: os.environ.get(name) for name in ("PATH", "FAKE_SSH_LOG", "FAKE_SSH_DELAY")}
		os.environ["PATH"] = self.root + os.pathsep + os.environ.get("PATH", "")
		os.environ["FAKE_SSH_LOG"] = self.log
		os.environ["FAKE_SSH_DELAY"] = str(self.delay)
		self.paramiko = with_paramiko()
		# The SDK's client module takes the ssh adapter only if paramiko was
		# there when it was first imported; on a laptop without it, it is
		# handed over here, and taken back afterwards.
		import docker.api.client
		from docker.transport.sshconn import SSHHTTPAdapter
		self.lent_adapter = not hasattr(docker.api.client, "SSHHTTPAdapter")
		if self.lent_adapter:
			docker.api.client.SSHHTTPAdapter = SSHHTTPAdapter
		return self

	def processes(self):
		"""How many ssh processes have been started."""
		try:
			with open(self.log) as f:
				return len(f.read().split())
		except FileNotFoundError:
			return 0

	def client(self, timeout):
		"""
		The SDK's own client over it, as _build_client makes one. The class
		itself, not docker.DockerClient: other modules stub that one out.
		"""
		from docker.client import DockerClient
		client = DockerClient(base_url="ssh://root@nas", use_ssh_client=True, version="1.41", timeout=timeout)
		assert host_registry._reuse_ssh_connections(client, "h_nas") is True
		return client

	@staticmethod
	def connect(client):
		"""
		Starts the connection with time to spare: starting the stand-in is an
		interpreter starting, which the short timeouts under test would
		otherwise have to cover as well.
		"""
		assert client.api._get(client.api._url("/_ping"), timeout=10).text == "OK"

	def __exit__(self, *exc):
		import shutil
		import docker.api.client
		if self.lent_adapter:
			del docker.api.client.SSHHTTPAdapter
		for name, value in self.saved.items():
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value
		without_paramiko(self.paramiko)
		shutil.rmtree(self.root, ignore_errors=True)
		return False


def test_an_ssh_read_gives_up_at_its_timeout_and_the_connection_is_not_reused():
	"""
	The SDK read an ssh:// response from the process's stdout with no deadline:
	the timeout landed on a socket nothing reads, so a daemon that stopped
	answering with sshd still up held the thread for good.

	Giving up has to throw the connection away as well. The late answer is
	still on its way down that pipe, and the next request would read it as its
	own.
	"""
	import requests
	import urllib3

	with FakeSsh(delay=1.5) as ssh:
		client = ssh.client(timeout=0.3)
		try:
			ssh.connect(client)
			started = time.monotonic()
			try:
				client.api._get(client.api._url("/late"))
				raise AssertionError("debería haberse rendido al agotar el plazo")
			except requests.exceptions.ReadTimeout as e:
				assert isinstance(e.args[0], urllib3.exceptions.ReadTimeoutError), repr(e.args[0])
			assert time.monotonic() - started < 1.2, "esperó a la respuesta en vez de rendirse"

			# Answered by a new process, not handed the late "late".
			ssh.connect(client)
			assert ssh.processes() == 2, ssh.processes()
		finally:
			client.close()


def test_an_ssh_read_without_a_timeout_waits_for_as_long_as_it_takes():
	"""What the event stream asks for, by passing None, and has to keep getting."""
	with FakeSsh(delay=0.8) as ssh:
		client = ssh.client(timeout=0.3)
		try:
			ssh.connect(client)
			# The same connection serves a request with the short timeout first,
			# so a deadline left behind on it would show here.
			assert client.ping() is True
			assert client.api._get(client.api._url("/late"), timeout=None).text == "late"
			assert ssh.processes() == 1
		finally:
			client.close()


def test_the_raw_stream_of_an_exec_over_ssh_is_read_as_over_a_socket():
	"""
	An exec's output is read from the connection itself once the headers are
	in. It has to come through the same buffer the headers did, or whatever
	arrived along with them is lost; and it waits with no deadline, as the SDK
	does over a socket, where the timeout never bounded an exec.
	"""
	with FakeSsh(delay=0.6) as ssh:
		client = ssh.client(timeout=0.2)
		try:
			ssh.connect(client)
			response = client.api._post(client.api._url("/stream"), stream=True)
			channel = client.api._get_raw_response_socket(response)
			assert channel.gettimeout() == 0.2
			assert channel.recv(5) == b"early"
			assert channel.recv(5) == b"late!"
			assert channel.recv(5) == b""
			response.close()
		finally:
			client.close()


def test_an_ssh_client_still_answers_after_it_was_closed():
	"""
	The whole way through the SDK: closing is what drop() does to a failing
	host's clients while an update may still be holding one, and that update
	has to be able to finish, or at least roll itself back.
	"""
	with FakeSsh(delay=0) as ssh:
		client = ssh.client(timeout=5)
		try:
			assert client.ping() is True
			client.close()
			assert client.ping() is True
			assert ssh.processes() == 2, ssh.processes()
		finally:
			client.close()
