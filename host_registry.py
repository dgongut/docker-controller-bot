"""
The Docker hosts the bot manages.

Hosts are defined in the settings file, not in the environment, so one can be
added from Telegram without touching the compose. Each entry looks like:

	{"id": "h_a91c", "alias": "nas", "url": "ssh://dgongut@nas", "local": false}

The id is generated once and never changes. Everything host-specific is keyed
on it rather than on the alias, so renaming a host — or moving it from tcp:// to
ssh:// once it has TLS — does not orphan its cached update state or its
schedules.

Credentials never live in the settings file. An ssh key or a TLS certificate is
mapped into the container as a file and the settings only name the path, which
keeps a chat message from ever carrying key material.
"""

import threading

import store
from logger import debug, warning

LOCAL_SOCKET_URL = "unix:///var/run/docker.sock"


class HostUnavailable(Exception):
	"""
	Raised when a host cannot be reached.

	Its own exception type because one host being down must never take the bot
	with it: callers catch this and carry on with the hosts that answer.
	"""

	def __init__(self, host_id, reason):
		super().__init__(f"Host {host_id} is unavailable: {reason}")
		self.host_id = host_id
		self.reason = reason


_lock = threading.RLock()
# Clients are cached per host, together with the URL they were built from, so
# that changing a host's URL rebuilds it instead of reusing a stale connection.
_clients = {}


def hosts():
	"""
	Every configured host, in the order they were added.

	Read from the settings on each call: a host added from Telegram has to show
	up without restarting.
	"""
	configured = store.get("hosts") or []
	return [host for host in configured if isinstance(host, dict) and host.get("id")]


def host(host_id):
	"""One host by id, or None."""
	for candidate in hosts():
		if candidate["id"] == host_id:
			return candidate
	return None


def local_host_id():
	"""
	The id of the host the bot itself runs on.

	The self-update path has to be pinned to it: the bot's own container only
	exists on one host, and updating it anywhere else would look for a
	container that is not there.
	"""
	for candidate in hosts():
		if candidate.get("local"):
			return candidate["id"]
	# No local host registered yet (migration has not run, or someone edited it
	# out); the first host is a better guess than nothing.
	return hosts()[0]["id"] if hosts() else None


def is_single_host():
	"""
	True while only one host is configured.

	The whole interface hangs off this: with a single host the host level is
	never shown anywhere, so the bot looks exactly as it did before hosts
	existed.
	"""
	return len(hosts()) <= 1


def alias(host_id):
	"""A host's display name, falling back to its id."""
	entry = host(host_id)
	return (entry.get("alias") or host_id) if entry else host_id


def find_by_alias(name):
	"""
	The host whose alias matches `name`, case-insensitively, or None.

	Used by the `nas:firefox` shorthand.
	"""
	wanted = (name or "").strip().lower()
	if not wanted:
		return None
	for candidate in hosts():
		if str(candidate.get("alias", "")).lower() == wanted:
			return candidate
	return None


def generate_host_id():
	"""A short id no configured host is using."""
	import uuid

	taken = {candidate["id"] for candidate in hosts()}
	while True:
		candidate = f"h_{uuid.uuid4().hex[:4]}"
		if candidate not in taken:
			return candidate


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _build_client(entry, verify=False):
	"""
	Opens a Docker client for one host.

	`verify` pings the daemon before handing the client back. It is off by
	default on purpose: the bot has always started even when Docker was not
	answering yet, and pinging here would turn a slow daemon at boot into a
	container that refuses to start. Callers that need to know now — adding a
	host, sweeping every host — ask for it explicitly.

	The SDK handles unix://, tcp:// and ssh:// itself. ssh:// additionally needs
	paramiko, which is reported as a missing dependency rather than as a
	connection failure, because the two have completely different fixes.
	"""
	import docker

	url = entry.get("url") or LOCAL_SOCKET_URL
	host_id = entry["id"]

	if url.startswith("ssh://"):
		try:
			import paramiko  # noqa: F401
		except ImportError:
			raise HostUnavailable(host_id, "ssh:// needs paramiko, which this image does not ship")

	kwargs = {"base_url": url, "timeout": int(entry.get("timeout") or 30)}

	# TLS for a tcp:// host. The certificates are files mapped into the
	# container; only their paths are ever stored.
	certs = entry.get("tls") or {}
	if certs:
		try:
			from docker.tls import TLSConfig

			kwargs["tls"] = TLSConfig(
				client_cert=(certs["cert"], certs["key"]) if certs.get("cert") else None,
				ca_cert=certs.get("ca"),
				verify=bool(certs.get("verify", True)),
			)
		except Exception as e:
			raise HostUnavailable(host_id, f"invalid TLS configuration: {e}")

	try:
		built = docker.DockerClient(**kwargs)
		if verify:
			built.ping()
	except Exception as e:
		raise HostUnavailable(host_id, str(e))
	return built


def client(host_id):
	"""
	A live client for `host_id`, building and caching it on first use.

	Raises HostUnavailable when the host cannot be reached, so a caller that
	iterates over hosts can skip the ones that are down.
	"""
	entry = host(host_id)
	if entry is None:
		raise HostUnavailable(host_id, "not configured")

	url = entry.get("url") or LOCAL_SOCKET_URL
	with _lock:
		cached = _clients.get(host_id)
		if cached and cached[0] == url:
			return cached[1]

		built = _build_client(entry)
		_clients[host_id] = (url, built)
		debug(f"Connected to host {entry.get('alias', host_id)} ({url})")
		return built


def try_client(host_id):
	"""The client for `host_id`, or None when it cannot be built."""
	try:
		return client(host_id)
	except HostUnavailable as e:
		warning(str(e))
		return None


def ping(host_id):
	"""
	Whether `host_id` answers right now.

	A cached client whose connection has gone bad is dropped, so the next use
	reconnects instead of reusing a dead socket.
	"""
	connection = try_client(host_id)
	if connection is None:
		return False
	try:
		connection.ping()
		return True
	except Exception as e:
		warning(f"Host {alias(host_id)} stopped answering: {e}")
		drop(host_id)
		return False


def drop(host_id):
	"""
	Forgets the cached client for a host.

	Called when a host's connection details change, and when a connection goes
	bad so that the next use reconnects instead of reusing a dead socket.
	"""
	with _lock:
		removed = _clients.pop(host_id, None)
	if removed:
		debug(f"Dropped the cached client for host {host_id}")


def reachable_hosts():
	"""
	Every host that answers right now, as (host, client) pairs.

	Anything that has to sweep every host goes through here, so one machine
	being down degrades that sweep instead of breaking it.
	"""
	pairs = []
	for entry in hosts():
		if ping(entry["id"]):
			pairs.append((entry, _clients[entry["id"]][1]))
	return pairs


def add_host(alias_name, url, tls=None, timeout=None):
	"""
	Registers a new host and returns its entry.

	The client is opened straight away: a host that cannot be reached is worth
	rejecting at the point someone adds it, while they still have the details
	in front of them.
	"""
	entry = {"id": generate_host_id(), "alias": alias_name, "url": url, "local": False}
	if tls:
		entry["tls"] = tls
	if timeout:
		entry["timeout"] = int(timeout)

	_build_client(entry, verify=True)  # raises HostUnavailable

	with _lock:
		configured = hosts()
		configured.append(entry)
		store.set("hosts", configured)
	debug(f"Registered host {alias_name} ({url}) as {entry['id']}")
	return entry


def remove_host(host_id):
	"""
	Removes a host. The local one cannot be removed: the bot runs on it.

	Returns True when something was removed.
	"""
	entry = host(host_id)
	if entry is None:
		return False
	if entry.get("local"):
		warning("Refusing to remove the local host: the bot itself runs on it")
		return False

	with _lock:
		store.set("hosts", [h for h in hosts() if h["id"] != host_id])
	drop(host_id)
	debug(f"Removed host {host_id}")
	return True


def rename_host(host_id, new_alias):
	"""
	Changes a host's display name.

	Nothing is keyed on the alias, so this touches no cached state and no
	schedule.
	"""
	configured = hosts()
	for entry in configured:
		if entry["id"] == host_id:
			entry["alias"] = new_alias
			store.set("hosts", configured)
			return True
	return False


def reset():
	"""Drops every cached client. For tests, and after a settings reload."""
	with _lock:
		_clients.clear()
