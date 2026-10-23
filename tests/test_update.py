"""
The container update and its rollback.

Runs against an in-memory Docker that enforces what the real one does about
names: two containers cannot share one, and a rename onto a taken name fails
with a conflict. That is exactly where the rollback used to go wrong — it
looked the old container up by name, and a leftover `<name>_old` made it
delete the real container and put the leftover in its place.
"""

import os
import sys
import types
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

if harness.REPO not in sys.path:
	sys.path.insert(0, harness.REPO)

import docker.errors
import docker_update

# The in-memory Docker never crashes on its own, so there is nothing to wait
# for; test_real_docker exercises the wait itself.
docker_update.VERIFY_STABLE_SECONDS = 0


class FakeContainer:
	def __init__(self, engine, name, status="running", role=None):
		self.engine = engine
		self.id = f"{len(engine.containers) + 1:064x}"
		self.name = name
		self.status = status
		self.role = role
		self.attrs = {}
		self.image = type("Image", (), {"id": "sha256:old"})()

	def stop(self, timeout=None):
		self.status = "exited"

	def start(self):
		if self.engine.fail_start and self.role == "new":
			raise Exception("start failed")
		if self.status == "paused":
			raise docker.errors.APIError("cannot start a paused container, try unpause instead")
		self.status = "running"

	def pause(self):
		self.status = "paused"

	def rename(self, name):
		if any(c.name == name and c is not self for c in self.engine.containers.values()):
			raise docker.errors.APIError(f'Conflict. The container name "/{name}" is already in use')
		self.name = name

	def remove(self, force=False):
		if self.role == "original" and self.engine.fail_remove:
			# (what the client gets, whether the daemon deletes it anyway)
			error, deleted = self.engine.fail_remove
			if deleted:
				self.engine.containers.pop(self.id, None)
			raise error
		self.engine.containers.pop(self.id, None)

	def reload(self):
		if self.id not in self.engine.containers:
			raise docker.errors.NotFound("gone")

	def logs(self, tail=50):
		return b""


class FakeContainers:
	def __init__(self, engine):
		self.engine = engine

	def get(self, key):
		for container in self.engine.containers.values():
			if container.id == key or container.name == key:
				return container
		raise docker.errors.NotFound(key)

	def create(self, image, name=None, **kwargs):
		if self.engine.on_create:
			self.engine.on_create()
		if self.engine.fail_create:
			raise Exception("create failed")
		self.engine.created = kwargs
		return self.engine.add(name, status="created", role="new")


class FakeImages:
	def __init__(self, engine):
		self.engine = engine

	def get(self, reference):
		self.engine.looked_up.append(reference)
		return type("Image", (), {"id": "sha256:new"})()

	def remove(self, image_id):
		self.engine.removed_images.append(image_id)


class FakeDocker:
	def __init__(self):
		self.containers = {}
		self.pulled = False
		self.pulls = []
		# What the daemon streams back for a pull.
		self.pull_stream = [{"status": "Pulling from library/app"}, {"status": "Status: Image is up to date"}]
		self.looked_up = []
		self.removed_images = []
		self.created = None
		self.fail_create = False
		self.fail_start = False
		self.fail_remove = None
		self.on_create = None
		# The low-level client: the pull, and the API version it negotiated.
		self.api = self
		self._version = "1.47"

	def pull(self, repository, tag=None, platform=None, stream=False, decode=False):
		self.pulled = True
		self.pulls.append((repository, tag, platform))
		return iter(self.pull_stream)

	def add(self, name, status="running", role=None):
		container = FakeContainer(self, name, status, role)
		self.containers[container.id] = container
		return container

	def named(self, name):
		return [c for c in self.containers.values() if c.name == name]

	@property
	def client(self):
		client = type("Client", (), {})()
		client.api = self.api
		client.containers = FakeContainers(self)
		client.images = FakeImages(self)
		client.networks = None
		return client


def _config():
	config = defaultdict(lambda: None)
	config.update(image="repo/app:latest", is_running=True, networks={})
	return config


def _update(engine, container, config=None, **overrides):
	arguments = dict(
		client=engine.client, container=container, config=config if config is not None else _config(),
		container_name=container.name, message=None,
		edit_message_func=lambda *a, **k: None, debug_func=lambda m: None,
		error_func=lambda m: None, get_text_func=lambda key, *a: key,
		save_status_func=lambda *a: None, container_id_length=12, telegram_group=0)
	arguments.update(overrides)
	return docker_update.perform_update(**arguments)


def _with_values(key, *values):
	"""A get_text that shows what it was handed."""
	return " ".join((key,) + tuple(str(value) for value in values))


def test_a_leftover_old_container_stops_the_update_before_anything_is_touched():
	"""
	The bug this file exists for. The leftover may be the copy the user needs
	to recover, so it is not deleted either: the update refuses and says why.
	"""
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	leftover = engine.add("nginx_old", status="exited", role="leftover")

	result = _update(engine, original)

	assert result == "error_update_leftover_old", result
	assert not engine.pulled, "se descargó la imagen antes de rendirse"
	assert original.id in engine.containers and original.name == "nginx"
	assert original.status == "running", "el original se quedó parado"
	assert leftover.id in engine.containers and leftover.name == "nginx_old"


def test_a_failed_create_puts_the_original_back_under_its_name():
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.fail_create = True

	assert _update(engine, original) == "error_updating_container"
	assert [c.role for c in engine.named("nginx")] == ["original"]
	assert original.status == "running"
	assert engine.named("nginx_old") == []


def test_a_failed_start_removes_only_the_new_container():
	"""The new one holds the name when it fails, and it is the only thing the rollback may delete."""
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.fail_start = True

	assert _update(engine, original) == "error_updating_container"
	assert [c.role for c in engine.containers.values()] == ["original"]
	assert original.name == "nginx" and original.status == "running"


def test_the_rollback_never_deletes_a_container_it_did_not_create():
	"""
	Someone else took the name while the update ran. The old rollback removed
	whatever was called that; now the stranger stays, and so does the original,
	still under _old, for the user to sort out.
	"""
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.on_create = lambda: engine.add("nginx", role="stranger")
	engine.fail_create = True

	assert _update(engine, original) == "error_updating_container"
	roles = sorted(c.role for c in engine.containers.values())
	assert roles == ["original", "stranger"], roles
	assert original.name == "nginx_old"


def test_a_successful_update_leaves_just_the_new_container():
	engine = FakeDocker()
	original = engine.add("nginx", role="original")

	assert _update(engine, original) == "updated_container"
	assert [c.role for c in engine.containers.values()] == ["new"]
	assert engine.named("nginx")[0].status == "running"


def test_an_original_that_will_not_go_is_left_and_the_new_one_kept():
	"""
	The new container had passed its verification and the original could not
	be deleted: the rollback took that for a failed update and deleted the new
	one, which worked. Now the update stands, and the original is left under
	its _old name with a word to the user — with the success text unchanged,
	since that is how the caller tells success from failure.
	"""
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.fail_remove = (docker.errors.APIError("driver failed to remove root filesystem: device or resource busy"), False)
	notes = []

	result = _update(engine, original, get_text_func=_with_values, notify_func=notes.append)

	assert result == "updated_container nginx", result
	new = engine.named("nginx")
	assert [c.role for c in new] == ["new"] and new[0].status == "running", "se borró el nuevo"
	assert original.id in engine.containers and original.name == "nginx_old"
	assert notes == ["update_old_left_behind nginx nginx_old"], notes
	assert engine.removed_images == [], "se intentó borrar la imagen que el viejo sigue usando"

	# Without anyone to tell, it is the same update.
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.fail_remove = (docker.errors.APIError("device or resource busy"), False)
	assert _update(engine, original) == "updated_container"
	assert sorted(c.role for c in engine.containers.values()) == ["new", "original"]


def test_a_delete_that_times_out_but_happens_is_a_plain_update():
	"""
	A ReadTimeout is not an APIError, and on a slow disk the daemon goes on to
	delete the original after the client gave up. The rollback then deleted
	the new container and found no original to restore: nothing was left.
	"""
	import requests
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.fail_remove = (requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)"), True)
	notes = []

	assert _update(engine, original, notify_func=notes.append) == "updated_container"
	assert [c.role for c in engine.containers.values()] == ["new"]
	assert engine.named("nginx")[0].status == "running"
	assert notes == [], notes


def test_nothing_after_the_verification_rolls_the_update_back():
	"""Housekeeping that fails is reported; the container that works stays."""
	engine = FakeDocker()
	original = engine.add("nginx", role="original")

	def save_status(*args):
		raise OSError("No space left on device")

	assert _update(engine, original, save_status_func=save_status) == "updated_container"
	assert [c.role for c in engine.containers.values()] == ["new"]

	# Something fails before the original is even deleted: it is still there,
	# and said so.
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	message = type("Message", (), {"message_id": 1})()
	notes = []

	def edit(text, *args):
		if text == "updating_deleting_old":
			raise Exception("Too Many Requests: retry after 5")

	assert _update(engine, original, message=message, edit_message_func=edit, notify_func=notes.append) == "updated_container"
	assert engine.named("nginx")[0].role == "new" and engine.named("nginx")[0].status == "running"
	assert original.name == "nginx_old" and notes == ["update_old_left_behind"], notes


def test_a_paused_container_rolls_back_paused():
	"""
	It came back running. And when the pull was what failed, the original had
	not even been stopped: start() on a paused container fails, and a rollback
	that had touched nothing was logged as failed.
	"""
	for failure in ("pull", "create"):
		engine = FakeDocker()
		original = engine.add("vpn", status="paused", role="original")
		if failure == "pull":
			engine.pull_stream = [{"error": "manifest unknown", "errorDetail": {"message": "manifest unknown"}}]
		else:
			engine.fail_create = True
		config = _config()
		config.update(was_paused=True)
		debug, errors = [], []

		assert _update(engine, original, config, debug_func=debug.append, error_func=errors.append) == "error_updating_container"
		assert original.name == "vpn" and original.status == "paused", (failure, original.name, original.status)
		assert any("[ROLLBACK_RESULT] ✅" in line for line in debug), (failure, errors)
		assert not any("ROLLBACK_STEP2] ❌" in line for line in errors), (failure, errors)


def test_an_anonymous_volume_declared_as_a_mount_comes_back_with_its_data():
	"""
	`volumes: - /var/lib/mysql` in compose, or `--mount type=volume,dst=…`, is
	a Mount with no Source: handed back as it was, Docker made a new, empty
	volume and mariadb initialised a fresh database. The new container gets
	the old one by name; the compose generator still sees `- /var/lib/mysql`.
	"""
	engine = FakeDocker()
	original = engine.add("db", role="original")
	anonymous = "a" * 64
	original.attrs = {
		"Config": {"Image": "mariadb:11"},
		"HostConfig": {"Mounts": [
			{"Type": "volume", "Target": "/var/lib/mysql"},
			{"Type": "volume", "Source": "backups", "Target": "/backup", "ReadOnly": True},
			{"Type": "bind", "Source": "/srv/db/conf", "Target": "/etc/mysql/conf.d"}]},
		"Mounts": [
			{"Type": "volume", "Name": anonymous, "Destination": "/var/lib/mysql", "RW": True},
			{"Type": "volume", "Name": "backups", "Destination": "/backup", "RW": False},
			{"Type": "bind", "Source": "/srv/db/conf", "Destination": "/etc/mysql/conf.d", "RW": True}]}
	config = docker_update.extract_container_config(original)

	assert _update(engine, original, config) == "updated_container"
	assert engine.created["mounts"] == [
		{"Type": "volume", "Source": anonymous, "Target": "/var/lib/mysql"},
		{"Type": "volume", "Source": "backups", "Target": "/backup", "ReadOnly": True},
		{"Type": "bind", "Source": "/srv/db/conf", "Target": "/etc/mysql/conf.d"}], engine.created["mounts"]
	assert config["mounts_list"][0] == {"Type": "volume", "Target": "/var/lib/mysql"}, "el compose escribiría el nombre aleatorio"
	# Once, as a mount: not a second time as an anonymous `-v`.
	assert config["anonymous_volumes"] == [], config["anonymous_volumes"]


def test_a_fixed_mac_is_kept_on_daemons_before_api_1_44():
	"""
	It was read from HostConfig, which has no MacAddress, so only the
	endpoint's survived: and daemons before Docker 25 ignore that one. On
	those it goes in as the container's; on newer ones it stays per endpoint.
	"""
	mac = "02:42:c0:a8:01:63"
	for version, container_wide in (("1.41", mac), ("1.43", mac), ("1.44", None), ("1.47", None)):
		engine = FakeDocker()
		engine._version = version
		original = engine.add("pihole", role="original")
		original.attrs = {
			"Config": {"Image": "pihole/pihole:latest", "MacAddress": mac},
			"HostConfig": {"NetworkMode": "lan"},
			"NetworkSettings": {"Networks": {"lan": {"IPAMConfig": {"IPv4Address": "192.168.1.10"}, "MacAddress": mac}}}}
		config = docker_update.extract_container_config(original)
		assert config["mac_address"] == mac, config["mac_address"]

		assert _update(engine, original, config) == "updated_container"
		assert engine.created["mac_address"] == container_wide, (version, engine.created["mac_address"])
		assert engine.created["networking_config"]["lan"]["MacAddress"] == mac, version

	# Sharing another container's network, it has none of its own to keep.
	engine = FakeDocker()
	engine._version = "1.43"
	original = engine.add("app", role="original")
	original.attrs = {"Config": {"Image": "app", "MacAddress": mac}, "HostConfig": {"NetworkMode": "container:" + "b" * 64}}
	config = docker_update.extract_container_config(original)
	assert config["mac_address"] is None
	assert _update(engine, original, config) == "updated_container"
	assert engine.created["mac_address"] is None


def test_a_healthchecks_start_interval_reaches_the_daemon():
	"""
	The SDK rebuilds a healthcheck from the fields it knows, and
	`start_interval` is not one of them. Through the real SDK, with only the
	HTTP request caught.
	"""
	api = docker.APIClient(base_url="tcp://127.0.0.1:9", version="1.44")
	sent = []

	class Response:
		def raise_for_status(self):
			pass

		def json(self):
			return {"Id": "f" * 64}

	def post_json(url, data=None, params=None, **kwargs):
		sent.append((url, data, params))
		return Response()

	api._post_json = post_json
	client = type("Client", (), {})()
	client.api = api
	client.containers = type("Containers", (), {"get": staticmethod(lambda key: key)})()
	healthcheck = {"Test": ["CMD-SHELL", "curl -f localhost"], "Interval": 30_000_000_000, "Timeout": 5_000_000_000,
				   "Retries": 3, "StartPeriod": 60_000_000_000, "StartInterval": 2_000_000_000}

	created = docker_update._create_container(client, None, None, "app:1.0", name="app", healthcheck=dict(healthcheck),
											  environment=["A=1"], labels={"x": "y"})

	assert created == "f" * 64
	url, data, params = sent[0]
	assert url.endswith("/containers/create") and params["name"] == "app", (url, params)
	assert data["Healthcheck"] == healthcheck, data["Healthcheck"]
	assert data["Image"] == "app:1.0" and "A=1" in data["Env"] and data["Labels"] == {"x": "y"}, data


def test_a_pull_that_fails_halfway_is_a_failed_pull():
	"""
	The daemon says 200 when a pull starts and sends what goes wrong after
	that inside the stream; the SDK returned the image already there as if
	it were the new one.
	"""
	engine = FakeDocker()
	engine.pull_stream = [
		{"status": "Pulling from library/nginx"},
		{"errorDetail": {"message": "write /var/lib/docker/tmp/x: no space left on device"},
		 "error": "write /var/lib/docker/tmp/x: no space left on device"},
	]
	try:
		docker_update.pull_checked(engine.client, "nginx:latest")
	except docker.errors.APIError as error:
		assert "no space left on device" in str(error), str(error)
	else:
		raise AssertionError("el pull fallido devolvió una imagen")
	assert engine.looked_up == []

	engine.pull_stream = [{"errorDetail": {"message": "unauthorized"}}]
	try:
		docker_update.pull_checked(engine.client, "nginx:latest")
	except docker.errors.APIError as error:
		assert "unauthorized" in str(error), str(error)
	else:
		raise AssertionError("un errorDetail sin error pasó por bueno")

	# In an update, it fails before the original is touched.
	engine = FakeDocker()
	original = engine.add("nginx", role="original")
	engine.pull_stream = [{"error": "no space left on device"}]
	assert _update(engine, original) == "error_updating_container"
	assert [c.role for c in engine.containers.values()] == ["original"]
	assert original.name == "nginx" and original.status == "running"


def test_a_checked_pull_returns_the_image_it_pulled():
	"""
	As images.pull() does, by digest when there is one: `repo:tag@sha256:…`
	is pulled, and kept, by its digest alone.
	"""
	a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
	cases = {
		"nginx": (("nginx", "latest"), "nginx:latest"),
		"nginx:1.27": (("nginx", "1.27"), "nginx:1.27"),
		"nas:5000/app": (("nas:5000/app", "latest"), "nas:5000/app:latest"),
		"lscr.io/linuxserver/plex:latest": (("lscr.io/linuxserver/plex", "latest"), "lscr.io/linuxserver/plex:latest"),
		f"nginx@{a}": (("nginx", a), f"nginx@{a}"),
		f"nas:5000/app:1.0@{b}": (("nas:5000/app", b), f"nas:5000/app@{b}"),
	}
	for reference, ((repository, tag), looked_up) in cases.items():
		engine = FakeDocker()
		image = docker_update.pull_checked(engine.client, reference, platform="linux/arm64")
		assert image.id == "sha256:new"
		assert engine.pulls == [(repository, tag, "linux/arm64")], (reference, engine.pulls)
		assert engine.looked_up == [looked_up], (reference, engine.looked_up)


def test_a_container_is_stopped_with_the_grace_period_it_asked_for():
	"""
	With no `t`, the daemon waits the container's own StopTimeout but the
	client gives up after its request timeout: 60 s in 4.x, 30 s now. A
	`stop_grace_period: 60s` came back as a timeout halfway through, and the
	update reported failure on a container left stopping.
	"""
	from unittest.mock import MagicMock
	import docker_update

	for config, expected in (({"StopTimeout": 60}, 60), ({}, 10), ({"StopTimeout": None}, 10),
							({"StopTimeout": 0}, 0), ({"StopTimeout": -1}, 10)):
		container = MagicMock()
		container.attrs = {"Config": config}
		docker_update.stop_container(container)
		container.stop.assert_called_once_with(timeout=expected)


def test_nothing_stops_a_container_behind_the_helpers_back():
	"""A bare stop() comes back on the 30 s client timeout; one with t=10 cuts a longer grace short."""
	import re
	offenders = []
	for name in ("core.py", "docker_update.py", "docker_compose_manager.py", "callbacks.py"):
		with open(os.path.join(harness.REPO, name), encoding="utf-8") as f:
			for number, line in enumerate(f, 1):
				if re.search(r"\bcontainer\.stop\(", line) and "def stop_container" not in line:
					offenders.append(f"{name}:{number}: {line.strip()}")
	# The helper itself. The rollback stops `new_container`, created a moment
	# ago with no grace period of its own, and does not match.
	allowed = ("container.stop(timeout=grace)",)
	offenders = [o for o in offenders if not o.endswith(allowed)]
	assert not offenders, "\n".join(offenders)


def test_an_update_in_progress_is_claimed_and_then_released():
	"""
	A lock per container id was kept for good, and every update gives the
	container a new id: one more entry each time, never one fewer.
	"""
	from unittest.mock import MagicMock
	container = MagicMock()
	container.id = "a" * 64
	seen = []
	original = docker_update._perform_update_locked

	def locked(*args, **kwargs):
		# While this one runs, a second update of the same container is refused.
		seen.append(docker_update.perform_update(None, container, {}, "plex", None, None,
			lambda *a: None, lambda *a: None, None, None, 5, None))
		return "ok"

	docker_update._perform_update_locked = locked
	try:
		result = docker_update.perform_update(None, container, {}, "plex", None, None,
			lambda *a: None, lambda *a: None, None, None, 5, None)
		assert result == "ok", result
		assert "already being updated" in seen[0], seen
		assert docker_update._updating == set(), docker_update._updating
	finally:
		docker_update._perform_update_locked = original


def test_the_repository_of_an_image_keeps_the_registrys_port():
	"""
	`nas:5000/app:latest` split on the first colon is `nas`, and a /changetag
	to v2 then pulled `nas:v2`. Found by test_real_docker, whose registry is
	on localhost:55000.
	"""
	cases = {
		"nginx": "nginx",
		"nginx:1.27": "nginx",
		"lscr.io/linuxserver/plex:latest": "lscr.io/linuxserver/plex",
		"nas:5000/app": "nas:5000/app",
		"nas:5000/app:latest": "nas:5000/app",
		"localhost:55000/team/app:v2": "localhost:55000/team/app",
		"nginx@sha256:" + "a" * 64: "nginx",
		"nas:5000/app:1.0@sha256:" + "b" * 64: "nas:5000/app",
	}
	for reference, expected in cases.items():
		assert docker_update.image_repository(reference) == expected, (reference, docker_update.image_repository(reference))


def test_an_image_says_its_version_by_label_or_by_its_own_variable():
	"""
	The OCI label first — most images set it. Docker's official images do
	not, and say it in NGINX_VERSION or PG_VERSION; but only the variable
	named after the image counts, not GOSU_VERSION or NODE_VERSION.
	"""
	version = docker_update.image_version
	assert version({"Labels": {"org.opencontainers.image.version": "1.43.4-ls326"}}) == "1.43.4-ls326"
	assert version({"Labels": {"org.label-schema.version": "2.1"}}) == "2.1"
	assert version({"Env": ["NJS_VERSION=1.0.1", "NGINX_VERSION=1.31.6"]}, "nginx") == "1.31.6"
	assert version({"Env": ["GOSU_VERSION=1.19", "PG_VERSION=16.15-1"]}, "library/postgres") == "16.15-1"
	assert version({"Env": ["NODE_VERSION=24.21.0"]}, "immich-app/immich-server") is None
	assert version({"Env": ["NGINX_VERSION=1.31.6"]}) is None, "sin repositorio no se adivina"
	assert version({}) is None and version(None) is None
	assert version({"Labels": {"org.opencontainers.image.version": "  "}}) is None


def test_a_major_upgrade_is_one_whose_first_number_goes_up():
	major = docker_update.is_major_upgrade
	assert major("1.43.3", "2.0.0") and major("v0.107.79", "v1.0.0")
	assert major("5.2.4_v2.0.15-ls479", "6.0.0-ls480")
	assert not major("1.43.3", "1.44.0") and not major("2.0", "2.0")
	assert not major("2.0", "1.9"), "bajar no es subir"
	# Calendar versions change their first number every January.
	assert not major("2026.12.3", "2027.1.0")
	assert not major("latest", "2.0") and not major(None, "2.0")


def test_image_metadata_labels_are_left_to_the_new_image():
	"""Whatever their value: a stale one pinned by 4.x matches no image any more."""
	container = FakeDocker().add("duck")
	container.attrs = {"Config": {"Labels": {
		"org.opencontainers.image.version": "c1012ade-ls54",
		"org.label-schema.build-date": "2024-01-01",
		"traefik.enable": "true"}}, "HostConfig": {}}
	labels = docker_update.extract_container_config(container)["labels"]
	assert labels == {"traefik.enable": "true"}, labels


def test_a_dcb_label_set_to_false_is_off():
	from config import label_enabled, LABEL_AUTO_UPDATE
	assert label_enabled({LABEL_AUTO_UPDATE: ""}, LABEL_AUTO_UPDATE)
	assert label_enabled({LABEL_AUTO_UPDATE: "true"}, LABEL_AUTO_UPDATE)
	for off in ("false", "False", " no ", "0", "off"):
		assert not label_enabled({LABEL_AUTO_UPDATE: off}, LABEL_AUTO_UPDATE), off
	assert not label_enabled({}, LABEL_AUTO_UPDATE)
	assert not label_enabled(None, LABEL_AUTO_UPDATE)


class _NetworkedDocker(FakeDocker):
	"""A FakeDocker whose networks can be connected to, and whose new container
	says which networks it was created on, as the daemon does."""

	def __init__(self, version):
		super().__init__()
		self._version = version
		self.connected = []
		# The daemon reports a network given by id under its name.
		self.network_names = {}

	def create_endpoint_config(self, *args, **kwargs):
		import docker.types
		return docker.types.EndpointConfig(self._version, *args, **kwargs)

	@property
	def client(self):
		client = super().client
		engine = self

		class Network:
			def __init__(self, name):
				self.name, self.id = name, name

			def connect(self, container_id, **kwargs):
				engine.connected.append((self.name, kwargs))

		client.networks = type("Networks", (), {"get": staticmethod(lambda name: Network(name))})()
		original_create = client.containers.create

		def create(image, name=None, **kwargs):
			container = original_create(image, name=name, **kwargs)
			container.attrs = {"NetworkSettings": {"Networks": {
				engine.network_names.get(net, net): {}
				for net in (kwargs.get("networking_config") or {kwargs.get("network_mode"): {}})}}}
			return container
		client.containers.create = create
		return client


_LAN_ID = "4c414e" + "0" * 58


def _two_networks(engine, hand_mac=None, stopped=False, main_by_id=False):
	original = engine.add("pihole", role="original")
	# Read with the client the update uses, and its API version.
	original.client = engine.client
	config = {"Image": "pihole/pihole:latest"}
	# As Docker 25 to 28 report it: only when the MAC was set by hand, and
	# only when the network mode names the network.
	if hand_mac and not main_by_id:
		config["MacAddress"] = hand_mac
	if main_by_id:
		engine.network_names[_LAN_ID] = "lan"
	# Stopped after running, a container reports no MAC on its endpoints.
	lan_mac = "" if stopped else hand_mac or "16:bc:22:09:71:2e"
	iot_mac = "" if stopped else "02:42:ac:22:00:02"
	original.attrs = {
		"Config": config,
		# `--network <id>` and Compose 2.29 leave the id here; the endpoints
		# go by name.
		"HostConfig": {"NetworkMode": _LAN_ID if main_by_id else "lan"},
		"NetworkSettings": {"Networks": {
			"lan": {"NetworkID": _LAN_ID, "IPAMConfig": {"IPv4Address": "192.168.1.10"},
					"IPAddress": "" if stopped else "192.168.1.10", "MacAddress": lan_mac},
			"iot": {"NetworkID": "b" * 64, "IPAMConfig": {"IPv4Address": "10.0.0.5"},
					"IPAddress": "" if stopped else "10.0.0.5", "MacAddress": iot_mac, "Aliases": ["dns"]}}}}
	return original, docker_update.extract_container_config(original)


def test_every_network_goes_in_at_create_from_api_1_44():
	"""
	Connected afterwards, Docker 25 to 28 ignore the MAC asked for: one set by
	hand on a second network —a DHCP reservation— came back different from
	every update. Created on all of them at once, as compose does, it stays.
	Before 1.44 a create takes a single network, and the rest are connected.
	"""
	for version, at_create in (("1.43", False), ("1.44", True), ("1.48", True), ("1.54", True)):
		engine = _NetworkedDocker(version)
		original, config = _two_networks(engine, hand_mac="02:42:c0:a8:01:63")
		assert _update(engine, original, config) == "updated_container", version
		created = engine.created["networking_config"]
		if at_create:
			assert set(created) == {"lan", "iot"}, (version, created)
			assert created["iot"]["MacAddress"] == "02:42:ac:22:00:02", (version, created["iot"])
			assert created["iot"]["IPAMConfig"]["IPv4Address"] == "10.0.0.5" and created["iot"]["Aliases"] == ["dns"]
			assert engine.connected == [], "la ha conectado otra vez"
		else:
			assert set(created) == {"lan"}, (version, created)
			assert [name for name, _ in engine.connected] == ["iot"], engine.connected
			assert engine.connected[0][1]["mac_address"] == "02:42:ac:22:00:02"


def test_a_mac_docker_assigned_is_not_pinned_where_it_would_read_as_set_by_hand():
	"""
	Docker 25 to 28 copy any MAC given at create into Config.MacAddress,
	where it reads as set by hand, and /compose wrote out a `mac_address`
	nobody set. There that field says which it was, and one Docker assigned
	is left to Docker. Set by hand, or from Docker 29, it stays.
	"""
	for hand_mac, version, kept in ((None, "1.44", None),                         # assigned, Docker 25
									(None, "1.51", None),                         # assigned, Docker 28
									("02:42:c0:a8:01:63", "1.48", "02:42:c0:a8:01:63"),  # by hand, Docker 28
									(None, "1.52", "16:bc:22:09:71:2e"),          # Docker 29: kept
									(None, "1.54", "16:bc:22:09:71:2e")):
		engine = _NetworkedDocker(version)
		original, config = _two_networks(engine, hand_mac=hand_mac)
		assert _update(engine, original, config) == "updated_container"
		assert engine.created["networking_config"]["lan"].get("MacAddress") == kept, (hand_mac, version)
		# The second network's is kept either way: /compose never writes those.
		assert engine.created["networking_config"]["iot"]["MacAddress"] == "02:42:ac:22:00:02"


def test_a_stopped_container_keeps_the_mac_set_by_hand_on_its_main_network():
	"""
	Stopped, a container reports no MAC on its endpoints. With a second
	network it was then created with none on the main one, and the MAC set
	by hand —still in Config.MacAddress— was lost on the first update.
	"""
	for version in ("1.44", "1.48", "1.51"):
		engine = _NetworkedDocker(version)
		original, config = _two_networks(engine, hand_mac="02:42:c0:a8:01:63", stopped=True)
		assert _update(engine, original, config) == "updated_container", version
		assert engine.created["networking_config"]["lan"].get("MacAddress") == "02:42:c0:a8:01:63", version
		assert "MacAddress" not in engine.created["networking_config"]["iot"], version


def test_a_main_network_given_by_its_id_keeps_its_settings():
	"""
	`docker run --network <id>`, and Compose 2.29, leave the id as
	NetworkMode while the endpoints are listed by name. The main network's
	IP and MAC were looked for under the id and not found, and the network
	went in as one more.

	Docker 25 to 28 then leave Config.MacAddress empty even for a MAC set by
	hand, which the update took for one Docker assigned, and lost. Created
	again under the id it came with, as it was, and with its MAC.
	"""
	for version in ("1.44", "1.48", "1.51", "1.54"):
		engine = _NetworkedDocker(version)
		original, config = _two_networks(engine, hand_mac="02:42:c0:a8:01:63", main_by_id=True)
		assert config["network_mode"] == "lan", config["network_mode"]
		assert config["ipv4_address"] == "192.168.1.10"
		assert _update(engine, original, config) == "updated_container"
		created = engine.created["networking_config"]
		assert set(created) == {_LAN_ID, "iot"}, (version, created)
		assert engine.created["network_mode"] == _LAN_ID, version
		assert created[_LAN_ID]["IPAMConfig"]["IPv4Address"] == "192.168.1.10"
		assert created[_LAN_ID]["MacAddress"] == "02:42:c0:a8:01:63", (version, created)
		assert created["iot"]["MacAddress"] == "02:42:ac:22:00:02"
		assert engine.connected == []

	# Never started, it reports no network ids: the daemon says which it is.
	engine = _NetworkedDocker("1.48")
	original = engine.add("pihole", role="original")
	original.client = types.SimpleNamespace(api=types.SimpleNamespace(
		inspect_network=lambda network: {"Name": "lan"} if _LAN_ID.startswith(network) else {}))
	settings = {"Networks": {"lan": {"NetworkID": ""}, "iot": {"NetworkID": ""}}}
	assert docker_update._main_network_name(original, {"NetworkMode": _LAN_ID[:12]}, settings) == "lan"
	# A short mode, one not among its networks, or the daemon not knowing it,
	# is left as it came.
	assert docker_update._main_network_name(original, {"NetworkMode": "4c41"}, settings) == "4c41"
	assert docker_update._main_network_name(original, {"NetworkMode": "f" * 12}, settings) == "f" * 12
	assert docker_update._main_network_name(original, {"NetworkMode": "bridge"}, {"Networks": {}}) == "bridge"
	assert docker_update._main_network_name(original, {"NetworkMode": "container:" + "a" * 12}, settings) == "container:" + "a" * 12


def test_link_local_addresses_are_carried_over():
	"""
	Docker reports them only inside IPAMConfig. Read from the endpoint, they
	were always empty, and every update took them off every network.
	"""
	engine = _NetworkedDocker("1.48")
	original, _ = _two_networks(engine)
	networks = original.attrs["NetworkSettings"]["Networks"]
	networks["lan"]["IPAMConfig"]["LinkLocalIPs"] = ["169.254.7.7"]
	networks["iot"]["IPAMConfig"]["LinkLocalIPs"] = ["169.254.8.8"]
	config = docker_update.extract_container_config(original)
	assert config["link_local_ips"] == ["169.254.7.7"]
	assert config["networks"]["iot"]["link_local_ips"] == ["169.254.8.8"]
	assert _update(engine, original, config) == "updated_container"
	created = engine.created["networking_config"]
	assert created["lan"]["IPAMConfig"]["LinkLocalIPs"] == ["169.254.7.7"], created["lan"]
	assert created["iot"]["IPAMConfig"]["LinkLocalIPs"] == ["169.254.8.8"], created["iot"]


def test_docker_25_s_hostname_alias_is_not_carried_over():
	"""
	On API 1.44 the inspect lists the hostname among the aliases of every
	network, without anyone having put it there. Carried over, it was set by
	hand from then on, and /compose wrote it under `aliases:`. Before 1.44
	the hostname is no DNS name, so an alias with its value is somebody's.
	"""
	for version, implicit in (("1.43", False), ("1.44", True), ("1.45", False), ("1.48", False)):
		engine = FakeDocker()
		engine._version = version
		original = engine.add("web", role="original")
		original.client = engine.client
		short_id = original.id[:12]
		original.attrs = {
			"Config": {"Image": "nginx", "Hostname": "myhost"},
			"HostConfig": {"NetworkMode": "lan"},
			"NetworkSettings": {"Networks": {
				"lan": {"Aliases": [short_id, "web", "myhost"]},
				"iot": {"Aliases": [short_id, "myhost"]}}}}
		config = docker_update.extract_container_config(original)
		assert config["hostname"] == "myhost"
		expected = ["web"] if implicit else ["web", "myhost"]
		assert config["network_aliases"] == expected, (version, config["network_aliases"])
		assert config["networks"]["lan"].get("aliases") == expected, version
		assert config["networks"]["iot"].get("aliases") == (None if implicit else ["myhost"]), version


def _inspected_by(version, attrs, old_view=None):
	"""A container whose client agreed `version` with its daemon, which serves
	`old_view` as the inspect of API 1.51."""
	engine = FakeDocker()
	container = engine.add("pihole", role="original")
	container.attrs = attrs
	asked = []

	def get(url):
		asked.append(url)
		if old_view is None:
			raise docker.errors.APIError("client version 1.51 is too old")
		return old_view
	container.client = types.SimpleNamespace(api=types.SimpleNamespace(
		_version=version, base_url="http+docker://localhost", _get=get, _result=lambda response, json=False: response))
	return container, asked


def test_a_mac_docker_made_from_the_address_is_left_to_docker():
	"""
	Docker 25 to 27 make a MAC from the IPv4 address: 02:42 and its four
	octets. Given back at create, it outlived the address: a container
	without a fixed IP came back from the update on another one with the
	old one's MAC, which the next container on that address was given too.
	Docker 28 assigns them at random, and one set by hand is kept.
	"""
	def attrs(main_mac, second_mac):
		return {"Config": {"Image": "pihole/pihole"},
				"HostConfig": {"NetworkMode": _LAN_ID},
				"NetworkSettings": {"Networks": {
					"lan": {"NetworkID": _LAN_ID, "IPAddress": "172.30.1.3", "MacAddress": main_mac},
					"iot": {"NetworkID": "b" * 64, "IPAddress": "172.30.3.3", "MacAddress": second_mac}}}}

	for version, dropped in (("1.43", False), ("1.44", True), ("1.47", True), ("1.48", False), ("1.54", False)):
		container, _ = _inspected_by(version, attrs("02:42:ac:1e:01:03", "02:42:ac:1e:03:03"))
		config = docker_update.extract_container_config(container)
		assert (config["network_mac_address"] is None) == dropped, (version, config["network_mac_address"])
		assert ("mac_address" not in config["networks"]["iot"]) == dropped, (version, config["networks"]["iot"])
	# Set by hand, it is not the address's, and stays.
	container, _ = _inspected_by("1.47", attrs("02:42:c0:a8:01:63", "02:42:ac:1e:03:63"))
	config = docker_update.extract_container_config(container)
	assert config["network_mac_address"] == "02:42:c0:a8:01:63"
	assert config["networks"]["iot"]["mac_address"] == "02:42:ac:1e:03:63"
	assert not docker_update._derived_from_ipv4("02:42:ac:1e:03:03", {"IPAddress": ""})
	assert not docker_update._derived_from_ipv4("", {"IPAddress": "172.30.3.3"})


def test_a_network_docker_lists_twice_goes_in_once():
	"""
	`docker run --network <short id> --ip ...` has Docker list the network
	under that id and under its name. Created again on both, the container
	was attached twice to the same network and would not start.
	"""
	short = _LAN_ID[:12]

	def inspected(mode, others):
		return _inspected_by("1.48", {
			"Config": {"Image": "pihole/pihole"},
			"HostConfig": {"NetworkMode": mode},
			"NetworkSettings": {"Networks": {
				short: {"NetworkID": _LAN_ID, "IPAMConfig": {"IPv4Address": "172.31.1.30"}, "GwPriority": 10},
				"lan": {"NetworkID": _LAN_ID, "IPAMConfig": {"IPv4Address": "172.31.1.30"}, "GwPriority": 10},
				**others}}})[0]

	# On one network the mode stays the short id; on more, Docker gives the
	# full id, which both keys start with.
	for mode, others in ((short, {}), (_LAN_ID, {"iot": {"NetworkID": "b" * 64, "MacAddress": "02:aa:00:00:02:40"}})):
		config = docker_update.extract_container_config(inspected(mode, others))
		assert config["network_mode"] == "lan" and config["network_mode_by_id"] == mode, (mode, config["network_mode"])
		assert set(config["networks"]) == {"lan", *others}, config["networks"]
		assert config["ipv4_address"] == "172.31.1.30"
		assert config["networks"]["lan"]["gw_priority"] == 10
	# Two networks whose names happen to look like ids are both kept.
	assert docker_update._id_named_duplicates({"a" * 12: {"NetworkID": "a" * 64}, "b" * 12: {"NetworkID": "b" * 64}}) == set()


def test_a_stopped_container_on_docker_29_keeps_the_mac_set_by_hand():
	"""
	Stopped, a container reports no MAC on its endpoints, and from API 1.52
	there is no Config.MacAddress either: a stopped Pi-hole on macvlan lost
	its DHCP reservation on the first update. The daemon still says it to a
	client of 1.51.
	"""
	stopped = {"Config": {"Image": "pihole/pihole"}, "HostConfig": {"NetworkMode": "lan"},
			   "NetworkSettings": {"Networks": {"lan": {"NetworkID": _LAN_ID, "MacAddress": ""}}}}
	container, asked = _inspected_by("1.54", stopped, {"Config": {"MacAddress": "02:42:c0:a8:01:63"}})
	config = docker_update.extract_container_config(container)
	assert config["network_mac_address"] == "02:42:c0:a8:01:63"
	assert asked == [f"http+docker://localhost/v1.51/containers/{container.id}/json"], asked
	assert docker_update._primary_mac_to_keep(config, "1.54") == "02:42:c0:a8:01:63"
	# A daemon that no longer serves 1.51, or has nothing to say: as before.
	for old_view in (None, {"Config": {}}):
		container, _ = _inspected_by("1.54", stopped, old_view)
		assert docker_update.extract_container_config(container)["network_mac_address"] is None
	# Nothing to ask before 1.52, nor for a running container.
	container, asked = _inspected_by("1.48", stopped, {"Config": {"MacAddress": "02:42:c0:a8:01:63"}})
	docker_update.extract_container_config(container)
	running = {**stopped, "NetworkSettings": {"Networks": {"lan": {"NetworkID": _LAN_ID, "MacAddress": "8a:7b:b3:fa:b4:0c"}}}}
	other, asked_running = _inspected_by("1.54", running, {"Config": {}})
	docker_update.extract_container_config(other)
	assert asked == [] and asked_running == [], (asked, asked_running)


def test_the_default_bridge_reported_as_bridge_still_gives_its_settings():
	"""
	Docker 25 and older keep `default` as the network mode and report the
	endpoint as `bridge`: its link-local addresses were read from nowhere,
	and lost. The create still takes `default`.
	"""
	container, _ = _inspected_by("1.44", {
		"Config": {"Image": "nginx"}, "HostConfig": {"NetworkMode": "default"},
		"NetworkSettings": {"Networks": {"bridge": {"NetworkID": "c" * 64, "IPAMConfig": {"LinkLocalIPs": ["169.254.20.7"]}}}}})
	config = docker_update.extract_container_config(container)
	assert config["network_mode"] == "default" and config["network_mode_by_id"] is None
	assert config["link_local_ips"] == ["169.254.20.7"]


def test_whether_config_mac_address_can_say_a_mac_was_set_by_hand():
	"""
	Docker 25 to 28 copy the main MAC into Config.MacAddress by looking the
	network mode up among the endpoints. Under an id they are not listed by,
	it says nothing, and the endpoint's MAC is kept as it came. Under a
	short id they are listed by, it does say: an assigned one is left to
	Docker, which Docker 28 would otherwise copy there and pin.
	"""
	short = _LAN_ID[:12]

	def config_for(mode, keys, cfg_mac=None, mac="02:6f:61:2c:fe:94", version="1.48"):
		endpoint = {"NetworkID": _LAN_ID, "IPAddress": "172.31.1.75", "MacAddress": mac}
		container, _ = _inspected_by(version, {
			"Config": {"Image": "alpine", **({"MacAddress": cfg_mac} if cfg_mac else {})},
			"HostConfig": {"NetworkMode": mode},
			"NetworkSettings": {"Networks": {key: dict(endpoint) for key in keys}}})
		return docker_update.extract_container_config(container)

	single_short = config_for(short, (short, "lan"))
	assert not single_short["mac_address_unsaid"]
	assert docker_update._primary_mac_to_keep(single_short, "1.48") is None
	by_hand = config_for(short, (short, "lan"), cfg_mac="02:bb:00:00:01:76", mac="02:bb:00:00:01:76")
	assert docker_update._primary_mac_to_keep(by_hand, "1.48") == "02:bb:00:00:01:76"
	full_id = config_for(_LAN_ID, ("lan",))
	assert full_id["mac_address_unsaid"]
	assert docker_update._primary_mac_to_keep(full_id, "1.48") == "02:6f:61:2c:fe:94"


def test_compose_gets_the_mac_set_by_hand_where_only_the_endpoint_says_it():
	"""
	A container Compose 2.29 created by the network's id has nothing in
	Config.MacAddress, and the second /compose lost its MAC. On Docker 25
	to 27 the endpoint's MAC still tells, since theirs come from the address.
	"""
	def config_for(version, mac, mode=_LAN_ID):
		container, _ = _inspected_by(version, {
			"Config": {"Image": "pihole/pihole"}, "HostConfig": {"NetworkMode": mode},
			"NetworkSettings": {"Networks": {"lan": {"NetworkID": _LAN_ID, "IPAddress": "172.30.10.2", "MacAddress": mac}}}})
		return docker_update.extract_container_config(container)

	assert config_for("1.44", "02:42:ac:aa:00:06")["mac_address_by_hand"] == "02:42:ac:aa:00:06"
	assert config_for("1.47", "02:42:ac:aa:00:06")["mac_address_by_hand"] == "02:42:ac:aa:00:06"
	# Docker's own, made from the address.
	assert config_for("1.47", "02:42:ac:1e:0a:02")["mac_address_by_hand"] is None
	# From 28 they are random, and by name Config.MacAddress says it.
	assert config_for("1.48", "02:42:ac:aa:00:06")["mac_address_by_hand"] is None
	assert config_for("1.47", "02:42:ac:aa:00:06", mode="lan")["mac_address_by_hand"] is None
