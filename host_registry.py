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

import shutil
import threading
import time

import store
from logger import debug, warning

LOCAL_SOCKET_URL = "unix:///var/run/docker.sock"

# The three transports the Docker SDK speaks. Anything else parses fine as a
# URL and then fails much later, with an error that says nothing about what was
# actually wrong.
SUPPORTED_SCHEMES = ("unix://", "tcp://", "ssh://")

# What a host's operations wait for when it does not say otherwise. The SDK
# applies this to every request, not just to opening the connection, so it has
# to be long enough for the slowest legitimate call — an /exec running someone's
# command — rather than for the quickest.
DEFAULT_TIMEOUT_SECONDS = 30

# What a reachability check waits for. Far shorter, because the only question it
# asks is whether the daemon is there: nothing it does can legitimately take
# thirty seconds, so waiting that long only delays finding out the host is down.
PROBE_TIMEOUT_SECONDS = 5


class HostRejected(Exception):
	"""
	Raised when a host is not worth even trying to reach.

	Separate from HostUnavailable because the fix is different: the machine may
	well be up, it is what was typed that cannot be registered.

	`reason` is one of "scheme" or "duplicate", so the caller picks the message
	instead of parsing text.
	"""

	def __init__(self, reason):
		super().__init__(reason)
		self.reason = reason


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

# Reachability checks in flight, and the outcomes they left behind. A ping that
# blocks past the deadline cannot be cancelled, so it is remembered instead: the
# next sweep reuses it rather than piling a second thread on the same host.
_probe_lock = threading.Lock()
_probes = {}
_probe_results = {}
# Probe clients are cached separately from the working ones. They differ only in
# their timeout, and sharing a cache would mean whichever ran first decided how
# long every later operation waits.
_probe_clients = {}


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

def _build_client(entry, verify=False, timeout=None):
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

	`timeout` overrides what the host is configured with, and exists for the
	reachability checks: the SDK applies the timeout to every request, so the
	value that lets an /exec finish is far too long to spend finding out that a
	machine is unplugged.
	"""
	import docker

	url = entry.get("url") or LOCAL_SOCKET_URL
	host_id = entry["id"]

	if timeout is None:
		timeout = int(entry.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
	kwargs = {"base_url": url, "timeout": timeout}

	if url.startswith("ssh://"):
		# The SDK imports paramiko to load its ssh transport at all, even when
		# the connection itself is handed to the ssh binary. The image ships
		# both; a custom build without them gets told which is missing, since a
		# missing dependency and an unreachable machine have different fixes.
		try:
			import paramiko  # noqa: F401
		except ImportError:
			raise HostUnavailable(host_id, "ssh:// needs the py3-paramiko package, which this image does not have")
		if not shutil.which("ssh"):
			raise HostUnavailable(host_id, "ssh:// needs the ssh client, which this image does not have")

		# Hand the connection to the system ssh client rather than letting
		# paramiko dial it. That is what makes ~/.ssh/config, known_hosts and
		# the agent work, so a host that answers `ssh nas` answers here too and
		# is debugged the same way.
		kwargs["use_ssh_client"] = True

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
		displaced = _clients.get(host_id)
		_clients[host_id] = (url, built)
		debug(f"Connected to host {entry.get('alias', host_id)} ({url})")
	# The URL changed under us, so what was cached is not this host any more.
	# Same leak as drop(): nothing will ask for that client again, and its ssh
	# process outlives it until somebody closes it.
	_close((displaced,), host_id)
	return built


def try_client(host_id):
	"""The client for `host_id`, or None when it cannot be built."""
	try:
		return client(host_id)
	except HostUnavailable as e:
		warning(str(e))
		return None


def probe_client(host_id):
	"""
	A short-timeout client for `host_id`, used only to ask whether it is there.

	Separate from the working client because the SDK's timeout applies to every
	request, not just to connecting. The working client has to wait long enough
	for the slowest legitimate call — an /exec running whatever the user typed —
	while a reachability check that waits that long turns an unplugged machine
	into half a minute of a menu looking frozen.

	Raises HostUnavailable, like client().
	"""
	entry = host(host_id)
	if entry is None:
		raise HostUnavailable(host_id, "not configured")

	url = entry.get("url") or LOCAL_SOCKET_URL
	with _lock:
		cached = _probe_clients.get(host_id)
		if cached and cached[0] == url:
			return cached[1]

		built = _build_client(entry, timeout=PROBE_TIMEOUT_SECONDS)
		displaced = _probe_clients.get(host_id)
		_probe_clients[host_id] = (url, built)
	# As in client(): the entry it replaces belongs to a URL nobody will ask
	# for again, and its connection stays open until it is closed.
	_close((displaced,), host_id)
	return built


def ping(host_id):
	"""
	Whether `host_id` answers right now.

	Asked through the probe client, so a host that has gone away is reported as
	down in seconds rather than after a full operation timeout.

	A cached client whose connection has gone bad is dropped, so the next use
	reconnects instead of reusing a dead socket.
	"""
	try:
		connection = probe_client(host_id)
	except HostUnavailable as e:
		warning(str(e))
		return False
	try:
		connection.ping()
		return True
	except Exception as e:
		warning(f"Host {alias(host_id)} stopped answering: {e}")
		drop(host_id)
		return False


def _close(entries, host_id):
	"""
	Hands discarded clients' resources back, given their cache entries.

	Forgetting a client is not the same as releasing it. Behind an `ssh://` one
	there is an `ssh ... docker system dial-stdio` process, and drop() runs
	every time a connection goes bad — which for the event monitor is once per
	reconnection attempt. A host with a flaky link would leave abandoned ssh
	processes piling up inside the container for days.

	Takes the `(url, client)` entries the caches hold rather than bare clients,
	so no caller has to remember to unwrap them.

	Closing one that is already dead can raise, and by then there is nothing
	left to salvage: the client is gone from the cache either way.
	"""
	for entry in entries:
		if not entry:
			continue
		try:
			entry[1].close()
		except Exception as e:
			debug(f"Could not close the client for host {host_id}: {e}")


def drop(host_id):
	"""
	Forgets the cached clients for a host, and closes them.

	Called when a host's connection details change, and when a connection goes
	bad so that the next use reconnects instead of reusing a dead socket. Both
	the working client and the probe one go: they point at the same daemon, and
	leaving one behind means the next check answers about a connection the rest
	of the bot has already given up on.
	"""
	with _lock:
		removed = _clients.pop(host_id, None)
		probe = _probe_clients.pop(host_id, None)
	# Outside the lock: an ssh teardown is not instant, and nothing else needs
	# to wait on it to look up a different host.
	_close((removed, probe), host_id)
	if removed:
		debug(f"Dropped the cached client for host {host_id}")


def reachable_hosts():
	"""
	Every host that answers right now, as (host, client) pairs.

	Anything that has to sweep every host goes through here, so one machine
	being down degrades that sweep instead of breaking it.

	The client handed back is the working one, not the probe that answered the
	ping: callers operate through it, and they must not inherit the short
	timeout that only makes sense for asking whether a host is there.
	"""
	pairs = []
	for entry in hosts():
		if not ping(entry["id"]):
			continue
		try:
			pairs.append((entry, client(entry["id"])))
		except HostUnavailable as e:
			warning(str(e))
	return pairs


def host_status(host_id, deadline_seconds=5):
	"""
	Whether one host answers, as (ok, reason).

	For the screens that show a single machine: sweeping the whole fleet to
	draw one host means waiting on every other one as well.
	"""
	entry = host(host_id)
	if entry is None:
		return False, ""
	return status_snapshot(deadline_seconds, entries=[entry]).get(host_id, (False, ""))


def status_snapshot(deadline_seconds=5, entries=None):
	"""
	Whether each host answers, as {host_id: (ok, reason)}.

	Every host is checked at the same time, on its own thread, and the whole
	thing gives up after `deadline_seconds` no matter how many there are. A
	menu that pinged hosts one after another would hang for the sum of their
	timeouts, so opening it with one machine unplugged would look like the bot
	had frozen.

	A host that has not answered by the deadline is reported as failing, with
	the deadline as the reason: from the user's side "did not answer in five
	seconds" and "refused the connection" both mean the same thing, which is
	that it cannot be used right now.

	Giving up on the deadline does not stop the ping — a blocking socket read
	cannot be cancelled — so a check still running is remembered and no second
	one is started for that host. Otherwise a machine that hangs for a minute
	would leave a thread behind on every refresh of the menu.

	The probe client is what does the asking, so an abandoned check gives up on
	its own shortly after the deadline instead of holding a thread for as long
	as a real operation is allowed to take.
	"""
	configured = hosts() if entries is None else entries

	def check(entry):
		host_id = entry["id"]
		try:
			connection = probe_client(host_id)
			connection.ping()
			outcome = (True, "")
		except HostUnavailable as e:
			outcome = (False, e.reason)
		except Exception as e:
			drop(host_id)
			outcome = (False, str(e))
		with _probe_lock:
			_probe_results[host_id] = outcome
			_probes.pop(host_id, None)

	threads = []
	for entry in configured:
		host_id = entry["id"]
		with _probe_lock:
			# Discard whatever the last sweep left: this one reports now.
			_probe_results.pop(host_id, None)
			pending = _probes.get(host_id)
			if pending is not None and pending.is_alive():
				continue
			thread = threading.Thread(target=check, args=(entry,), daemon=True)
			_probes[host_id] = thread
		thread.start()
		threads.append(thread)

	deadline = time.monotonic() + deadline_seconds
	for thread in threads:
		thread.join(max(0, deadline - time.monotonic()))

	with _probe_lock:
		return {
			entry["id"]: _probe_results.pop(entry["id"], (False, f"no answer in {deadline_seconds}s"))
			for entry in configured
		}


def same_url(one, other):
	"""
	Whether two host URLs point at the same daemon.

	Only the obvious differences are folded in — a missing URL means the local
	socket, and a trailing slash is not a different machine. Nothing here tries
	to resolve names: two aliases of one host are not worth chasing.
	"""
	return ((one or LOCAL_SOCKET_URL).strip().rstrip("/")
			== (other or LOCAL_SOCKET_URL).strip().rstrip("/"))


def add_host(alias_name, url, tls=None, timeout=None):
	"""
	Registers a new host and returns its entry.

	The client is opened straight away: a host that cannot be reached is worth
	rejecting at the point someone adds it, while they still have the details
	in front of them.

	Raises HostRejected before that for a transport the SDK does not speak, and
	for a machine that is already registered: a second entry for the local
	socket would come in without `local`, so the guard in remove_host would not
	protect it, it would show up twice in every listing and its update cache
	would drift between the two ids.
	"""
	url = (url or "").strip()
	if not url.startswith(SUPPORTED_SCHEMES):
		raise HostRejected("scheme")
	if any(same_url(existing.get("url"), url) for existing in hosts()):
		raise HostRejected("duplicate")

	entry = {"id": generate_host_id(), "alias": alias_name, "url": url, "local": False}
	if tls:
		entry["tls"] = tls
	if timeout:
		entry["timeout"] = int(timeout)

	_build_client(entry, verify=True)  # raises HostUnavailable

	with _lock:
		configured = hosts()
		# Checked again under the lock: verifying the connection takes long
		# enough for the same host to have been added meanwhile.
		if any(same_url(existing.get("url"), url) for existing in configured):
			raise HostRejected("duplicate")
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

	Under the same lock as adding and removing: this rewrites the whole list
	from a copy it read, so a rename racing an add would drop the host that was
	just added.
	"""
	with _lock:
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
		discarded = list(_clients.items()) + list(_probe_clients.items())
		_clients.clear()
		_probe_clients.clear()
	for host_id, entry in discarded:
		_close((entry,), host_id)
	with _probe_lock:
		# The threads themselves are daemons and cannot be stopped; forgetting
		# them keeps a check against the old settings from being reused.
		_probes.clear()
		_probe_results.clear()
