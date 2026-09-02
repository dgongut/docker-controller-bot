"""
Persistent storage for everything the bot writes.

A single mapped volume holds it all, split by lifecycle:

	<root>/settings.json         user configuration (hand-edited or via /settings)
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
	},
	# Docker hosts the bot manages. Each entry is
	# {"id": "h_xxxx", "alias": "casa", "url": "unix:///var/run/docker.sock"}.
	# The id is generated once and never changes, so cache entries and
	# schedules survive an alias rename or a change of connection URL.
	"hosts": [],
	# Host paths later features need to write into (stacks, backups), keyed by
	# host id.
	"paths": {},
}

STATE_DEFAULTS = {
	# Epoch seconds until which notifications are silenced. 0 means not muted.
	"mute_until": 0,
}

_lock = threading.RLock()
_root = None
_legacy_root_in_use = False
_persistent = False

_settings = None
_state = None
_updates = None

# Files whose writes are being coalesced by batch(), and whether each changed.
_batch_depth = 0
_batch_dirty = set()


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

def _read_document(path, defaults):
	"""
	Loads a JSON document, filling in anything missing from `defaults`.

	Unknown keys are kept so a downgrade does not discard what a newer version
	wrote, and a missing or unreadable file falls back to the defaults rather
	than aborting: a corrupt cache must never stop the bot from starting.
	"""
	document = json.loads(json.dumps(defaults))
	try:
		with open(path, "r", encoding="utf-8") as handle:
			stored = json.load(handle)
	except FileNotFoundError:
		return document
	except Exception as e:
		warning(f"Cannot read {path}, falling back to defaults: {e}")
		return document

	if not isinstance(stored, dict):
		warning(f"{path} does not contain an object, falling back to defaults")
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


def _write_document(path, document):
	"""
	Writes `document` atomically, creating the parent directory if needed.

	The flush and fsync are what make the rename atomic in practice: without
	them the metadata operation can reach the disk before the contents, and a
	power cut in between leaves a settings file that exists and is empty.
	"""
	temporary = f"{path}.tmp"
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(temporary, "w", encoding="utf-8") as handle:
			json.dump(document, handle, indent=4, ensure_ascii=False)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, path)
	except Exception as e:
		error(f"Cannot write {path}: {e}")
		try:
			os.remove(temporary)
		except OSError:
			pass


def _flush(name):
	"""
	Persists one document, or defers it when inside batch().

	`name` is one of "settings", "state" or "updates".
	"""
	if _batch_depth > 0:
		_batch_dirty.add(name)
		return
	if name == "settings":
		_write_document(os.path.join(root(), SETTINGS_FILE), _settings)
	elif name == "state":
		_write_document(os.path.join(state_dir(), STATE_FILE), _state)
	elif name == "updates":
		_write_document(os.path.join(state_dir(), UPDATES_FILE), _updates)


@contextmanager
def batch():
	"""
	Coalesces the writes made inside the block into one write per document.

	An update-check cycle touches every container, and rewriting the whole
	cache once per container would be hundreds of writes per pass. Wrapping the
	cycle keeps it to one, which matters on the SD cards this runs on.
	"""
	global _batch_depth

	with _lock:
		_batch_depth += 1
	try:
		yield
	finally:
		with _lock:
			_batch_depth -= 1
			if _batch_depth == 0:
				pending = sorted(_batch_dirty)
				_batch_dirty.clear()
				for name in pending:
					_flush(name)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings_document():
	global _settings

	if _settings is None:
		_settings = _read_document(os.path.join(root(), SETTINGS_FILE), DEFAULTS)
	return _settings


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
		return container[last]


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


def set_update_status(host_id, container_name, image, has_update, checked_at=None):
	"""Records the outcome of an update check."""
	with _lock:
		entry = {"image": image, "update": bool(has_update)}
		if checked_at is not None:
			entry["checked"] = checked_at
		_updates_document()["entries"][_update_key(host_id, container_name)] = entry
		_flush("updates")


def forget_update_status(host_id, container_name):
	"""Drops a container's cached update state, e.g. when it is deleted."""
	with _lock:
		removed = _updates_document()["entries"].pop(
			_update_key(host_id, container_name), None
		)
		if removed is not None:
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


def reload():
	"""Drops every in-memory document, forcing the next read to hit disk."""
	global _settings, _state, _updates

	with _lock:
		_settings = None
		_state = None
		_updates = None
