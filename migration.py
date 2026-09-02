"""
One-time migrations, run at startup before anything reads a setting.

Updating from 4.x has to be a no-op from the user's side: the settings file is
seeded from the environment variables already in their compose, so every value
they had is the value they keep. What changes is where those values live from
then on.

Running this again on an already-migrated install is safe: the seeding is
skipped outright, and every other step is idempotent.
"""

import os
import shutil
import time
import uuid
from collections import namedtuple

import store
from config import SETTINGS_FROM_ENV
from logger import debug, error, warning

# Directory 4.x wrote its pickled update cache into, relative to the working
# directory. It was never part of a mapped volume.
LEGACY_CACHE_DIR = "./cache/"

# File 4.x kept the mute expiry in, inside the schedules volume.
LEGACY_MUTE_FILE = ".muted_until"

LOCAL_SOCKET_URL = "unix:///var/run/docker.sock"

# What the startup needs to know once storage is up to date.
#
# `ask_for_language` is true only on a genuinely new install: one where there
# were no settings to read and no LANGUAGE to import either, so the bot has no
# way of knowing which language to speak and may as well ask.
MigrationResult = namedtuple("MigrationResult", ["host_id", "ask_for_language"])


def run():
	"""
	Brings storage up to date.

	Only the seeding is guarded against running twice, because it is the one
	step that would be destructive if repeated: it would overwrite whatever the
	user has since changed from /settings. The rest are idempotent and run on
	every start, so nothing can be left half done if the container is killed
	during the first one.
	"""
	store.init()
	seeded = _seed_settings_from_env()
	host_id = _ensure_local_host()
	_warn_deprecated_env(seeded)
	_migrate_mute_file()
	_discard_legacy_cache()
	ask_for_language = seeded and not os.environ.get("LANGUAGE")
	return MigrationResult(host_id=host_id, ask_for_language=ask_for_language)


def _seed_settings_from_env():
	"""
	Writes the settings file from the environment the first time only.

	Returns True when it seeded, False when a settings file already existed.
	After the first run the environment is ignored, so this must not overwrite
	anything: a user who changed a setting from Telegram would see it revert on
	the next restart.
	"""
	if store.settings_exists():
		return False

	with store.batch():
		for variable, (key, parse) in SETTINGS_FROM_ENV.items():
			raw = os.environ.get(variable)
			if raw is None or raw == "":
				continue
			try:
				store.set(key, parse(raw))
			except (TypeError, ValueError):
				warning(f"Ignoring {variable}={raw!r}: not a valid value for {key}")

		# Touch the file even when the compose set nothing, so the next start
		# knows the seeding already happened and does not try again.
		store.set("settings_version", 1)
	debug(f"Settings file created at {store.settings_path()}")
	return True


def _ensure_local_host():
	"""
	Makes sure the local Docker socket is registered as a host, and returns its
	id.

	The id is generated once and never changes. Cache entries and schedules are
	keyed on it rather than on the alias precisely so that renaming a host, or
	moving it from one connection URL to another, does not orphan them.
	"""
	hosts = store.get("hosts") or []
	for host in hosts:
		if isinstance(host, dict) and host.get("local") and host.get("id"):
			return host["id"]

	host_id = _generate_host_id(hosts)
	hosts.append({
		"id": host_id,
		"alias": "local",
		"url": LOCAL_SOCKET_URL,
		"local": True,
	})
	store.set("hosts", hosts)
	debug(f"Registered the local Docker host as {host_id}")
	return host_id


def _generate_host_id(hosts):
	"""A short id that no existing host is using."""
	taken = {host.get("id") for host in hosts if isinstance(host, dict)}
	while True:
		candidate = f"h_{uuid.uuid4().hex[:4]}"
		if candidate not in taken:
			return candidate


def _warn_deprecated_env(seeded):
	"""
	Reports variables that no longer do anything.

	Staying quiet would be worse than noisy: the user edits the compose,
	restarts, sees no change, and has no way of knowing the value is now read
	from somewhere else.
	"""
	present = [name for name in SETTINGS_FROM_ENV if os.environ.get(name) not in (None, "")]
	if not present:
		return
	if seeded:
		debug(f"Imported into {store.settings_path()}: {', '.join(present)}. They can be removed from the compose.")
		return
	warning(
		f"These variables are no longer read and can be removed from the compose: {', '.join(present)}. "
		f"Their values are now managed with /settings."
	)


def _migrate_mute_file():
	"""
	Carries an active mute over from the 4.x file into the state document.

	Unlike the update cache this is worth migrating: someone who silences the
	bot for six hours and updates it in the meantime would otherwise get every
	notification back immediately, with nothing to explain why. An expired or
	absent mute just removes the file.
	"""
	legacy_path = os.path.join(store.root(), LEGACY_MUTE_FILE)
	if not os.path.isfile(legacy_path):
		return

	mute_until = 0
	try:
		with open(legacy_path, "r", encoding="utf-8") as handle:
			mute_until = float(handle.readline().strip() or 0)
	except (OSError, ValueError) as e:
		warning(f"Could not read the old mute file {legacy_path}: {e}")

	# Only carry it over when it is still in the future, and never on top of a
	# mute the new state file already holds.
	if mute_until > time.time() and not store.state_get("mute_until"):
		store.state_set("mute_until", mute_until)
		remaining = int((mute_until - time.time()) / 60)
		debug(f"Carried the active mute over from {LEGACY_MUTE_FILE} ({remaining} minutes left)")

	try:
		os.remove(legacy_path)
	except OSError as e:
		warning(f"Could not remove the old mute file {legacy_path}: {e}")


def _discard_legacy_cache():
	"""
	Removes the 4.x pickled update cache.

	It is not migrated, for two reasons. It lived outside any mapped volume, so
	on most installs it was already being lost on every container recreation;
	and it stored the *translated* status message rather than a boolean, which
	made the entries unreadable as soon as the language changed. The first check
	cycle rebuilds it, silently, because a cold cache means those updates are
	pre-existing rather than new.
	"""
	if not os.path.isdir(LEGACY_CACHE_DIR):
		return
	try:
		discarded = len(os.listdir(LEGACY_CACHE_DIR))
		shutil.rmtree(LEGACY_CACHE_DIR)
		if discarded:
			debug(f"Discarded {discarded} entries of the 4.x update cache. It will be rebuilt on the next check.")
	except OSError as e:
		error(f"Could not remove the old cache directory {LEGACY_CACHE_DIR}: {e}")
