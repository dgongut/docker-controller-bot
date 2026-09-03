"""
Loads the bot for testing, without Telegram and without Docker.

The bot is a single module that does its whole setup at import time: it
validates the environment, resolves storage, runs migrations and instantiates
the Telegram client. Importing it under a temporary storage root and a stubbed
Docker client is enough to exercise almost everything without a daemon or a
token, which is what makes these tests runnable anywhere.
"""

import importlib.util
import os
import sys
import tempfile
import warnings
from unittest.mock import MagicMock

warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Variables the bot refuses to start without. The values only have to be
# well-formed: nothing here ever reaches Telegram.
#
# CONTAINER_NAME is in here on purpose even though the bot no longer needs it:
# the tests run outside a container, where there is no /proc/self/mountinfo to
# read, so this is what keeps the fallback path — matching the name on the
# local host — under test. The detection path itself is covered by
# test_identity.py against captured /proc files, and by the tests that stand in
# for own_container_ids().
BOOTSTRAP_ENV = {
	"TELEGRAM_TOKEN": "111111:test-token",
	"TELEGRAM_ADMIN": "4405089",
	"CONTAINER_NAME": "docker-controller-bot",
}

# Set as soon as this module is imported, before anything can pull in config,
# which reads the environment once at import time and exits when the token is
# missing.
os.environ.update(BOOTSTRAP_ENV)

# The ones that moved into /settings. Cleared by default so a test starts from
# the documented defaults instead of whatever is in the developer's shell.
SETTINGS_ENV = (
	"LANGUAGE", "BUTTON_COLUMNS", "EXTENDED_MESSAGES", "MULTI_SELECTION",
	"CHECK_UPDATES", "CHECK_UPDATE_EVERY_HOURS",
	"CHECK_UPDATE_STOPPED_CONTAINERS", "TELEGRAM_NOTIFICATION_CHANNEL",
)


def tempdir():
	"""A throwaway directory, for the tests that need a file to read."""
	import tempfile
	return tempfile.mkdtemp()


def temp_storage(env=None, legacy=False, seed_files=None):
	"""
	Points the store at a fresh temporary root and returns it.

	`legacy` maps the 4.x directory instead of the 5.0 one, so the upgrade path
	can be exercised. `seed_files` writes files into it first, which is how a
	pre-existing 4.x install is simulated.
	"""
	if REPO not in sys.path:
		sys.path.insert(0, REPO)
	import store

	root = tempfile.mkdtemp()
	# The 4.x update cache lived at ./cache/, relative to the working
	# directory, so tests have to run from somewhere disposable.
	os.chdir(root)

	store.CONFIG_ROOT = os.path.join(root, "config")
	store.LEGACY_ROOT = os.path.join(root, "schedule")
	os.makedirs(store.LEGACY_ROOT if legacy else store.CONFIG_ROOT)
	store._root = None
	store.reload()

	for name in SETTINGS_ENV:
		os.environ.pop(name, None)
	os.environ.update(BOOTSTRAP_ENV)
	os.environ.update(env or {})

	for relative, content in (seed_files or {}).items():
		path = os.path.join(store.LEGACY_ROOT if legacy else store.CONFIG_ROOT, relative)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w", encoding="utf-8") as handle:
			handle.write(content)

	return store, root


# Modules that register commands or inline-button callbacks by being imported.
# The entry point imports them in this order for the same reason.
REGISTRARS = ("commands", "callbacks")


_loaded = None


def load_bot(env=None):
	"""
	Imports the core module under a temporary storage root, followed by
	whatever registers commands and callbacks.

	Cached, and deliberately so: there is one bot per process. Importing it
	twice would hit the callback registry's duplicate check, which is there to
	stop two handlers claiming one button. `env` therefore only applies to the
	first call, which is why anything asserting on start-up values snapshots
	them instead of reading them live.

	Docker is stubbed rather than mocked selectively: the core builds a
	DockerManager at import time, and no test here needs a real daemon.
	"""
	global _loaded
	if _loaded is not None:
		return _loaded

	store, root = temp_storage(env)

	import docker
	# Both entry points the bot uses: from_env for anything still resolving the
	# environment, DockerClient for the host registry.
	docker.from_env = lambda *args, **kwargs: MagicMock()
	docker.DockerClient = lambda *args, **kwargs: MagicMock()

	import host_registry
	host_registry.reset()

	core = _import("core")
	for name in REGISTRARS:
		_import(name)
	_loaded = (core, store, root)
	return _loaded


def _import(name):
	spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, f"{name}.py"))
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


def quiet(module):
	"""Silences the message queue, for tests that trigger error paths."""
	module.send_message = lambda *args, **kwargs: None
	module.edit_message_text = lambda *args, **kwargs: None
	module.delete_message = lambda *args, **kwargs: None


def keyboard_callbacks(markup):
	"""Every callback_data in a keyboard, row by row."""
	return [button.callback_data for row in markup.keyboard for button in row]


def keyboard_labels(markup):
	"""Every button label in a keyboard."""
	return [button.text for row in markup.keyboard for button in row]


def capture_edit(module, function, *args):
	"""
	Runs a function that repaints a message and returns the markup it sent.

	The rendering helpers edit in place instead of returning anything, so the
	only way to look at what they built is to intercept the edit.
	"""
	captured = {}
	original = module.edit_message_text
	module.edit_message_text = lambda text, chat, message, **kwargs: captured.update(
		text=text, markup=kwargs.get("reply_markup"))
	try:
		function(*args)
	finally:
		module.edit_message_text = original
	return captured["text"], captured["markup"]
