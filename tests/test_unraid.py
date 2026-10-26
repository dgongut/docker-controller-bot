"""
Telling Unraid that a container the bot updated is up to date.

The files and what goes in them are Unraid 7.3.4's, read off a real server:
the status of `dgongut/dropbot:latest` and the page's cache, as they were
while its Docker page marked DropBot "update ready" after the bot updated it.
The helper and the files it reaches are doubles here; test_real_docker.py
writes through a real one.
"""

import io
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

if harness.SRC not in sys.path:
	sys.path.insert(0, harness.SRC)

import docker.errors

import unraid

OLD = "sha256:1becb9d2c0cfc940b2e8aa5aecf416b1f16800afe901a35280cbe92e852a808e"
NEW = "sha256:1f3318f853feafaf36a384a90825cc01029645ee5b620bd8e686c04b848daaa3"
OTHER = "sha256:8f655bad7dcbc5398a78f70a5af12600b4ed3424f901566ed1eaa6710e9e2334"

STATUS = {
	"lscr.io/linuxserver/plex:latest": {"local": OTHER, "remote": OTHER, "status": "true"},
	"dgongut/dropbot:latest": {"local": OLD, "remote": NEW, "status": "false"},
}
INFO = {
	"plex": {"running": True, "updated": "true", "icon": "/state/plugins/dynamix.docker.manager/images/plex-icon.png"},
	"DropBot": {"running": True, "updated": "false", "url": "", "cpuset": ""},
}


def test_the_key_is_the_one_unraid_files_an_image_under():
	assert unraid.status_key("dgongut/dropbot") == "dgongut/dropbot:latest"
	assert unraid.status_key("lscr.io/linuxserver/plex:latest") == "lscr.io/linuxserver/plex:latest"
	assert unraid.status_key("nginx") == "library/nginx:latest"
	assert unraid.status_key("nginx:1.27") == "library/nginx:1.27"
	# The first colon is the registry's port, not a tag.
	assert unraid.status_key("nas:5000/app") == "nas:5000/app:latest"
	assert unraid.status_key("nas:5000/team/app:v2") == "nas:5000/team/app:v2"
	# Unraid does not drop it, so neither does the key.
	assert unraid.status_key("docker.io/library/nginx") == "docker.io/library/nginx:latest"
	for pinned in ("nginx@" + NEW, "dgongut/dropbot:latest@" + NEW, NEW, "", None):
		assert unraid.status_key(pinned) is None, pinned


def test_the_digest_is_the_one_of_the_containers_repository():
	assert unraid.pulled_digest([f"dgongut/dropbot@{NEW}"], "dgongut/dropbot:latest") == NEW
	# Docker Hub's official images are recorded without `library/`.
	assert unraid.pulled_digest([f"nginx@{NEW}"], "library/nginx:latest") == NEW
	assert unraid.pulled_digest([f"nginx@{NEW}"], "docker.io/library/nginx:latest") == NEW
	# An image pulled from two places has a digest for each.
	digests = [f"linuxserver/plex@{OTHER}", f"lscr.io/linuxserver/plex@{NEW}"]
	assert unraid.pulled_digest(digests, "lscr.io/linuxserver/plex:latest") == NEW
	assert unraid.pulled_digest(digests, "linuxserver/plex:latest") == OTHER
	assert unraid.pulled_digest([f"someone/else@{NEW}"], "dgongut/dropbot:latest") is None
	assert unraid.pulled_digest(["dgongut/dropbot@sha256:short"], "dgongut/dropbot:latest") is None
	assert unraid.pulled_digest(None, "dgongut/dropbot:latest") is None


def test_the_status_is_patched_as_unraid_does_after_its_own_pull():
	patched = unraid.patch_status(STATUS, "dgongut/dropbot:latest", NEW)
	assert patched["dgongut/dropbot:latest"] == {"local": NEW, "remote": NEW, "status": "true"}
	assert patched["lscr.io/linuxserver/plex:latest"] == STATUS["lscr.io/linuxserver/plex:latest"]
	assert STATUS["dgongut/dropbot:latest"]["local"] == OLD, "ha cambiado el original"
	# Already right: nothing to write.
	assert unraid.patch_status(patched, "dgongut/dropbot:latest", NEW) is None


def test_only_an_entry_unraid_already_has_is_patched():
	assert unraid.patch_status(STATUS, "dgongut/other:latest", NEW) is None
	assert unraid.patch_status({"dgongut/dropbot:latest": "?"}, "dgongut/dropbot:latest", NEW) is None
	# PHP writes an empty array as [].
	assert unraid.patch_status([], "dgongut/dropbot:latest", NEW) is None
	assert unraid.patch_status(None, "dgongut/dropbot:latest", NEW) is None


def test_the_page_forgets_the_container_and_any_helper_it_saw():
	cleaned = unraid.patch_info(dict(INFO, **{"dcb-unraid-0a1b2c3d": {"updated": "null"}}), "DropBot")
	assert set(cleaned) == {"plex"}, cleaned
	assert unraid.patch_info(INFO, "sonarr") is None
	assert unraid.patch_info([], "DropBot") is None


# --- The helper ---------------------------------------------------------------


def _tar(name, content):
	buffer = io.BytesIO()
	with tarfile.open(fileobj=buffer, mode="w") as tar:
		member = tarfile.TarInfo(name)
		member.size = len(content)
		tar.addfile(member, io.BytesIO(content))
	return buffer.getvalue()


class Helper:
	"""A created container: files under its mounts, and no start()."""

	def __init__(self, disk, kwargs):
		self.disk = disk
		self.kwargs = kwargs
		self.name = kwargs["name"]
		self.mounts = {m["Target"]: m["Source"] for m in kwargs["mounts"] if m["Type"] == "bind"}
		self.removed = None
		self.fail_on_read = False

	def _host_path(self, path):
		directory, _, name = path.rpartition("/")
		return f"{self.mounts[directory]}/{name}"

	def get_archive(self, path):
		if self.fail_on_read:
			raise docker.errors.APIError("connection reset")
		host_path = self._host_path(path)
		if host_path not in self.disk:
			raise docker.errors.NotFound("no such file")
		content = self.disk[host_path]
		return iter([_tar(path.rpartition("/")[2], content)]), {"size": len(content)}

	def put_archive(self, directory, data):
		with tarfile.open(fileobj=io.BytesIO(data)) as tar:
			for member in tar.getmembers():
				self.disk[f"{self.mounts[directory]}/{member.name}"] = tar.extractfile(member).read()
				assert member.mode == 0o644, oct(member.mode)
		return True

	def remove(self, v=False, force=False):
		self.removed = (v, force)


class Leftover:
	def __init__(self, name, labels, created):
		self.name = name
		self.labels = labels
		self.attrs = {"Created": created.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")}
		self.removed = False

	def remove(self, v=False, force=False):
		assert v and force
		self.removed = True


class Client:
	def __init__(self, os_name="Unraid OS 7.3 x86_64", disk=None, volumes=None, missing=(), leftovers=()):
		self.disk = dict(disk if disk is not None else {
			"/var/lib/docker/unraid-update-status.json": json.dumps(STATUS, indent=4).encode(),
			f"{unraid.INFO_DIR}/docker.json": json.dumps(INFO, indent=4).encode(),
		})
		self.os_name = os_name
		self.missing = set(missing)
		self.helpers = []
		self.info_calls = 0
		self.leftovers = list(leftovers)
		image = SimpleNamespace(id="sha256:" + "6" * 64,
								attrs={"RepoDigests": [f"dgongut/dropbot@{NEW}"],
										"Config": {"Volumes": volumes}})
		self.images = SimpleNamespace(get=lambda ref: image)
		self.containers = SimpleNamespace(create=self._create, list=self._list)

	def info(self):
		self.info_calls += 1
		return {"OperatingSystem": self.os_name}

	def _create(self, image, **kwargs):
		for mount in kwargs["mounts"]:
			if mount["Type"] == "bind" and mount["Source"] in self.missing:
				raise docker.errors.APIError(f"bind source path does not exist: {mount['Source']}")
		helper = Helper(self.disk, kwargs)
		helper.image = image
		self.helpers.append(helper)
		return helper

	def _list(self, all=False, filters=None):
		assert all and filters == {"label": f"{unraid.HELPER_LABEL}={unraid.HELPER_KIND}"}, filters
		return self.leftovers

	def status(self):
		return json.loads(self.disk["/var/lib/docker/unraid-update-status.json"])

	def page(self):
		return json.loads(self.disk[f"{unraid.INFO_DIR}/docker.json"])


def _dropbot(manager="dockerman", image="dgongut/dropbot"):
	labels = {unraid.MANAGED_LABEL: manager} if manager else {}
	return SimpleNamespace(name="DropBot", attrs={"Image": "sha256:" + "6" * 64,
												  "Config": {"Image": image, "Labels": labels}})


def _sync(client, container=None):
	logged = []
	unraid.sync_after_update(client, "h_local", container or _dropbot(), logged.append)
	return logged


def test_after_an_update_unraid_sees_the_container_up_to_date():
	client = Client()
	_sync(client)
	assert client.status()["dgongut/dropbot:latest"] == {"local": NEW, "remote": NEW, "status": "true"}
	assert client.status()["lscr.io/linuxserver/plex:latest"] == STATUS["lscr.io/linuxserver/plex:latest"]
	assert set(client.page()) == {"plex"}, client.page()
	assert client.page()["plex"] == INFO["plex"]


def test_the_files_are_written_the_way_unraid_writes_them():
	"""JSON_PRETTY_PRINT|JSON_UNESCAPED_SLASHES: four spaces, slashes as they are."""
	client = Client()
	_sync(client)
	text = client.disk["/var/lib/docker/unraid-update-status.json"].decode()
	assert '\n    "lscr.io/linuxserver/plex:latest": {\n        "local": ' in text, text
	assert "\\/" not in text
	assert "\\/" not in client.disk[f"{unraid.INFO_DIR}/docker.json"].decode()


def test_the_helper_never_runs_anything_and_is_removed():
	client = Client(volumes={"/config": {}, "/data": {}})
	_sync(client)
	assert len(client.helpers) == 1
	helper = client.helpers[0]
	kwargs = helper.kwargs
	# Made from the image just pulled, which is certain to be on that host.
	assert helper.image == "sha256:" + "6" * 64
	assert not hasattr(helper, "start")
	assert kwargs["entrypoint"] == ["true"] and kwargs["network_disabled"] is True
	assert kwargs["labels"] == {unraid.HELPER_LABEL: unraid.HELPER_KIND}
	assert kwargs["name"].startswith(unraid.HELPER_PREFIX)
	assert unraid.is_helper(kwargs["name"], kwargs["labels"])
	binds = {m["Source"]: m["Target"] for m in kwargs["mounts"] if m["Type"] == "bind"}
	assert set(binds) == {"/var/lib/docker", unraid.INFO_DIR}, binds
	assert not any(m.get("ReadOnly") for m in kwargs["mounts"])
	# No anonymous volume for what the image declares: a tmpfs over each.
	tmpfs = {m["Target"] for m in kwargs["mounts"] if m["Type"] == "tmpfs"}
	assert tmpfs == {"/config", "/data"}, tmpfs
	assert helper.removed == (True, True)


def test_a_host_that_is_not_unraid_is_left_alone():
	client = Client(os_name="Ubuntu 24.04.3 LTS")
	_sync(client)
	assert client.helpers == []
	assert client.status() == STATUS


def test_a_container_unraid_does_not_track_is_left_alone_without_asking_the_host():
	for manager in ("composeman", None):
		client = Client()
		_sync(client, _dropbot(manager=manager))
		assert client.helpers == [] and client.info_calls == 0, manager


def test_an_image_pinned_by_digest_is_left_alone():
	client = Client()
	_sync(client, _dropbot(image=f"dgongut/dropbot@{NEW}"))
	assert client.helpers == []


def test_an_entry_unraid_does_not_have_is_not_made_up():
	"""Unraid reads the image itself when there is none, and gets it right."""
	status = {"lscr.io/linuxserver/plex:latest": STATUS["lscr.io/linuxserver/plex:latest"]}
	client = Client(disk={
		"/var/lib/docker/unraid-update-status.json": json.dumps(status, indent=4).encode(),
		f"{unraid.INFO_DIR}/docker.json": json.dumps(INFO, indent=4).encode(),
	})
	before = client.disk["/var/lib/docker/unraid-update-status.json"]
	_sync(client)
	assert client.disk["/var/lib/docker/unraid-update-status.json"] == before
	# The page's "update ready" goes all the same.
	assert "DropBot" not in client.page()
	assert client.helpers[0].removed == (True, True)


def test_a_file_that_is_not_unraids_is_not_written():
	for content in (b"{roto", b"[]", b'"texto"', b""):
		client = Client(disk={"/var/lib/docker/unraid-update-status.json": content})
		_sync(client)
		assert client.disk["/var/lib/docker/unraid-update-status.json"] == content, content
		assert client.helpers[0].removed == (True, True)


def test_no_status_file_at_all_writes_nothing():
	client = Client(disk={})
	logged = _sync(client)
	assert client.disk == {}, client.disk
	assert client.helpers[0].removed == (True, True), logged


def test_without_the_pages_cache_the_status_is_still_written():
	"""The cache is in RAM, under the web interface: it is the one that may be missing."""
	client = Client(missing={unraid.INFO_DIR})
	client.disk.pop(f"{unraid.INFO_DIR}/docker.json")
	logged = _sync(client)
	assert client.status()["dgongut/dropbot:latest"]["status"] == "true", logged
	binds = [m["Source"] for m in client.helpers[0].kwargs["mounts"] if m["Type"] == "bind"]
	assert binds == ["/var/lib/docker"], binds


def test_without_the_status_directory_nothing_is_created_and_nothing_raised():
	client = Client(missing={"/var/lib/docker", unraid.INFO_DIR})
	logged = _sync(client)
	assert client.helpers == []
	assert any("Could not tell Unraid" in line for line in logged), logged


def test_a_failure_halfway_still_removes_the_helper_and_raises_nothing():
	client = Client()
	original = client._create

	def create(image, **kwargs):
		helper = original(image, **kwargs)
		helper.fail_on_read = True
		return helper

	client.containers.create = create
	logged = _sync(client)
	assert client.helpers[0].removed == (True, True)
	assert client.status() == STATUS
	assert any("connection reset" in line for line in logged), logged


def test_a_helper_left_behind_is_swept_and_nothing_else_is():
	now = datetime.now(timezone.utc)
	label = {unraid.HELPER_LABEL: unraid.HELPER_KIND}
	abandoned = Leftover("dcb-unraid-00000001", label, now - timedelta(hours=2))
	# Another bot's, at work right now on the same host.
	working = Leftover("dcb-unraid-00000002", label, now - timedelta(seconds=5))
	# The label alone, on something of the user's, is not enough.
	theirs = Leftover("postgres", label, now - timedelta(days=30))
	client = Client(leftovers=[abandoned, working, theirs])
	_sync(client)
	assert abandoned.removed
	assert not working.removed and not theirs.removed
