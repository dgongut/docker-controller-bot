"""
The host registry.

Runs without the bot: the registry only depends on the settings and on the
Docker SDK, which is stubbed. What matters here is that one host being down
degrades what it can and nothing else, and that nothing durable is ever keyed
on something the user can rename.
"""

import os
import shutil
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

import host_registry
import store


def setup(hosts=None):
	"""A fresh settings file with the given hosts, and a stubbed Docker SDK."""
	store_, root = harness.temp_storage()
	import docker
	docker.DockerClient = lambda *args, **kwargs: MagicMock()
	host_registry.reset()
	store_.set("hosts", hosts if hosts is not None else [
		{"id": "h_local", "alias": "local", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
	])
	return store_, root


def test_a_single_host_hides_the_host_level():
	"""
	The whole interface hangs off this: with one host the bot has to look
	exactly as it did before hosts existed.
	"""
	_, root = setup()
	assert host_registry.is_single_host() is True
	assert host_registry.local_host_id() == "h_local"
	assert host_registry.alias("h_local") == "local"
	shutil.rmtree(root, ignore_errors=True)


def test_several_hosts():
	_, root = setup([
		{"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
		{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
	])
	assert host_registry.is_single_host() is False
	assert host_registry.local_host_id() == "h_local"
	assert host_registry.alias("h_nas") == "nas"
	assert host_registry.find_by_alias("NAS")["id"] == "h_nas"
	assert host_registry.find_by_alias("no-existe") is None
	shutil.rmtree(root, ignore_errors=True)


def test_the_local_host_is_found_by_its_flag_not_its_position():
	_, root = setup([
		{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
		{"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
	])
	assert host_registry.local_host_id() == "h_local"
	shutil.rmtree(root, ignore_errors=True)


def test_renaming_a_host_changes_nothing_durable():
	"""
	Cache entries and schedules are keyed on the id precisely so that a rename,
	which is two taps from Telegram, cannot orphan them.
	"""
	store_, root = setup()
	store_.set_update_status("h_local", "nginx", "nginx:1.27", True)
	assert host_registry.rename_host("h_local", "casa") is True
	assert host_registry.alias("h_local") == "casa"
	assert store_.update_status("h_local", "nginx", "nginx:1.27") is True
	shutil.rmtree(root, ignore_errors=True)


def test_changing_the_url_rebuilds_the_client():
	"""Otherwise a host moved to a new address keeps using the dead socket."""
	store_, root = setup()
	first = host_registry.client("h_local")
	assert host_registry.client("h_local") is first, "debería estar cacheado"

	hosts = store_.get("hosts")
	hosts[0]["url"] = "tcp://elsewhere:2375"
	store_.set("hosts", hosts)
	assert host_registry.client("h_local") is not first
	shutil.rmtree(root, ignore_errors=True)


def test_an_unconfigured_host_is_unavailable_not_a_crash():
	_, root = setup()
	try:
		host_registry.client("h_nope")
		raise AssertionError("debería lanzar")
	except host_registry.HostUnavailable as e:
		assert e.host_id == "h_nope"
	assert host_registry.try_client("h_nope") is None
	shutil.rmtree(root, ignore_errors=True)


def test_building_a_client_does_not_ping():
	"""
	The bot has always started even when Docker was not answering yet. Pinging
	at construction would turn a slow daemon at boot into a container that
	refuses to start.
	"""
	_, root = setup()
	import docker
	pinged = []

	def sdk(*args, **kwargs):
		fake = MagicMock()
		fake.ping.side_effect = lambda: pinged.append(True)
		return fake

	docker.DockerClient = sdk
	host_registry.reset()
	host_registry.client("h_local")
	assert pinged == [], "construir un cliente no debe hacer ping"
	assert host_registry.ping("h_local") is True
	assert pinged == [True]
	shutil.rmtree(root, ignore_errors=True)


def test_a_host_that_stops_answering_is_dropped():
	"""So the next use reconnects instead of reusing a dead socket."""
	_, root = setup()
	import docker
	docker.DockerClient = lambda *a, **kw: MagicMock(
		**{"ping.side_effect": Exception("connection refused")})
	host_registry.reset()
	assert host_registry.ping("h_local") is False
	assert "h_local" not in host_registry._clients
	shutil.rmtree(root, ignore_errors=True)


def test_one_host_being_down_does_not_hide_the_others():
	_, root = setup([
		{"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
		{"id": "h_dead", "alias": "nas", "url": "tcp://nas:2375"},
	])
	import docker

	def sdk(base_url=None, **kwargs):
		fake = MagicMock()
		if "nas" in (base_url or ""):
			fake.ping.side_effect = Exception("no route to host")
		return fake

	docker.DockerClient = sdk
	host_registry.reset()
	reachable = host_registry.reachable_hosts()
	assert [entry["id"] for entry, _ in reachable] == ["h_local"]
	shutil.rmtree(root, ignore_errors=True)


def test_ssh_without_paramiko_says_so():
	"""
	A missing dependency and an unreachable machine have completely different
	fixes, so they must not look the same in the log.
	"""
	_, root = setup([{"id": "h_ssh", "alias": "nas", "url": "ssh://user@nas"}])
	blocked = {"paramiko"}
	real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

	def guard(name, *args, **kwargs):
		if name in blocked:
			raise ImportError(name)
		return real_import(name, *args, **kwargs)

	if isinstance(__builtins__, dict):
		__builtins__["__import__"] = guard
	else:
		__builtins__.__import__ = guard
	try:
		host_registry.client("h_ssh")
		raise AssertionError("debería lanzar")
	except host_registry.HostUnavailable as e:
		assert "paramiko" in e.reason, e.reason
	finally:
		if isinstance(__builtins__, dict):
			__builtins__["__import__"] = real_import
		else:
			__builtins__.__import__ = real_import
	shutil.rmtree(root, ignore_errors=True)


def test_adding_a_host_verifies_it_first():
	"""
	Worth rejecting at the point someone adds it, while they still have the
	connection details in front of them.
	"""
	store_, root = setup()
	import docker
	docker.DockerClient = lambda *a, **kw: MagicMock(
		**{"ping.side_effect": Exception("connection refused")})
	host_registry.reset()
	try:
		host_registry.add_host("nas", "tcp://nas:2375")
		raise AssertionError("debería lanzar")
	except host_registry.HostUnavailable:
		pass
	assert len(store_.get("hosts")) == 1, "un host inalcanzable no debe guardarse"

	docker.DockerClient = lambda *a, **kw: MagicMock()
	entry = host_registry.add_host("nas", "tcp://nas:2375")
	assert entry["id"].startswith("h_")
	assert entry["local"] is False
	assert len(store_.get("hosts")) == 2
	shutil.rmtree(root, ignore_errors=True)


def test_generated_ids_do_not_collide():
	_, root = setup()
	seen = {"h_local"}
	for _ in range(50):
		new = host_registry.generate_host_id()
		assert new not in seen
	shutil.rmtree(root, ignore_errors=True)


def test_the_local_host_cannot_be_removed():
	"""The bot runs on it: removing it would leave nothing to manage."""
	store_, root = setup([
		{"id": "h_local", "alias": "casa", "url": host_registry.LOCAL_SOCKET_URL, "local": True},
		{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
	])
	assert host_registry.remove_host("h_local") is False
	assert len(store_.get("hosts")) == 2

	assert host_registry.remove_host("h_nas") is True
	assert [h["id"] for h in store_.get("hosts")] == ["h_local"]
	assert host_registry.remove_host("h_nas") is False, "quitar dos veces no debe fallar"
	shutil.rmtree(root, ignore_errors=True)
