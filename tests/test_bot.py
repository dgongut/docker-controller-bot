"""
The bot module: settings, menus and the callback registry.

Imported once with Docker stubbed and storage in a temporary directory, then
every test runs against that instance. They share it deliberately: the module
is global state by design, and loading it is the slow part.
"""

import io
import json
import os
import re
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

import i18n

import callback_registry

dcb, store, _root = harness.load_bot(env={"LANGUAGE": "ES", "BUTTON_COLUMNS": "3"})
import callbacks
import commands

# What the seeding produced, captured before any test can change it: these
# tests share one module instance on purpose, so nothing that asserts on
# start-up values may read them live.
SEEDED = {
	"language": store.get("bot.language"),
	"button_columns": dcb.button_columns(),
	"host_id": dcb.LOCAL_HOST_ID,
}


# ---------------------------------------------------------------------------
# Settings, read at the point of use
# ---------------------------------------------------------------------------

def test_settings_are_seeded_from_the_environment():
	assert SEEDED["language"] == "ES"
	assert SEEDED["button_columns"] == 3
	assert SEEDED["host_id"].startswith("h_")


def test_the_language_applies_without_a_restart():
	store.set("bot.language", "EN")
	assert dcb.get_text("check_for_updates") == i18n.load_locale("en")["check_for_updates"]
	store.set("bot.language", "IT")
	assert dcb.get_text("check_for_updates") == i18n.load_locale("it")["check_for_updates"]
	# An unsupported value falls back rather than crashing on a missing file.
	store.set("bot.language", "XX")
	assert dcb.get_text("check_for_updates") == i18n.load_locale("es")["check_for_updates"]
	store.set("bot.language", "ES")


def test_button_columns_are_clamped():
	"""A hand-edited file must not produce a keyboard Telegram rejects."""
	for stored, expected in ((0, 1), (99, 8), ("abc", 2), (2, 2)):
		store.set("bot.button_columns", stored)
		assert dcb.button_columns() == expected, stored
	store.set("bot.button_columns", 2)


def test_the_notification_channel_is_optional():
	store.set("bot.notification_channel", "")
	assert dcb.notification_channel() is None
	store.set("bot.notification_channel", "  -100999 ")
	assert dcb.notification_channel() == "-100999"
	store.set("bot.notification_channel", "")


def test_the_wait_between_checks_is_slept_in_steps():
	"""
	4.x slept the whole interval in one call, so shortening it only took effect
	after the current wait finished, hours later.
	"""
	original = dcb.UPDATE_CHECK_POLL_SECONDS
	dcb.UPDATE_CHECK_POLL_SECONDS = 0.02
	try:
		store.set("bot.check_updates", True)
		store.set("bot.check_update_every_hours", 0.00001)
		started = time.time()
		dcb.wait_for_next_update_check()
		assert time.time() - started < 1

		# Switching checks off ends a wait already in progress.
		store.set("bot.check_update_every_hours", 10)
		done = threading.Event()
		threading.Thread(
			target=lambda: (dcb.wait_for_next_update_check(), done.set()), daemon=True).start()
		time.sleep(0.1)
		assert not done.is_set()
		store.set("bot.check_updates", False)
		assert done.wait(2), "apagar las comprobaciones debe cortar la espera"
	finally:
		dcb.UPDATE_CHECK_POLL_SECONDS = original
		store.set("bot.check_updates", True)
		store.set("bot.check_update_every_hours", 4)


# ---------------------------------------------------------------------------
# Update status
# ---------------------------------------------------------------------------

def test_update_status_is_a_boolean():
	assert dcb.read_container_update_status("nginx:1.27", "nginx") is None
	dcb.save_container_update_status("nginx:1.27", "nginx", True)
	assert dcb.read_container_update_status("nginx:1.27", "nginx") is True
	assert dcb.read_container_update_status("nginx:1.28", "nginx") is None
	dcb.save_container_update_status("nginx:1.27", "nginx", None)
	assert dcb.read_container_update_status("nginx:1.27", "nginx") is None


def test_changing_the_language_no_longer_empties_the_cache():
	"""
	4.x stored the translated status message and detected a pending update by
	looking for the current language's wording inside it, so switching language
	silently lost every pending update.
	"""
	dcb.save_container_update_status("redis:7", "redis", True)
	store.set("bot.language", "DE")
	assert dcb.read_container_update_status("redis:7", "redis") is True
	assert dcb.update_status_text(True) == i18n.load_locale("de")["NEED_UPDATE_CONTAINER_TEXT"]
	store.set("bot.language", "ES")
	assert dcb.update_status_text(True) == i18n.load_locale("es")["NEED_UPDATE_CONTAINER_TEXT"]
	assert dcb.update_status_text(False) == i18n.load_locale("es")["UPDATED_CONTAINER_TEXT"]
	assert dcb.update_status_text(None) == ""


def test_mute_round_trips_through_the_state_file():
	assert dcb.is_muted() is False
	store.state_set("mute_until", time.time() + 600)
	assert dcb.is_muted() is True
	store.state_set("mute_until", 0)
	assert dcb.is_muted() is False


# ---------------------------------------------------------------------------
# The settings menu
# ---------------------------------------------------------------------------

def test_the_settings_body_repeats_nothing_from_its_buttons():
	"""
	Every value lives on its own button. Repeating each setting in a block of
	text above them meant reading the same thing twice and left the buttons
	unable to say what they were set to.
	"""
	store.set("bot.notification_channel", "")
	text, markup = dcb.build_settings()
	labels = harness.keyboard_labels(markup)

	assert store.settings_path() not in text
	assert any("Idioma" in label and "Español" in label for label in labels), labels
	assert any("sin configurar" in label for label in labels), labels

	for label in labels:
		name = label.split(" - ", 1)[-1].split(" · ", 1)[0].strip()
		if name and name != "Cerrar":
			assert name not in text, f"'{name}' está en el texto y en un botón a la vez"

	# One row per setting: a label carrying its value cannot share a line
	# without Telegram truncating it.
	assert all(len(row) == 1 for row in markup.keyboard)


def test_the_settings_body_is_only_the_title_unless_something_is_wrong():
	original = store.is_persistent
	try:
		store.is_persistent = lambda: True
		text, _ = dcb.build_settings()
		assert len(text.strip().split("\n")) == 1, repr(text)
		store.is_persistent = lambda: False
		text, _ = dcb.build_settings()
		assert "⚠️" in text, "el aviso de persistencia sí debe aparecer"
	finally:
		store.is_persistent = original


def test_the_update_settings_live_on_their_own_screen():
	"""
	"Include stopped containers" cannot explain itself in a label: it needs both
	"in the update check" and "which containers". A screen of its own says it
	once, at the top.
	"""
	callbacks = harness.keyboard_callbacks(dcb.build_settings()[1])
	assert "settingsUpdates" in callbacks
	for hidden in ("settingsToggle|check_updates", "settingsAskInterval",
					"settingsToggle|check_update_stopped_containers"):
		assert hidden not in callbacks, hidden

	text, markup = dcb.build_settings_updates()
	assert len(text.strip().split("\n")) > 1, "la pantalla necesita su frase explicativa"
	assert harness.keyboard_callbacks(markup) == [
		"settingsToggle|check_updates",
		"settingsAskInterval",
		"settingsToggle|check_update_stopped_containers",
		"settings",
	]


def test_the_updates_row_summarises_its_state():
	store.set("bot.check_updates", True)
	store.set("bot.check_update_every_hours", 6)
	row = [button.text for row in dcb.build_settings()[1].keyboard for button in row
			if button.callback_data == "settingsUpdates"][0]
	assert "6" in row, row

	store.set("bot.check_updates", False)
	row = [button.text for row in dcb.build_settings()[1].keyboard for button in row
			if button.callback_data == "settingsUpdates"][0]
	assert "desactivadas" in row, row

	store.set("bot.check_updates", True)
	store.set("bot.check_update_every_hours", 4)


def test_every_toggle_knows_which_screen_it_is_on():
	"""Otherwise pressing one bounces the user out of where they were working."""
	assert set(dcb.SETTINGS_TOGGLES) == set(dcb.SETTINGS_TOGGLE_SCREEN)
	assert dcb.SETTINGS_TOGGLE_SCREEN["check_update_stopped_containers"] == "updates"
	assert dcb.SETTINGS_TOGGLE_SCREEN["extended_messages"] == "main"


def test_the_clear_channel_button_only_appears_with_a_channel():
	store.set("bot.notification_channel", "")
	assert "settingsClearChannel" not in harness.keyboard_callbacks(dcb.build_settings()[1])
	store.set("bot.notification_channel", "-100123")
	assert "settingsClearChannel" in harness.keyboard_callbacks(dcb.build_settings()[1])
	store.set("bot.notification_channel", "")


def test_the_interval_is_validated():
	assert dcb.apply_settings_text_value("check_update_every_hours", "0.5") is not None
	assert store.get("bot.check_update_every_hours") == 0.5
	assert dcb.apply_settings_text_value("check_update_every_hours", "2,5") is not None
	assert store.get("bot.check_update_every_hours") == 2.5, "debe aceptar coma decimal"

	original = dcb.send_message
	dcb.send_message = lambda *args, **kwargs: None
	try:
		for bad in ("0", "-3", "hola", ""):
			assert dcb.apply_settings_text_value("check_update_every_hours", bad) is None, bad
		assert store.get("bot.check_update_every_hours") == 2.5, "un valor inválido no se guarda"
	finally:
		dcb.send_message = original
	store.set("bot.check_update_every_hours", 4)


def test_the_channel_is_verified_before_being_saved():
	"""
	Saving an id the bot cannot post to would send every notification into the
	void, with nothing in the interface to explain why.
	"""
	store.set("bot.notification_channel", "")
	original_send, original_chat = dcb.send_message, dcb.bot.get_chat
	dcb.send_message = lambda *args, **kwargs: None
	try:
		dcb.bot.get_chat = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("chat not found"))
		assert dcb.apply_settings_text_value("notification_channel", "-100999") is None
		assert store.get("bot.notification_channel") == ""

		dcb.bot.get_chat = lambda *args, **kwargs: MagicMock()
		assert dcb.apply_settings_text_value("notification_channel", "-100999") is not None
		assert store.get("bot.notification_channel") == "-100999"
	finally:
		dcb.send_message, dcb.bot.get_chat = original_send, original_chat
	store.set("bot.notification_channel", "")


def test_an_unknown_setting_writes_nothing():
	assert dcb.apply_settings_text_value("does_not_exist", "x") is None


# ---------------------------------------------------------------------------
# The language picker
# ---------------------------------------------------------------------------

def test_the_language_picker_marks_the_current_one():
	store.set("bot.language", "ES")
	labels = harness.keyboard_labels(dcb.build_language_keyboard())
	assert "✅ Español" in labels
	assert "English" in labels and "✅ English" not in labels
	assert "settings" in harness.keyboard_callbacks(dcb.build_language_keyboard())


def test_the_first_run_picker_marks_nothing():
	"""
	There is no previous choice to go back to, and the default is a fallback the
	user never picked, so marking it would be a lie.
	"""
	keyboard = dcb.build_language_keyboard(with_back=False, mark_current=False)
	labels = harness.keyboard_labels(keyboard)
	assert "Español" in labels and "✅ Español" not in labels
	assert len(labels) == len(dcb.SUPPORTED_LANGUAGES)
	assert "settings" not in harness.keyboard_callbacks(keyboard)


def test_every_language_has_a_name_of_its_own():
	assert set(dcb.SUPPORTED_LANGUAGES) == set(dcb.LANGUAGE_NAMES)


def test_answering_the_mute_prompt_goes_through_the_registry():
	"""
	The core used to call cmd_mute directly. Once the commands moved out that
	was a NameError waiting for someone to answer the prompt, and no test
	reached it: the static check did.
	"""
	assert "/mute" in dcb.COMMAND_ACTIONS
	source = io.open(os.path.join(harness.REPO, "core.py"), encoding="utf-8").read()
	assert "cmd_mute(" not in source, "el núcleo no debe llamar a un comando por su nombre"


def test_mute_asks_for_its_argument_when_pressed_as_a_button():
	asked = {}
	# Patched on `commands`, not on `core`: the `from core import` binding is
	# module-local, so replacing it on the core would not be seen here.
	original = commands.ask_text_input
	commands.ask_text_input = lambda user, field, prompt, back_to="main": asked.update(
		field=field, back=back_to)
	try:
		commands.cmd_mute(user_id=1)
	finally:
		commands.ask_text_input = original
	assert asked == {"field": "mute_minutes", "back": None}, asked


# ---------------------------------------------------------------------------
# The main menu
# ---------------------------------------------------------------------------

def _registered_commands():
	"""The command names the Telegram handler is subscribed to."""
	path = os.path.join(harness.REPO, "core.py")
	line = [l for l in io.open(path, encoding="utf-8")
			if l.startswith("@bot.message_handler(commands=")][0]
	return set(re.findall(r'"([a-z]+)"', line))


def test_no_command_was_lost():
	"""Both directions, so an orphan cannot slip in either way."""
	registered = _registered_commands()
	for name in registered - {"start"}:
		assert f"/{name}" in dcb.COMMAND_ACTIONS, f"/{name} se acepta pero no tiene acción"
	for command in dcb.COMMAND_ACTIONS:
		assert command.lstrip("/") in registered, f"{command} tiene acción pero no se acepta"


def test_every_command_is_reachable_with_buttons():
	in_menu = set()
	for kind, key in dcb.START_ROOT:
		if kind == "command":
			in_menu.add(key)
		else:
			assert key in dcb.START_CATEGORY_COMMANDS, f"categoría {key} sin comandos"
			in_menu.update(dcb.START_CATEGORY_COMMANDS[key])

	actions = {command.lstrip("/") for command in dcb.COMMAND_ACTIONS}
	assert not actions - in_menu, f"comandos sin botón: {sorted(actions - in_menu)}"
	assert not in_menu - actions, f"botones sin comando: {sorted(in_menu - actions)}"


def test_categories_are_shown_once_and_none_is_orphaned():
	categories = [key for kind, key in dcb.START_ROOT if kind == "category"]
	assert len(categories) == len(set(categories))
	assert set(categories) == set(dcb.START_CATEGORY_COMMANDS)


def test_every_menu_label_exists():
	in_menu = set()
	for kind, key in dcb.START_ROOT:
		in_menu.update([key] if kind == "command" else dcb.START_CATEGORY_COMMANDS[key])

	for locale in ("es", "en"):
		keys = i18n.load_locale(locale)
		for key in ("start_title", "start_summary", "mute_ask_minutes"):
			assert key in keys, f"{key} falta en {locale}"
		for category in dcb.START_CATEGORY_COMMANDS:
			assert f"start_cat_{category}" in keys, f"start_cat_{category} falta en {locale}"
		for name in in_menu:
			assert f"start_cmd_{name}" in keys, f"start_cmd_{name} falta en {locale}"


def test_the_menu_survives_docker_being_unreachable():
	original = dcb.docker_manager.list_containers
	dcb.docker_manager.list_containers = lambda *a, **kw: (_ for _ in ()).throw(Exception("down"))
	try:
		assert dcb._start_summary() is None
		text, _ = dcb.build_start_menu()
		assert "Docker Controller Bot" in text
	finally:
		dcb.docker_manager.list_containers = original


def test_every_submenu_has_a_way_back():
	for category, commands in dcb.START_CATEGORY_COMMANDS.items():
		built = dcb.build_start_category(category)
		assert built, category
		callbacks = harness.keyboard_callbacks(built[1])
		assert "startMenu" in callbacks, f"{category} no tiene Volver"
		assert len(callbacks) == len(commands) + 1, (category, callbacks)
	assert dcb.build_start_category("does-not-exist") is None


# ---------------------------------------------------------------------------
# Container references and the multi-host listing
# ---------------------------------------------------------------------------

def test_a_reference_carries_its_host():
	"""
	Five hex characters are only unique within one daemon, so what travels in
	a button has to say which machine it means.
	"""
	assert dcb.make_ref("h_5f55", "9a3b1c2d3e4f") == "h_5f55:9a3b1"
	assert dcb.parse_ref("h_5f55:9a3b1") == ("h_5f55", "9a3b1")
	assert dcb.ref_host("h_5f55:9a3b1") == "h_5f55"
	assert dcb.ref_id("h_5f55:9a3b1") == "9a3b1"


def test_a_bare_id_still_means_the_local_host():
	"""
	That is what a button sent before the upgrade carries. Pressing one has to
	keep working rather than failing with something inscrutable.
	"""
	local = dcb.host_registry.local_host_id()
	assert dcb.parse_ref("9a3b1") == (local, "9a3b1")
	assert dcb.ref_host("9a3b1") == local
	assert dcb.parse_ref("") == (local, "")
	assert dcb.parse_ref(None) == (local, "")


def test_a_reference_to_an_unreachable_host_finds_nothing():
	"""Rather than raising, so a stale button gives the usual "not found"."""
	_with_hosts(HOST_FIXTURE)
	try:
		owner, container = dcb.find_container("h_nas:abc12")
		assert (owner, container) == (None, None)
	finally:
		_restore_hosts()


def test_the_single_host_listing_is_unchanged():
	"""
	The golden rule of the release: with one host the bot has to read exactly
	as it did before hosts existed, byte for byte.
	"""
	registry = _with_hosts([HOST_FIXTURE[0]], unreachable=())
	fake = [_container("nginx", "running"), _container("redis", "exited")]
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake
	try:
		assert registry.is_single_host()
		grouped = dcb.display_all_hosts(comando="/list")
		plain = dcb.display_containers(fake, "h_local")
		assert grouped == plain, "con un host no debe añadirse ninguna cabecera"
		assert "🖥️" not in grouped
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_several_hosts_get_a_section_each():
	_with_hosts(HOST_FIXTURE, unreachable=())
	fake = {
		"h_local": [_container("nginx", "running")],
		"h_nas": [_container("plex", "running"), _container("tautulli", "exited")],
	}
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake[self.host_id]
	try:
		out = dcb.display_all_hosts(comando="/list")
		assert "casa" in out and "nas" in out
		assert out.index("casa") < out.index("nas"), "el orden debe ser el de los ajustes"
		assert "nginx" in out and "plex" in out and "tautulli" in out
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_a_host_that_is_down_is_reported_not_hidden():
	"""
	Quietly showing fewer containers than the user has is worse than saying a
	machine is unreachable: they would think something had been deleted.

	The real listing path is used here, so the exclusion comes from the client
	failing rather than from a mock deciding it should.
	"""
	_with_hosts(HOST_FIXTURE)  # nas falla en todas sus llamadas
	try:
		sections = dcb.hosts_with_containers()
		assert [entry["id"] for entry, _, _ in sections] == ["h_local"]
		assert [entry["id"] for entry in dcb.unreachable_hosts(sections)] == ["h_nas"]

		out = dcb.display_all_hosts(comando="/list")
		assert "nas" in out
		assert "🔴" in out, out
	finally:
		_restore_hosts()


def test_a_host_that_fails_mid_listing_does_not_break_the_rest():
	"""A daemon can answer a ping and then fail the very next call."""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers

	def flaky(self, comando=""):
		if self.host_id == "h_nas":
			raise Exception("connection reset by peer")
		return [_container("nginx", "running")]

	dcb.DockerManager.list_containers = flaky
	try:
		sections = dcb.hosts_with_containers()
		assert [entry["id"] for entry, _, _ in sections] == ["h_local"]
		out = dcb.display_all_hosts(comando="/list")
		assert "nginx" in out
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_update_state_is_read_from_the_right_host():
	"""
	Two hosts can run a container of the same name. Reading the cache without
	saying which host would show one machine's pending update on the other.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	try:
		store.set("bot.check_updates", True)
		store.set_update_status("h_nas", "nginx", "nginx:1.27", True)
		nginx = _container("nginx", "running", image="nginx:1.27")
		assert dcb.update_available(nginx, "h_nas") is True
		assert dcb.update_available(nginx, "h_local") is False
	finally:
		store.forget_update_status("h_nas", "nginx")
		_restore_hosts()


# ---------------------------------------------------------------------------
# Managing hosts
# ---------------------------------------------------------------------------

def _container(name, status, image=None):
	"""A stand-in for a docker-py container, with what the renderer reads."""
	fake = MagicMock()
	fake.name = name
	fake.status = status
	fake.id = f"{abs(hash(name)) % 0xfffffffff:09x}"
	fake.labels = {}
	fake.attrs = {"Config": {"Image": image or f"{name}:latest"}}
	return fake


HOST_FIXTURE = [
	{"id": "h_local", "alias": "casa", "url": "unix:///var/run/docker.sock", "local": True},
	{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
]


def _with_hosts(hosts, unreachable=("nas",)):
	"""
	Points the registry at `hosts`, with the named ones failing to answer.

	Deep-copied: store.set keeps the object it is given, so a test that renames
	a host would otherwise mutate the shared fixture and break whichever test
	runs next.
	"""
	import copy
	import docker
	import host_registry

	hosts = copy.deepcopy(hosts)

	def sdk(base_url=None, **kwargs):
		fake = MagicMock()
		if any(name in (base_url or "") for name in unreachable):
			# Everything fails, not just the ping: nothing pings before using a
			# client, so a host is excluded by the call itself failing.
			down = Exception("no route to host")
			fake.ping.side_effect = down
			fake.info.side_effect = down
			fake.containers.get.side_effect = down
			fake.containers.list.side_effect = down
		return fake

	docker.DockerClient = sdk
	store.set("hosts", hosts)
	host_registry.reset()
	return host_registry


def _restore_hosts():
	import docker
	import host_registry
	docker.DockerClient = lambda *a, **kw: MagicMock()
	store.set("hosts", [HOST_FIXTURE[0]])
	host_registry.reset()


def test_reading_what_someone_typed_when_adding_a_host():
	"""
	One prompt instead of two: asking for a name and then a URL is twice the
	taps for something people paste in one go.
	"""
	cases = {
		"nas ssh://dgongut@nas": ("nas", "ssh://dgongut@nas"),
		"ssh://dgongut@nas": ("nas", "ssh://dgongut@nas"),
		"tcp://192.168.1.50:2375": ("192.168.1.50", "tcp://192.168.1.50:2375"),
		"mi nas de casa ssh://root@10.0.0.5": ("mi nas de casa", "ssh://root@10.0.0.5"),
		"unix:///var/run/docker.sock": ("local", "unix:///var/run/docker.sock"),
	}
	for raw, expected in cases.items():
		assert dcb.parse_host_definition(raw) == expected, raw

	for junk in ("hola", "", "   ", "nas"):
		assert dcb.parse_host_definition(junk) == (None, None), junk


def test_the_host_list_shows_who_answers():
	_with_hosts(HOST_FIXTURE)
	try:
		_, markup = dcb.build_settings_hosts()
		labels = harness.keyboard_labels(markup)
		assert any(l.startswith("🟢") and "casa" in l for l in labels), labels
		assert any(l.startswith("🔴") and "nas" in l for l in labels), labels
		assert any("Añadir" in l for l in labels), labels
	finally:
		_restore_hosts()


def test_a_dead_host_shows_why():
	"""
	The reason is the whole point of the screen: "does not answer" without it
	leaves nothing to act on.
	"""
	_with_hosts(HOST_FIXTURE)
	try:
		text, _ = dcb.build_settings_host("h_nas")
		assert "no route to host" in text, text
	finally:
		_restore_hosts()


def test_the_local_host_has_no_remove_button():
	"""The bot runs on it, so offering to remove it would be a trap."""
	_with_hosts(HOST_FIXTURE)
	try:
		_, markup = dcb.build_settings_host("h_local")
		assert not any("settingsHostRemove" in c for c in harness.keyboard_callbacks(markup))
		text, _ = dcb.build_settings_host("h_local")
		assert "bot" in text.lower()

		_, markup = dcb.build_settings_host("h_nas")
		assert "settingsHostRemove|h_nas" in harness.keyboard_callbacks(markup)
	finally:
		_restore_hosts()


def test_removing_a_host_is_confirmed_first():
	_with_hosts(HOST_FIXTURE)
	try:
		text, markup = dcb.build_settings_host_remove("h_nas")
		assert "nas" in text
		callbacks = harness.keyboard_callbacks(markup)
		assert "settingsHostRemoveConfirm|h_nas" in callbacks
		# Cancelling goes back to the host, not out of the menu.
		assert "settingsHost|h_nas" in callbacks
	finally:
		_restore_hosts()


def test_an_unknown_host_screen_does_not_crash():
	_with_hosts(HOST_FIXTURE)
	try:
		assert dcb.build_settings_host("h_nope") is None
		assert dcb.build_settings_host_remove("h_nope") is None
	finally:
		_restore_hosts()


def test_an_unreachable_host_is_not_saved():
	"""
	Rejected while the connection details are still in front of the person
	typing them, with the reason shown.
	"""
	registry = _with_hosts([HOST_FIXTURE[0]])
	sent = []
	original = dcb.send_message
	dcb.send_message = lambda *a, **kw: sent.append(kw.get("message", "")) 
	try:
		assert dcb.apply_settings_text_value("host_add", "nas tcp://nas:2375") is None
		assert len(registry.hosts()) == 1, "no debe guardarse"
		assert any("nas" in message for message in sent), sent
	finally:
		dcb.send_message = original
		_restore_hosts()


def test_a_reachable_host_is_saved():
	registry = _with_hosts([HOST_FIXTURE[0]], unreachable=())
	try:
		assert dcb.apply_settings_text_value("host_add", "nas tcp://nas:2375") is not None
		assert [h["alias"] for h in registry.hosts()] == ["casa", "nas"]
		assert registry.hosts()[1]["local"] is False
	finally:
		_restore_hosts()


def test_junk_is_rejected_without_saving():
	registry = _with_hosts([HOST_FIXTURE[0]], unreachable=())
	original = dcb.send_message
	dcb.send_message = lambda *a, **kw: None
	try:
		assert dcb.apply_settings_text_value("host_add", "esto no es una url") is None
		assert len(registry.hosts()) == 1
	finally:
		dcb.send_message = original
		_restore_hosts()


def test_renaming_a_host_from_the_menu():
	registry = _with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.send_message
	dcb.send_message = lambda *a, **kw: None
	try:
		assert dcb.apply_settings_text_value("host_rename:h_nas", "sinologia") is not None
		assert registry.alias("h_nas") == "sinologia"
		# An empty name, or one for a host that is gone, changes nothing.
		assert dcb.apply_settings_text_value("host_rename:h_nas", "   ") is None
		assert dcb.apply_settings_text_value("host_rename:h_nope", "x") is None
		assert registry.alias("h_nas") == "sinologia"
	finally:
		dcb.send_message = original
		_restore_hosts()


def test_checking_hosts_does_not_wait_for_them_one_by_one():
	"""
	A menu that pinged hosts in sequence would hang for the sum of their
	timeouts, so opening it with a machine unplugged would look frozen.
	"""
	import host_registry
	slow = [
		{"id": f"h_{index}", "alias": f"host{index}", "url": f"tcp://slow{index}:2375"}
		for index in range(6)
	]
	import docker

	def sdk(base_url=None, **kwargs):
		fake = MagicMock()
		fake.ping.side_effect = lambda: time.sleep(2)
		return fake

	docker.DockerClient = sdk
	store.set("hosts", slow)
	host_registry.reset()
	try:
		started = time.time()
		statuses = host_registry.status_snapshot(deadline_seconds=1)
		elapsed = time.time() - started
		assert elapsed < 2.5, f"tardó {elapsed:.1f}s con 6 hosts lentos"
		assert len(statuses) == 6
		assert all(not ok for ok, _ in statuses.values())
	finally:
		_restore_hosts()


# ---------------------------------------------------------------------------
# The callback registry
# ---------------------------------------------------------------------------

def _all_keyboards():
	store.set("bot.notification_channel", "-1")
	keyboards = [
		dcb.build_settings()[1],
		dcb.build_settings_updates()[1],
		dcb.build_start_menu()[1],
		dcb.build_language_keyboard(),
		dcb.build_language_keyboard(with_back=False, mark_current=False),
		harness.capture_edit(dcb, dcb.show_settings_language, 1, 2)[1],
		harness.capture_edit(dcb, dcb.show_settings_columns, 1, 2)[1],
	]
	keyboards += [dcb.build_start_category(c)[1] for c in dcb.START_CATEGORY_COMMANDS]

	# The host screens, with one host down so its remove path shows up too.
	import host_registry
	previous = store.get("hosts")
	store.set("hosts", HOST_FIXTURE)
	host_registry.reset()
	keyboards.append(dcb.build_settings_hosts()[1])
	keyboards.append(dcb.build_settings_host("h_local")[1])
	keyboards.append(dcb.build_settings_host("h_nas")[1])
	keyboards.append(dcb.build_settings_host_remove("h_nas")[1])
	store.set("hosts", previous)
	host_registry.reset()

	store.set("bot.notification_channel", "")
	return keyboards


def test_every_button_parses_and_fits():
	"""
	Telegram caps callback_data at 64 bytes, and a button whose callback is not
	registered does nothing at all when pressed.
	"""
	for markup in _all_keyboards():
		for data in harness.keyboard_callbacks(markup):
			assert len(data.encode()) <= 64, data
			callback_registry.parse(data)


def test_anything_that_repaints_in_place_keeps_its_message():
	"""
	The dispatcher deletes the message a press came from unless the callback
	says otherwise. Forgetting that flag is exactly how the settings menu shipped
	broken once: the handler ran, then edited a message that was already gone.
	"""
	# The ones that deliberately hand the message over to something else: a
	# text prompt, or whatever a command opens.
	replaces_message = {"settingsAskInterval", "settingsAskChannel", "cancelTextInput",
						"settingsHostAdd", "settingsHostRename",
						"cerrar", "startCommand"}
	emitted = set()
	for markup in _all_keyboards():
		for data in harness.keyboard_callbacks(markup):
			emitted.add(callback_registry.parse(data)[0].name)

	orphans = {name for name in emitted - replaces_message
				if not callback_registry.get(name).keeps_message}
	assert not orphans, f"estos botones se borrarían el mensaje antes de repintarlo: {sorted(orphans)}"


def test_the_registry_matches_4_2_0_exactly():
	"""
	The metadata used to live in four dictionaries in config.py. This compares
	the registry against a frozen snapshot of them, so a refactor cannot quietly
	change how a button behaves.
	"""
	with io.open(os.path.join(harness.DATA, "callbacks_4.2.0.json"), encoding="utf-8") as handle:
		baseline = json.load(handle)

	registry = callback_registry.specs()
	for name in baseline["_dead_in_4_2_0"]:
		assert name not in registry, f"{name} era una declaración muerta"

	missing = set(baseline["callbacks"]) - set(registry)
	assert not missing, f"callbacks de 4.2.0 perdidos: {sorted(missing)}"

	for name, expected in sorted(baseline["callbacks"].items()):
		spec = registry[name]
		assert list(spec.params) == expected["params"], f"{name}: params"
		assert spec.keeps_message == expected["keeps_message"], f"{name}: keeps_message"
		assert spec.project_arg == expected["project_arg"], f"{name}: project_arg"
		assert spec.multi_action == expected["multi_action"], f"{name}: multi_action"


def test_the_callbacks_added_after_4_2_0():
	expected = {
		"settings": (True, ()), "settingsToggle": (True, ("field",)),
		"settingsLanguage": (True, ()), "settingsSetLanguage": (True, ("value",)),
		"settingsColumns": (True, ()), "settingsSetColumns": (True, ("value",)),
		"settingsClearChannel": (True, ()), "settingsUpdates": (True, ()),
		"settingsAskInterval": (False, ()), "settingsAskChannel": (False, ()),
		"cancelTextInput": (False, ()),
		"startMenu": (True, ()), "startCategory": (True, ("value",)),
		"startCommand": (False, ("value",)),
	}
	for name, (keeps, params) in expected.items():
		spec = callback_registry.get(name)
		assert spec is not None, f"{name} no está registrado"
		assert spec.keeps_message == keeps, f"{name}: keeps_message"
		assert tuple(spec.params) == params, f"{name}: params"


def test_project_navigation_is_generated():
	"""
	22 callbacks with two distinct bodies between them, each needing entries in
	several registries. Adding an action used to mean ten edits in two files.
	"""
	generated = {f"enter{a}Project" for a in callbacks.PROJECT_NAVIGATION_ACTIONS}
	generated |= {f"backTo{a}Level1" for a in callbacks.PROJECT_NAVIGATION_ACTIONS}
	generated.add("backToComposeLevel1")
	assert len(generated) == 21

	for name in generated:
		spec = callback_registry.get(name)
		assert spec is not None, f"la fábrica no registró {name}"
		assert spec.keeps_message, f"{name} debe repintar en sitio"

	# Compose stays hand-written: the generic keyboard builder redirects a
	# stopped container's button to `run`, which for /compose would start the
	# container instead of showing its compose file.
	assert callback_registry.get("enterComposeProject").project_arg


def test_every_registered_callback_has_a_handler():
	for name, spec in callback_registry.specs().items():
		assert callable(spec.handler), name


def test_parse_rejects_the_unknown_and_the_malformed():
	for data, expected in (("doesNotExist", "NOT IN PATTERN"),
							("settingsToggle", "INCORRECT LENGTH")):
		try:
			callback_registry.parse(data)
			raise AssertionError(f"{data} debería lanzar")
		except ValueError as error:
			assert expected in str(error), error

	spec, args = callback_registry.parse("settingsToggle|check_updates")
	assert spec.name == "settingsToggle"
	assert args == {"field": "check_updates"}
