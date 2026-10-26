"""
Persistent storage for everything the bot writes.

A single mapped volume holds it all, split by lifecycle:

	<root>/settings.json         user configuration (hand-edited, via /settings or /info)
	<root>/schedules.json        scheduled tasks
	<root>/state/state.json      mute expiry and other small runtime scalars
	<root>/state/updates.json    update-check cache (regenerable)

Configuration is small, read-mostly and authored by the user. State is
machine-written and disposable. They never share a file: the whole document is
rewritten on every change, so mixing them would mean rewriting the user's
settings on every cache write and risking them to a crash mid-write.

Every write goes to a temporary file and is moved into place with os.replace,
which is atomic on POSIX. A reader never sees a half-written document.
"""

import builtins
import json
import os
import threading
from contextlib import contextmanager

from logger import debug, error, warning

# Documented volume from 5.0.0 on.
CONFIG_ROOT = "/app/config"
# The volume composes written for 4.x map instead.
LEGACY_ROOT = "/app/schedule"

SETTINGS_FILE = "settings.json"
SCHEDULES_FILE = "schedules.json"
STATE_DIR = "state"
STATE_FILE = "state.json"
UPDATES_FILE = "updates.json"

DEFAULTS = {
	"bot": {
		"language": "ES",
		"button_columns": 2,
		"extended_messages": False,
		"multi_selection": True,
		"notification_channel": "",
		"check_updates": True,
		"check_update_every_hours": 4.0,
		"check_update_stopped_containers": True,
		# Anonymous usage statistics, sent once a day. See telemetry.py.
		"telemetry": True,
	},
	# Docker hosts the bot manages. Each entry is
	# {"id": "h_xxxx", "alias": "casa", "url": "unix:///var/run/docker.sock"}.
	# The id is generated once and never changes, so cache entries and
	# schedules survive an alias rename or a change of connection URL.
	"hosts": [],
	# How the bot treats each container, by host id and then container name:
	# {"h_xxxx": {"jellyfin": {"updates": "auto"}}}. See container_update_mode.
	"containers": {},
}

STATE_DEFAULTS = {
	# Epoch seconds until which notifications are silenced. 0 means not muted.
	"mute_until": 0,
	# Whether an update check has ever completed on this install. See
	# update_check_done.
	"update_check_done": False,
	# Since when each container with a setting has been missing from its host,
	# as {"<host_id>:<name>": epoch seconds}. See reconcile_containers.
	"containers_absent": {},
	# How each scheduled task's last run went, by task id. See schedules.record_run.
	"schedule_runs": {},
}

_lock = threading.RLock()
_root = None
_legacy_root_in_use = False
_persistent = False

_settings = None
_state = None
_updates = None
# Why settings.json could not be read, when it could not. While set, the file
# is never written: it holds the user's hosts and choices, and a typo in a hand
# edit must not end with all of it replaced by defaults.
_settings_unreadable = None

# Files whose writes are being coalesced by batch(), and whether each changed.
#
# `builtins.set` y no `set` a secas: este módulo define su propio `set()` unas
# líneas más abajo, y aquí hace falta el incorporado. Funciona por el orden
# —esta línea se ejecuta antes que el def— pero solo por eso: mover una de las
# dos, o volver a ejecutar el módulo, convierte esto en un TypeError. Decirlo
# explícitamente cuesta una palabra y quita la trampa.
_batch_depth = {}   # document name -> how many batch() blocks are deferring it
_batch_dirty = builtins.set()


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def _is_mounted(path):
	"""
	True if `path` is itself a mount point.

	os.path.ismount compares device ids, which is enough for the volumes Docker
	creates, but /proc/mounts is consulted too: getting this wrong means
	silently writing into the container filesystem and losing the user's
	settings on the next update.
	"""
	try:
		if os.path.ismount(path):
			return True
	except OSError:
		pass
	try:
		with open("/proc/mounts", "r", encoding="utf-8") as mounts:
			for line in mounts:
				fields = line.split()
				if len(fields) > 1 and fields[1] == path:
					return True
	except OSError:
		pass
	return False


def _is_persisted(path):
	"""
	True if anything written under `path` survives a container recreation.

	A volume does not have to be mapped at exactly this path to count: mapping
	a parent works just as well, which is what the development compose does
	when it maps the whole of /app. Only the container root itself is not
	persistence, since in a container that is always a mount point.
	"""
	current = os.path.abspath(path)
	while current and current != os.path.dirname(current):
		if _is_mounted(current):
			return True
		current = os.path.dirname(current)
	return False


def _resolve_root():
	"""
	Picks the directory the bot writes to, preferring whichever one is actually
	mapped.

	An existing 4.x compose maps LEGACY_ROOT and keeps working untouched; a new
	one maps CONFIG_ROOT and gets the documented layout. When neither is mapped
	directly the bot still boots on CONFIG_ROOT, and whether that survives a
	recreate depends on a parent volume, which is what the caller is told.

	Returns (root, uses_legacy_root, is_persistent).
	"""
	if _is_mounted(CONFIG_ROOT):
		return CONFIG_ROOT, False, True
	if _is_mounted(LEGACY_ROOT):
		return LEGACY_ROOT, True, True
	# Nothing mapped. Reuse a directory that already holds data over creating
	# an empty one, so a misconfigured compose at least stays self-consistent.
	if os.path.isdir(LEGACY_ROOT) and os.listdir(LEGACY_ROOT):
		return LEGACY_ROOT, True, _is_persisted(LEGACY_ROOT)
	return CONFIG_ROOT, False, _is_persisted(CONFIG_ROOT)


def init():
	"""
	Resolves the storage location and creates the directories. Safe to call
	more than once. Returns the root in use.
	"""
	global _root, _legacy_root_in_use, _persistent

	with _lock:
		if _root is not None:
			return _root

		_root, _legacy_root_in_use, _persistent = _resolve_root()
		try:
			os.makedirs(os.path.join(_root, STATE_DIR), exist_ok=True)
		except OSError as e:
			error(f"Cannot create the storage directory {_root}: {e}")

		if _legacy_root_in_use:
			debug(f"Using the legacy storage volume {LEGACY_ROOT}. Map {CONFIG_ROOT} instead when convenient.")
		if not _persistent:
			warning(f"{_root} is not a mapped volume: settings, schedules and the update cache will be lost when the container is recreated.")
		return _root


def root():
	"""Storage root in use, resolving it on first access."""
	return init()


def state_dir():
	"""Directory holding regenerable state."""
	return os.path.join(root(), STATE_DIR)


def schedules_path():
	"""Full path of the schedules file."""
	return os.path.join(root(), SCHEDULES_FILE)


def uses_legacy_root():
	"""True when the bot is running off the 4.x volume path."""
	init()
	return _legacy_root_in_use


def is_persistent():
	"""False when nothing the bot writes will survive a container recreation."""
	init()
	return _persistent


# ---------------------------------------------------------------------------
# Reading and writing documents
# ---------------------------------------------------------------------------

def _read_document(path, defaults, on_unreadable=None):
	"""
	Loads a JSON document, filling in anything missing from `defaults`.

	Unknown keys are kept so a downgrade does not discard what a newer version
	wrote, and a missing or unreadable file falls back to the defaults rather
	than aborting: a corrupt cache must never stop the bot from starting.

	`on_unreadable` is called with the reason when the file exists but cannot
	be used, so a caller whose file is not regenerable can stop writing to it.
	"""
	document = json.loads(json.dumps(defaults))
	try:
		with open(path, "r", encoding="utf-8") as handle:
			stored = json.load(handle)
	except FileNotFoundError:
		return document
	except Exception as e:
		warning(f"Cannot read {path}, falling back to defaults: {e}")
		if on_unreadable:
			on_unreadable(str(e))
		return document

	if not isinstance(stored, dict):
		warning(f"{path} does not contain an object, falling back to defaults")
		if on_unreadable:
			on_unreadable("not a JSON object")
		return document
	return _merge(document, stored)


def _merge(base, stored):
	"""Recursively overlays `stored` onto `base`, keeping unknown keys."""
	for key, value in stored.items():
		if isinstance(value, dict) and isinstance(base.get(key), dict):
			base[key] = _merge(base[key], value)
		else:
			base[key] = value
	return base


def write_document(path, document):
	"""
	Writes `document` atomically, creating the parent directory if needed.

	The flush and fsync are what make the rename atomic in practice: without
	them the metadata operation can reach the disk before the contents, and a
	power cut in between leaves a settings file that exists and is empty.

	Raises when it cannot write, leaving the file as it was: a caller told
	nothing would go on as if it had been saved —a scheduled task reported as
	added that was gone at the next start.
	"""
	temporary = f"{path}.tmp"
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(temporary, "w", encoding="utf-8") as handle:
			json.dump(document, handle, indent=4, ensure_ascii=False)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, path)
	except Exception:
		try:
			os.remove(temporary)
		except OSError:
			pass
		raise


def _flush(name):
	"""
	Persists one document, or defers it when inside batch().

	`name` is one of "settings", "state" or "updates".
	"""
	if _batch_depth.get(name, 0) > 0:
		_batch_dirty.add(name)
		return
	if name == "settings":
		if _settings_unreadable is not None:
			debug(f"Not writing {SETTINGS_FILE}: it could not be read, and writing would replace it")
			return
		path, document = os.path.join(root(), SETTINGS_FILE), _settings
	elif name == "state":
		path, document = os.path.join(state_dir(), STATE_FILE), _state
	elif name == "updates":
		path, document = os.path.join(state_dir(), UPDATES_FILE), _updates
	else:
		return
	# Kept in memory either way, and written again with the next change: a
	# setting that cannot be saved still applies until the bot restarts.
	try:
		write_document(path, document)
	except Exception as e:
		error(f"Cannot write {path}: {e}")


@contextmanager
def batch(*names):
	"""
	Coalesces the writes made inside the block into one write per document.

	`names` limits it to some documents ("settings", "state", "updates"); with
	none, all three. The limit is what makes it safe to wrap something slow:
	the deferral is global, not per thread, so a block deferring the settings
	would hold back a change made from /settings on another thread until it
	ended, and lose it if the container stopped first.

	The update check wraps each host in batch("updates"): one write of the
	cache per host instead of one per container, which matters on the SD cards
	this runs on.
	"""
	deferred = names or ("settings", "state", "updates")
	with _lock:
		for name in deferred:
			_batch_depth[name] = _batch_depth.get(name, 0) + 1
	try:
		yield
	finally:
		with _lock:
			for name in deferred:
				_batch_depth[name] -= 1
			ready = sorted(n for n in _batch_dirty if _batch_depth.get(n, 0) == 0)
			_batch_dirty.difference_update(ready)
			for name in ready:
				_flush(name)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings_document():
	global _settings

	if _settings is None:
		_settings = _read_document(os.path.join(root(), SETTINGS_FILE), DEFAULTS,
									on_unreadable=_mark_settings_unreadable)
	return _settings


def _mark_settings_unreadable(reason):
	global _settings_unreadable

	_settings_unreadable = reason
	error(f"{settings_path()} cannot be read ({reason}). Running on defaults and "
			f"leaving the file untouched: fix it and restart the container.")


def settings_unreadable():
	"""Why settings.json could not be read, or None when it was fine."""
	with _lock:
		_settings_document()
		return _settings_unreadable


def _walk(document, dotted_key, create=False):
	"""
	Resolves a dotted key to its container and final segment.

	Returns (container, last_segment), or (None, None) when the path does not
	exist and `create` is False.
	"""
	segments = dotted_key.split(".")
	container = document
	for segment in segments[:-1]:
		nested = container.get(segment)
		if not isinstance(nested, dict):
			if not create:
				return None, None
			nested = {}
			container[segment] = nested
		container = nested
	return container, segments[-1]


def _default_for(dotted_key):
	container, last = _walk(json.loads(json.dumps(DEFAULTS)), dotted_key)
	if container is None:
		return None
	return container.get(last)


def _as_type_of(default, value):
	"""
	`value` read the way `default` is typed, for what a hand edit can get wrong.

	Callers test these settings for truth, and any non-empty string is true:
	`"telemetry": "false"` written by hand turned the statistics on. A string
	is on only when it plainly says so, the way the 4.x variables were read.
	"""
	if isinstance(default, bool) and not isinstance(value, bool):
		if isinstance(value, str):
			return value.strip().lower() in ("1", "true", "yes", "on")
		if isinstance(value, (int, float)):
			return bool(value)
		return default
	if isinstance(default, int) and not isinstance(default, bool) and isinstance(value, str):
		try:
			return int(value.strip())
		except ValueError:
			return default
	return value


def get(dotted_key):
	"""
	Reads a setting by dotted path, e.g. get("bot.language").

	Unset keys fall back to DEFAULTS, so a settings file written by an older
	version never leaves a new setting undefined.
	"""
	with _lock:
		container, last = _walk(_settings_document(), dotted_key)
		if container is None or last not in container:
			return _default_for(dotted_key)
		value = _as_type_of(_default_for(dotted_key), container[last])
		# A copy of anything mutable. Handing out the live list of hosts let a
		# caller change an entry in place without this lock —pausing a host
		# did— while another thread was in json.dump of the same document:
		# "dictionary changed size during iteration", and a lost write.
		if isinstance(value, (dict, list)):
			return json.loads(json.dumps(value))
		return value


def set(dotted_key, value):
	"""Writes a setting by dotted path and persists it."""
	with _lock:
		container, last = _walk(_settings_document(), dotted_key, create=True)
		container[last] = value
		_flush("settings")
		return value


def toggle(dotted_key):
	"""Inverts a boolean setting and returns the new value."""
	with _lock:
		return set(dotted_key, not bool(get(dotted_key)))


def settings_path():
	"""Full path of the settings file."""
	return os.path.join(root(), SETTINGS_FILE)


def settings_exists():
	"""True once a settings file has been written."""
	return os.path.exists(settings_path())


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_document():
	global _state

	if _state is None:
		_state = _read_document(os.path.join(state_dir(), STATE_FILE), STATE_DEFAULTS)
	return _state


def state_get(key):
	"""Reads a runtime scalar, falling back to STATE_DEFAULTS."""
	with _lock:
		document = _state_document()
		if key not in document:
			return STATE_DEFAULTS.get(key)
		return document[key]


def state_set(key, value):
	"""Writes a runtime scalar and persists it."""
	with _lock:
		_state_document()[key] = value
		_flush("state")
		return value


# ---------------------------------------------------------------------------
# Update-check cache
# ---------------------------------------------------------------------------
#
# Keyed by host id and container name, which together identify a container
# uniquely and stably: Docker enforces unique names per host, and a container
# recreated under the same name is the same container as far as updates go.
#
# The image lives in the value rather than the key. Changing a tag has to
# invalidate the entry, but keying on the image meant every tag a container had
# ever run left an entry behind for good. Comparing the stored image on read
# gives the same invalidation with one entry per container.

def _updates_document():
	global _updates

	if _updates is None:
		_updates = _read_document(
			os.path.join(state_dir(), UPDATES_FILE),
			{"version": 1, "entries": {}},
		)
		if not isinstance(_updates.get("entries"), dict):
			_updates["entries"] = {}
	return _updates


def _update_key(host_id, container_name):
	# ':' cannot appear in a container name, so it can never be confused with
	# part of one.
	return f"{host_id}:{container_name}"


def update_status(host_id, container_name, image):
	"""
	Whether `container_name` has a pending update.

	Returns True, False, or None when there is nothing usable cached: no entry,
	or an entry recorded against a different image (the tag changed since, so
	what was cached no longer says anything about what runs now).
	"""
	with _lock:
		entry = _updates_document()["entries"].get(_update_key(host_id, container_name))
		if not isinstance(entry, dict):
			return None
		if image and entry.get("image") != image:
			return None
		value = entry.get("update")
		return value if isinstance(value, bool) else None


def set_update_status(host_id, container_name, image, has_update, checked_at=None, versions=None):
	"""
	Records the outcome of an update check.

	`versions` is (running, available), either of them None when the image
	does not say: kept so every message about this update can say what it
	goes from and to without pulling anything again.
	"""
	with _lock:
		entry = {"image": image, "update": bool(has_update)}
		if checked_at is not None:
			entry["checked"] = checked_at
		if versions and any(versions):
			entry["versions"] = [versions[0], versions[1]]
		_updates_document()["entries"][_update_key(host_id, container_name)] = entry
		_flush("updates")


def update_checked_at(host_id, container_name):
	"""When this container's image was last checked for an update, or None."""
	with _lock:
		entry = _updates_document()["entries"].get(_update_key(host_id, container_name))
		checked = entry.get("checked") if isinstance(entry, dict) else None
		return checked if isinstance(checked, str) else None


def update_versions(host_id, container_name):
	"""(running, available) as last recorded, or (None, None)."""
	with _lock:
		entry = _updates_document()["entries"].get(_update_key(host_id, container_name))
		versions = entry.get("versions") if isinstance(entry, dict) else None
		if not isinstance(versions, list) or len(versions) != 2:
			return None, None
		return tuple(v if isinstance(v, str) and v else None for v in versions)


def forget_update_status(host_id, container_name):
	"""Drops a container's cached update state, e.g. when it is deleted."""
	with _lock:
		removed = _updates_document()["entries"].pop(
			_update_key(host_id, container_name), None
		)
		if removed is not None:
			_flush("updates")


def forget_host_updates(host_id):
	"""
	Drops the cached update state of every container on one host.

	For a host that was removed: nothing else ever clears those entries, and
	they stayed in updates.json for good.
	"""
	prefix = _update_key(host_id, "")
	with _lock:
		entries = _updates_document()["entries"]
		gone = [key for key in entries if key.startswith(prefix)]
		for key in gone:
			del entries[key]
		if gone:
			_flush("updates")


def update_entries():
	"""The whole cache, as {key: entry}. Read-only view for callers."""
	with _lock:
		return dict(_updates_document()["entries"])


def has_update_cache():
	"""
	False when the cache holds nothing yet.

	A cold cache means the next check is the first one, and its findings are
	pre-existing rather than new, so it must populate silently instead of
	announcing every pending update at once.
	"""
	with _lock:
		return bool(_updates_document()["entries"])


def clear_update_cache():
	"""Empties the cache. It is regenerable, so this is always safe."""
	with _lock:
		_updates_document()["entries"] = {}
		_flush("updates")


def update_check_done():
	"""
	Whether an update check has ever finished on this install.

	An empty cache used to be the sign of a first pass, which has to fill it
	without announcing anything. But the cache also empties when its last
	entry is forgotten —the only container stopped being checked, or was
	removed—, and the pass after that took every pending update for an old
	one: it saved them as announced without saying a word.
	"""
	return bool(state_get("update_check_done")) or has_update_cache()


def mark_update_check_done():
	"""Records that a pass finished, once."""
	if not state_get("update_check_done"):
		state_set("update_check_done", True)


# ---------------------------------------------------------------------------
# Per-container settings
# ---------------------------------------------------------------------------
#
# How the bot treats each container, chosen from /info or /settings, under
# "containers" in settings.json:
#
#	"containers": {"h_a91c": {"jellyfin": {"updates": "auto"}}}
#
# Configuration the user chose, so in settings.json and not in state/: it
# cannot be regenerated, and it takes on the rule that an unreadable settings
# file is never written over. Keyed like the update cache, by host id and
# name, because every update recreates a container under a new id and the
# same name.
#
# Only what differs from the default is stored: no entry is "tell me", and a
# container whose settings all go back to the default leaves the file.
#
# Functions of its own rather than get/set with a dotted path: Docker allows
# dots in a container name, and the path would split it in two.

# What the update check does with a container. None, the default, is to
# announce what it finds; these are the other two, the same as the
# DCB-Auto-Update and DCB-Ignore-Check-Updates labels.
UPDATE_MODES = ("auto", "ignore")


def _as_update_mode(value):
	"""A stored mode as one of UPDATE_MODES, or None for anything else."""
	if not isinstance(value, str):
		return None
	value = value.strip().lower()
	return value if value in UPDATE_MODES else None


def _stored_containers():
	"""The "containers" object as stored, or {} when it is missing or not one."""
	containers = _settings_document().get("containers")
	return containers if isinstance(containers, dict) else {}


def container_update_mode(host_id, name):
	"""How the update check treats a container: "auto", "ignore", or None."""
	with _lock:
		on_host = _stored_containers().get(host_id)
		entry = on_host.get(name) if isinstance(on_host, dict) else None
		return _as_update_mode(entry.get("updates")) if isinstance(entry, dict) else None


def set_container_update_mode(host_id, name, mode):
	"""
	Stores how the update check treats a container. None goes back to the
	default.

	Returns False when it could not be stored: with settings.json unreadable
	the change would only live in memory, and be gone at the next restart
	without anyone having been told.
	"""
	mode = _as_update_mode(mode)
	if not isinstance(host_id, str) or not isinstance(name, str):
		return False
	with _lock:
		document = _settings_document()
		if _settings_unreadable is not None:
			return False
		containers = document.get("containers")
		if not isinstance(containers, dict):
			containers = document["containers"] = {}
		on_host = containers.get(host_id)
		if not isinstance(on_host, dict):
			on_host = containers[host_id] = {}
		entry = on_host.get(name)
		if not isinstance(entry, dict):
			entry = on_host[name] = {}
		if mode is None:
			entry.pop("updates", None)
		else:
			entry["updates"] = mode
		# Nothing left to say about it: out of the file, and its host too.
		if not entry:
			del on_host[name]
		if not on_host:
			del containers[host_id]
		_flush("settings")
		_forget_absence(lambda key: key == _update_key(host_id, name))
		return True


def forget_container_settings(host_id, name):
	"""Drops everything stored about one container. True when there was something."""
	with _lock:
		on_host = _stored_containers().get(host_id)
		if not isinstance(on_host, dict) or name not in on_host:
			return False
		del on_host[name]
		if not on_host:
			del _settings_document()["containers"][host_id]
		_flush("settings")
		_forget_absence(lambda key: key == _update_key(host_id, name))
		return True


def forget_host_containers(host_id):
	"""
	Drops the settings of every container on one host, for a host removed.

	Not for one that is down or paused: those keep theirs until they are back.
	"""
	with _lock:
		containers = _stored_containers()
		if host_id not in containers:
			return False
		del containers[host_id]
		_flush("settings")
		_forget_absence(lambda key: key.startswith(_update_key(host_id, "")))
		return True


def _forget_absence(matches):
	"""
	Drops the absence marks `matches` picks, for settings set or gone.

	A mark outliving its setting counted for the next one: a container set
	again weeks later lost it the first time it was recreated, with a
	message saying it had been missing for a month.
	"""
	absent = state_get("containers_absent")
	if isinstance(absent, dict) and any(matches(key) for key in absent):
		state_set("containers_absent", {key: since for key, since in absent.items() if not matches(key)})


def container_settings():
	"""
	Every stored per-container setting, {host_id: {name: {...}}}. A copy.

	Only what has the shape: a hand edit that left a host or a container as
	something other than an object is no setting at all, here as it is for
	container_update_mode, and must not reach the screens that count them.
	"""
	with _lock:
		return json.loads(json.dumps({
			host_id: {name: entry for name, entry in on_host.items() if isinstance(entry, dict)}
			for host_id, on_host in _stored_containers().items() if isinstance(on_host, dict)}))


def reconcile_containers(present, now, grace_seconds):
	"""
	Forgets the settings of containers that are gone for good.

	`present` is {host_id: names} for the hosts whose listing came back whole,
	just now. Only those are judged: a host that is down, paused or too slow
	says nothing about its containers, and a setting is not lost because a
	machine was switched off for a week. An empty listing is not judged either:
	a daemon that answers with nothing is more likely a wrong one than one
	where everything was removed.

	A container missing from its host is noted as absent from `now`, and its
	settings go when a listing still misses it `grace_seconds` later. Not
	straight away, because recreating a container is removing it and creating
	it again: `docker compose up -d`, Watchtower, and a `down` followed by an
	`up` all leave a moment —or a weekend— without it. Docker's own events
	cannot tell those apart either, and they are lost while the bot is down.

	The marks are in state/, so a restart does not start the count again, and
	losing them only delays a cleanup. The time a host spends down counts:
	what was already missing before it went, and is still missing when it is
	back, has been missing all along.

	Returns the (host_id, name) pairs whose settings were forgotten.
	"""
	with _lock:
		_settings_document()
		if _settings_unreadable is not None:
			return []
		containers = _stored_containers()
		absent = state_get("containers_absent")
		absent = dict(absent) if isinstance(absent, dict) else {}
		forgotten = []
		with batch("settings"):
			for host_id, names in present.items():
				names = builtins.set(names or ())
				on_host = containers.get(host_id)
				if not names or not isinstance(on_host, dict):
					continue
				# Only what is a setting: an entry a hand edit left as something
				# other than an object is none, and has nothing to count down.
				for name in [n for n, entry in on_host.items() if isinstance(entry, dict)]:
					key = _update_key(host_id, name)
					if name in names:
						absent.pop(key, None)
						continue
					since = absent.get(key)
					if not isinstance(since, (int, float)):
						absent[key] = now
					elif now - since >= grace_seconds:
						forget_container_settings(host_id, name)
						absent.pop(key, None)
						forgotten.append((host_id, name))
		# A mark for a setting that is no longer there —changed back to the
		# default, or its host removed— has nothing left to count down to. Nor
		# one that cannot be placed at all: written by hand without a host, or
		# for a host whose value is not an object.
		stored = _stored_containers()

		def placed(key):
			host_id, separator, name = str(key).partition(":")
			on_host = stored.get(host_id)
			return bool(separator) and isinstance(on_host, dict) and isinstance(on_host.get(name), dict)
		absent = {key: since for key, since in absent.items() if placed(key)}
		if absent != state_get("containers_absent"):
			state_set("containers_absent", absent)
		return forgotten


def reload():
	"""Drops every in-memory document, forcing the next read to hit disk."""
	global _settings, _settings_unreadable, _state, _updates

	with _lock:
		_settings = None
		_settings_unreadable = None
		_state = None
		_updates = None
