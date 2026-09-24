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
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

if harness.REPO not in sys.path:
	sys.path.insert(0, harness.REPO)

import docker.errors
import docker_update


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
		self.status = "running"

	def rename(self, name):
		if any(c.name == name and c is not self for c in self.engine.containers.values()):
			raise docker.errors.APIError(f'Conflict. The container name "/{name}" is already in use')
		self.name = name

	def remove(self, force=False):
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
		return self.engine.add(name, status="created", role="new")


class FakeImages:
	def __init__(self, engine):
		self.engine = engine

	def pull(self, image):
		self.engine.pulled = True
		return type("Image", (), {"id": "sha256:new"})()

	def remove(self, image_id):
		pass


class FakeDocker:
	def __init__(self):
		self.containers = {}
		self.pulled = False
		self.fail_create = False
		self.fail_start = False
		self.on_create = None
		self.api = self  # unused, but the SDK has one

	def add(self, name, status="running", role=None):
		container = FakeContainer(self, name, status, role)
		self.containers[container.id] = container
		return container

	def named(self, name):
		return [c for c in self.containers.values() if c.name == name]

	@property
	def client(self):
		client = type("Client", (), {})()
		client.containers = FakeContainers(self)
		client.images = FakeImages(self)
		client.networks = None
		return client


def _config():
	config = defaultdict(lambda: None)
	config.update(image="repo/app:latest", is_running=True, networks={})
	return config


def _update(engine, container):
	return docker_update.perform_update(
		client=engine.client, container=container, config=_config(),
		container_name=container.name, message=None,
		edit_message_func=lambda *a, **k: None, debug_func=lambda m: None,
		error_func=lambda m: None, get_text_func=lambda key, *a: key,
		save_status_func=lambda *a: None, container_id_length=12, telegram_group=0)


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
