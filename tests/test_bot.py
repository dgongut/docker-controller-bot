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
		if name and name not in ("Cerrar", "Volver"):
			assert name not in text, f"'{name}' está en el texto y en un botón a la vez"

	# One row per setting: a label carrying its value cannot share a line
	# without Telegram truncating it. Only the navigation row pairs up, and it
	# carries no value to truncate.
	assert all(len(row) == 1 for row in markup.keyboard[:-1])
	assert len(markup.keyboard[-1]) == 2


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
		"cerrar",
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


def _locale_names():
	return sorted(
		name[:-len(".json")]
		for name in os.listdir(os.path.join(os.path.dirname(dcb.__file__), "locale"))
		if name.endswith(".json")
	)


def test_every_locale_has_every_key():
	"""
	A key missing from a locale falls back to English mid-message, so half the
	interface changes language without warning. Nothing catches that at runtime.
	"""
	base = i18n.load_locale("en")
	for locale in _locale_names():
		keys = i18n.load_locale(locale)
		missing = sorted(set(base) - set(keys))
		assert not missing, f"{locale}: faltan {len(missing)} claves: {missing[:10]}"


def test_no_locale_carries_keys_nobody_reads():
	"""Left over from a text that was removed: dead weight nobody notices."""
	base = i18n.load_locale("en")
	for locale in _locale_names():
		extra = sorted(set(i18n.load_locale(locale)) - set(base))
		assert not extra, f"{locale}: sobran {extra}"


def test_a_translation_keeps_the_placeholders_of_the_original():
	"""
	get_text substitutes $1, $2... positionally: a translation that drops one
	silently loses the datum, and one that invents another prints it raw.
	"""
	base = i18n.load_locale("en")
	placeholders = lambda text: sorted(set(re.findall(r"\$[0-9]", text)))
	for locale in _locale_names():
		keys = i18n.load_locale(locale)
		for key, original in base.items():
			assert placeholders(keys[key]) == placeholders(original), (locale, key)


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
		assert "cerrar" in callbacks, f"{category} no tiene Cerrar"
		assert len(callbacks) == len(commands) + 2, (category, callbacks)
	assert dcb.build_start_category("does-not-exist") is None


def test_every_settings_screen_offers_back_and_close():
	"""
	Half a navigation row is worse than none: a screen with only Volver leaves
	the menu stuck open, and one with only Cerrar turns going up a level into
	starting over from /start.
	"""
	screens = {
		"settings": dcb.build_settings()[1],
		"updates": dcb.build_settings_updates()[1],
		"language": dcb.build_language_keyboard(),
		"columns": harness.capture_edit(dcb, dcb.show_settings_columns, 1, 2)[1],
	}
	for name, markup in screens.items():
		row = [button.callback_data for button in markup.keyboard[-1]]
		assert len(row) == 2, (name, row)
		assert row[1] == "cerrar", (name, row)
		assert row[0] != "cerrar", (name, row)


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
# The container pickers
# ---------------------------------------------------------------------------

def _capture_picker(action_type):
	"""Runs a picker with sending stubbed, returning (text, markup)."""
	captured = {}

	class Sent:
		chat = type("Chat", (), {"id": 1})()
		message_id = 2

	original = (dcb.send_message, dcb.save_container_cache, dcb.save_multi_action)
	dcb.send_message = lambda **kwargs: (captured.update(kwargs), Sent())[1]
	dcb.save_container_cache = lambda *a, **k: None
	dcb.save_multi_action = lambda *a, **k: None
	try:
		dcb.send_picker(action_type)
	finally:
		dcb.send_message, dcb.save_container_cache, dcb.save_multi_action = original
	return captured.get("message", ""), captured.get("reply_markup")


def test_a_picker_offers_containers_from_every_host():
	_with_hosts(HOST_FIXTURE, unreachable=())
	fake = {
		"h_local": [_container("nginx", "running")],
		"h_nas": [_container("plex", "running")],
	}
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake[self.host_id]
	try:
		text, markup = _capture_picker("Stop")
		callbacks = harness.keyboard_callbacks(markup)
		assert "pickHost|Stop|h_local" in callbacks, callbacks
		assert "pickHost|Stop|h_nas" in callbacks, callbacks
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_no_host_level_when_only_one_host_can_offer_anything():
	"""
	A host button leading to a single container is a tap that disambiguates
	nothing. /run with one stopped container on one machine should go straight
	to it.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	fake = {
		"h_local": [_container("nginx", "running")],       # nada que arrancar
		"h_nas": [_container("plex", "exited")],           # el único candidato
	}
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake[self.host_id]
	try:
		text, markup = _capture_picker("Run")
		callbacks = harness.keyboard_callbacks(markup)
		assert not any(c.startswith("pickHost") for c in callbacks), callbacks
		assert any(c == "run|h_nas:" + dcb.ref_id(c) for c in callbacks if c.startswith("run|")), callbacks
		# Con varios hosts configurados, el prompt dice de cuál se trata.
		assert "nas" in text, text
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_a_single_host_picker_names_no_host():
	"""The golden rule again: one host means the menu reads as it always did."""
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("plex", "exited")]
	try:
		text, markup = _capture_picker("Run")
		assert "🖥️" not in text, text
		assert not any(c.startswith("pickHost") for c in harness.keyboard_callbacks(markup))
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_a_picker_with_nothing_anywhere_says_so():
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("nginx", "running")]
	try:
		text, markup = _capture_picker("Run")   # nada parado en ningún host
		assert markup is None, "no debe haber teclado si no hay nada que ofrecer"
		assert text, "y sí un mensaje explicándolo"
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def _capture_updateall():
	"""Runs /updateall and returns the message and keyboard it sent."""
	captured = {}
	original = (commands.send_message, commands.update_available,
				commands.save_update_data, commands.save_container_refs)
	commands.send_message = lambda message="", reply_markup=None, **kwargs: captured.update(
		message=message, reply_markup=reply_markup) or None
	# The update cache is not what this is about: every container has one
	# pending, so the list is the whole fleet.
	commands.update_available = lambda container, host_id=None: True
	commands.save_update_data = lambda *a, **k: None
	commands.save_container_refs = lambda *a, **k: None
	try:
		commands.cmd_updateall()
	finally:
		(commands.send_message, commands.update_available,
			commands.save_update_data, commands.save_container_refs) = original
	return captured.get("message", ""), captured.get("reply_markup")


def test_updateall_lists_every_host_and_its_buttons_carry_references():
	"""
	The manual command has to see what the automatic daemon sees. Listing only
	the local machine made the two disagree about what needs updating, and the
	bare ids its buttons carried resolved against the local host wherever the
	container actually lived.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	fake = {
		"h_local": [_container("nginx", "running")],
		"h_nas": [_container("plex", "running")],
	}
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake[self.host_id]
	try:
		_text, markup = _capture_updateall()
		callbacks = harness.keyboard_callbacks(markup)
		toggles = [c for c in callbacks if c.startswith("toggleUpdate|")]
		assert len(toggles) == 2, callbacks
		assert {dcb.ref_host(c.split("|", 1)[1]) for c in toggles} == {"h_local", "h_nas"}, toggles
		for callback in toggles:
			reference = callback.split("|", 1)[1]
			assert dcb.CONTAINER_REF_SEPARATOR in reference, reference
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_updateall_names_the_host_on_a_button_only_when_the_list_spans_hosts():
	"""
	Two machines can hold the same container name, so a fleet-wide list has to
	say which is which. On the first render as much as on the repaint after a
	tap: they go through the same builder for exactly that reason.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	fake = {
		"h_local": [_container("nginx", "running")],
		"h_nas": [_container("nginx", "running")],
	}
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": fake[self.host_id]
	try:
		_text, markup = _capture_updateall()
		labels = harness.keyboard_labels(markup)
		assert any("casa" in label and "nginx" in label for label in labels), labels
		assert any("nas" in label and "nginx" in label for label in labels), labels

		# And the repaint agrees: the toggle rebuilds from the same pairs.
		pairs = [[c.split("|", 1)[1], "nginx"]
					for c in harness.keyboard_callbacks(markup) if c.startswith("toggleUpdate|")]
		repainted = harness.keyboard_labels(
			dcb.build_generic_keyboard(pairs, set(), None, "Update", "update", "update all"))
		assert any("casa" in label for label in repainted), repainted
		assert any("nas" in label for label in repainted), repainted
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_a_single_host_updateall_names_no_host():
	"""The golden rule: with one host the list reads as it always did."""
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("nginx", "running")]
	try:
		_text, markup = _capture_updateall()
		labels = [label for label in harness.keyboard_labels(markup) if "nginx" in label]
		assert labels, harness.keyboard_labels(markup)
		for label in labels:
			assert "casa" not in label and "\u00b7" not in label, label
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_container_buttons_carry_their_host():
	"""
	The whole point: a button has to say which machine it means, or five hex
	characters could match a container on the wrong one.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("plex", "exited")] if self.host_id == "h_nas" else [])
	try:
		_, markup = _capture_picker("Run")
		for data in harness.keyboard_callbacks(markup):
			if data == "cerrar":
				continue
			assert data.startswith("run|h_nas:"), data
			assert len(data.encode()) <= 64
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_every_picker_action_has_its_texts():
	"""A missing key would show the raw key name to the user."""
	for action, spec in dcb.PICKER_ACTIONS.items():
		for locale in ("es", "en"):
			keys = i18n.load_locale(locale)
			assert spec["prompt_key"] in keys, (action, spec["prompt_key"], locale)
			assert spec["empty_key"] in keys, (action, spec["empty_key"], locale)


def test_repainting_a_host_picker_reports_an_unreachable_host():
	_with_hosts(HOST_FIXTURE)  # nas caído
	captured = {}
	original = dcb.edit_message_text
	dcb.edit_message_text = lambda text, chat, message, **kw: captured.update(text=text)
	try:
		dcb.render_picker_for_host(1, 2, "Stop", "h_nas")
		assert "nas" in captured.get("text", ""), captured
		assert "🔴" in captured.get("text", ""), captured
	finally:
		dcb.edit_message_text = original
		_restore_hosts()


def test_an_unknown_picker_action_is_ignored():
	captured = {}
	original = dcb.edit_message_text
	dcb.edit_message_text = lambda *a, **kw: captured.update(called=True)
	try:
		dcb.render_picker_for_host(1, 2, "NoExiste", "h_local")
		assert not captured, "no debe repintar nada"
	finally:
		dcb.edit_message_text = original


# ---------------------------------------------------------------------------
# The session cache
# ---------------------------------------------------------------------------

def test_the_session_cache_is_json_and_keeps_its_sets():
	"""
	JSON and not pickle: the cache directory lives in the volume the user
	mounts, and unpickling is running whatever is in the file. Sets do not
	survive JSON on their own, and two entries here hold one.
	"""
	dcb.write_cache_item("probe_1", {"containers": [["a", "nginx"]], "selected": {"nginx"}})
	path = dcb._cache_path("probe_1")
	assert path.endswith(".json")
	json.loads(open(path, encoding="utf-8").read())   # legible sin ejecutar nada

	value = dcb.read_cache_item("probe_1")
	assert value["selected"] == {"nginx"}
	assert value["containers"] == [["a", "nginx"]]
	dcb.delete_cache_item("probe_1")
	assert dcb.read_cache_item("probe_1") is None


def test_a_cache_entry_nobody_touches_expires():
	"""
	Interface state for menus nobody is looking at any more, sitting in the one
	directory that survives every update.
	"""
	dcb.write_cache_item("probe_old", {"action": "Stop"})
	stale = time.time() - dcb.CACHE_TTL_SECONDS - 60
	os.utime(dcb._cache_path("probe_old"), (stale, stale))
	assert dcb.read_cache_item("probe_old") is None
	assert not os.path.exists(dcb._cache_path("probe_old"))


def test_reading_an_entry_pushes_its_expiry_back():
	"""A menu still in use must not stop working underneath someone."""
	dcb.write_cache_item("probe_used", {"action": "Stop"})
	nearly = time.time() - dcb.CACHE_TTL_SECONDS + 120
	os.utime(dcb._cache_path("probe_used"), (nearly, nearly))
	assert dcb.read_cache_item("probe_used") == {"action": "Stop"}
	assert time.time() - os.path.getmtime(dcb._cache_path("probe_used")) < 5
	dcb.delete_cache_item("probe_used")


def test_the_sweep_clears_stale_entries_and_old_pickles():
	"""
	Without a sweep the directory only ever grows, one file per menu opened.
	"""
	dcb.write_cache_item("probe_fresh", {"action": "Stop"})
	dcb.write_cache_item("probe_stale", {"action": "Stop"})
	stale = time.time() - dcb.CACHE_TTL_SECONDS - 60
	os.utime(dcb._cache_path("probe_stale"), (stale, stale))
	leftover = os.path.join(dcb._cache_dir(), "update_data_1_2")
	with open(leftover, "wb") as handle:
		handle.write(b"pickled junk")

	assert dcb.sweep_cache() >= 2
	assert not os.path.exists(leftover)
	assert dcb.read_cache_item("probe_stale") is None
	assert dcb.read_cache_item("probe_fresh") == {"action": "Stop"}
	dcb.delete_cache_item("probe_fresh")


# ---------------------------------------------------------------------------
# The multi-selection session
# ---------------------------------------------------------------------------

def test_the_session_remembers_its_host():
	"""
	Without it the repaint after every press rebuilt from the local host, so
	stopping a remote container swapped the list for the local machine's.
	"""
	dcb.save_multi_action(1, 2, "Stop", host_id="h_nas")
	session = dcb.load_multi_action(1, 2)
	assert session["host"] == "h_nas"
	dcb.clear_multi_action(1, 2)


def test_a_session_from_before_hosts_means_the_local_one():
	"""A menu left open across the upgrade still has to repaint."""
	dcb.write_cache_item("multi_action_1_2", {
		"_timestamp": __import__("datetime").datetime.now().isoformat(),
		"action": "Stop", "level": 1, "project": None, "done": set(),
	})
	session = dcb.load_multi_action(1, 2)
	assert session["host"] == dcb.host_registry.local_host_id()
	dcb.clear_multi_action(1, 2)


def test_repainting_rebuilds_from_the_session_host():
	"""
	The bug as reported: stop a container on Ganimedes and the refreshed menu
	showed the local containers instead.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	asked = []
	fake = {
		"h_local": [_container("nginx", "running")],
		"h_nas": [_container("plex", "running"), _container("tautulli", "running")],
	}
	original = dcb.DockerManager.list_containers

	def listing(self, comando=""):
		asked.append(self.host_id)
		return fake[self.host_id]

	dcb.DockerManager.list_containers = listing
	captured = {}
	original_edit = dcb.edit_message_text
	dcb.edit_message_text = lambda text, chat, message, **kw: captured.update(
		text=text, markup=kw.get("reply_markup"))
	try:
		dcb.save_multi_action(1, 2, "Stop", host_id="h_nas")
		asked.clear()
		dcb.refresh_multi_action_menu(1, 2, ["plex"], succeeded=True)

		assert asked == ["h_nas"], f"reconstruyó desde {asked}, no desde el host de la sesión"
		labels = harness.keyboard_labels(captured["markup"])
		assert any("tautulli" in l for l in labels), labels
		assert not any("nginx" in l for l in labels), f"salieron los del host local: {labels}"
		# Y el que se acaba de parar queda marcado como hecho.
		assert any("plex" in l for l in labels), labels
	finally:
		dcb.DockerManager.list_containers = original
		dcb.edit_message_text = original_edit
		dcb.clear_multi_action(1, 2)
		_restore_hosts()


def test_the_containers_it_offers_keep_their_host():
	"""A repaint must not hand back buttons pointing at the wrong machine."""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("plex", "running")] if self.host_id == "h_nas" else [_container("nginx", "running")])
	captured = {}
	original_edit = dcb.edit_message_text
	dcb.edit_message_text = lambda text, chat, message, **kw: captured.update(markup=kw.get("reply_markup"))
	try:
		dcb.save_multi_action(1, 2, "Stop", host_id="h_nas")
		dcb.refresh_multi_action_menu(1, 2, None)
		for data in harness.keyboard_callbacks(captured["markup"]):
			if data == "cerrar":
				continue
			assert dcb.ref_host(data.split("|", 1)[1]) == "h_nas", data
	finally:
		dcb.DockerManager.list_containers = original
		dcb.edit_message_text = original_edit
		dcb.clear_multi_action(1, 2)
		_restore_hosts()


# ---------------------------------------------------------------------------
# Resolving a typed container name
# ---------------------------------------------------------------------------

def test_a_name_is_searched_on_every_host():
	"""
	What keeps a single-host bot feeling unchanged while a multi-host one needs
	no extra typing.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("plex", "running")] if self.host_id == "h_nas" else [_container("nginx", "running")])
	try:
		ref, name, candidates = dcb.resolve_container_argument("plex")
		assert candidates == []
		assert dcb.ref_host(ref) == "h_nas", ref
		assert name == "plex"

		ref, name, candidates = dcb.resolve_container_argument("nginx")
		assert dcb.ref_host(ref) == "h_local", ref
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_a_repeated_name_asks_instead_of_guessing():
	"""
	Guessing here would act on the wrong machine silently, which for /stop or
	/delete is not recoverable.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("plex", "running")]
	try:
		ref, name, candidates = dcb.resolve_container_argument("plex")
		assert ref is None, "no debe elegir por su cuenta"
		assert name == "plex"
		assert {entry["id"] for entry, _ in candidates} == {"h_local", "h_nas"}
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_host_prefix_shortcut():
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("plex", "running")]
	try:
		ref, name, candidates = dcb.resolve_container_argument("nas:plex")
		assert candidates == []
		assert dcb.ref_host(ref) == "h_nas", ref
		assert name == "plex"

		# An unknown prefix is part of the name, not a host.
		ref, name, candidates = dcb.resolve_container_argument("noexiste:plex")
		assert (ref, name, candidates) == (None, "noexiste:plex", [])
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_an_unknown_name_resolves_to_nothing():
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("nginx", "running")]
	try:
		assert dcb.resolve_container_argument("noexiste") == (None, "noexiste", [])
		assert dcb.resolve_container_argument("") == (None, None, [])
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_disambiguation_buttons_are_the_ordinary_ones():
	"""
	So choosing a host goes through exactly the same path as picking the
	container from a menu, with no second code path to keep in step.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("plex", "running")]
	captured = {}

	class Sent:
		chat = type("Chat", (), {"id": 1})()
		message_id = 2

	original_send = dcb.send_message
	dcb.send_message = lambda **kwargs: (captured.update(kwargs), Sent())[1]
	try:
		_, _, candidates = dcb.resolve_container_argument("plex")
		dcb.send_container_disambiguation("Stop", "plex", candidates)
		callbacks = harness.keyboard_callbacks(captured["reply_markup"])
		assert any(c.startswith("stop|h_local:") for c in callbacks), callbacks
		assert any(c.startswith("stop|h_nas:") for c in callbacks), callbacks
		for data in callbacks:
			if data != "cerrar":
				callback_registry.parse(data)
	finally:
		dcb.send_message = original_send
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_every_command_with_a_picker_can_disambiguate():
	"""A command that can take a name must know which button to offer."""
	for command, action in dcb.COMMAND_PICKERS.items():
		spec = dcb.PICKER_ACTIONS.get(action)
		assert spec is not None, command
		assert spec.get("container_callback"), (command, action)
		assert callback_registry.get(spec["container_callback"]) is not None, spec


def test_a_scheduled_task_resolves_on_its_own_host():
	"""
	A task names a container, and acting on a same-named container elsewhere
	would be silent and, for stop or exec, destructive.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	plex = _container("plex", "running")
	original = dcb.DockerManager.container_named
	dcb.DockerManager.container_named = lambda self, name: plex if name == "plex" else None
	try:
		ref = dcb.schedule_container_ref("h_nas", "plex")
		assert dcb.ref_host(ref) == "h_nas", ref
		assert dcb.schedule_container_ref("h_nas", "noexiste") is None
	finally:
		dcb.DockerManager.container_named = original
		_restore_hosts()


def test_a_host_screen_asks_only_for_the_host():
	"""
	It used to borrow the action's own prompt, so /prune asked for an object
	type and for a host in the same message, and /stop said "press a project
	or container" above a list of hosts.
	"""
	for action in list(dcb.PICKER_ACTIONS) + ["prune", "ports"]:
		text = dcb.host_question(action)
		assert dcb.get_text("pick_a_host") in text, (action, text)
		assert " - " not in text, f"{action}: el separador de botón se cuela en el título"
		assert len(text.strip().split("\n")) <= 3, (action, text)

	# And no instruction from the screen that comes after.
	stop = dcb.host_question("Stop")
	assert dcb.get_text("stop_a_container") not in stop, stop
	prune = dcb.host_question("prune")
	assert dcb.get_text("prune_system") not in prune, prune


def test_every_host_screen_has_its_title():
	"""A missing key would show the raw key name where the action should be."""
	for action in list(dcb.PICKER_ACTIONS) + ["prune", "ports"]:
		for locale in ("es", "en"):
			assert f"start_cmd_{action.lower()}" in i18n.load_locale(locale), (action, locale)


def test_prune_asks_which_host_when_there_are_several():
	"""It deletes things, so knowing where is worth a tap."""
	_with_hosts(HOST_FIXTURE, unreachable=())
	captured = {}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: captured.update(kwargs)
	try:
		dcb.send_prune_menu()
		callbacks = harness.keyboard_callbacks(captured["reply_markup"])
		assert "pruneHost|h_local" in callbacks, callbacks
		assert "pruneHost|h_nas" in callbacks, callbacks

		# With one host it goes straight to the object types, as it always did.
		captured.clear()
		_with_hosts([HOST_FIXTURE[0]], unreachable=())
		dcb.send_prune_menu()
		callbacks = harness.keyboard_callbacks(captured["reply_markup"])
		assert all(c.startswith("prune|") or c == "cerrar" for c in callbacks), callbacks
	finally:
		dcb.send_message = original
		_restore_hosts()


def test_the_prune_confirmation_names_the_host():
	_with_hosts(HOST_FIXTURE, unreachable=())
	captured = {}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: captured.update(kwargs)
	try:
		dcb.confirm_prune("Images", "h_nas")
		assert "nas" in captured["message"], captured["message"]
		assert "prune|pruneImages|h_nas" in harness.keyboard_callbacks(captured["reply_markup"])
	finally:
		dcb.send_message = original
		_restore_hosts()


# ---------------------------------------------------------------------------
# Whole-project actions and scheduling across hosts
# ---------------------------------------------------------------------------

def test_a_project_action_runs_on_the_project_host():
	"""
	A project name is no more unique between machines than a container name,
	so acting on one without saying where could restart the wrong stack.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	asked = []
	original = dcb.DockerManager.get_project_info
	dcb.DockerManager.get_project_info = lambda self, name: asked.append(self.host_id)
	original_send = dcb.send_message
	dcb.send_message = lambda *a, **kw: None
	try:
		dcb.run_compose_project("media", "h_nas")
		assert asked == ["h_nas"], asked
		asked.clear()
		dcb.delete_compose_project("media", "h_nas")
		assert asked == ["h_nas"], asked
		asked.clear()
		dcb.get_project_container_names("media", "h_nas")
		assert asked == ["h_nas"], asked
	finally:
		dcb.DockerManager.get_project_info = original
		dcb.send_message = original_send
		_restore_hosts()


def test_a_project_on_an_unreachable_host_gives_no_names():
	"""Rather than raising into whatever was iterating over them."""
	_with_hosts(HOST_FIXTURE)
	try:
		assert dcb.get_project_container_names("media", "h_nas") == []
	finally:
		_restore_hosts()


def test_scheduling_offers_containers_from_every_host():
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("plex", "running")] if self.host_id == "h_nas" else [_container("nginx", "running")])
	try:
		available = dcb._get_available_containers()
		assert {entry["id"] for entry, _ in available} == {"h_local", "h_nas"}
		assert {container.name for _, container in available} == {"nginx", "plex"}
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_bot_is_never_offered_to_a_scheduled_task():
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [
		_container(dcb.CONTAINER_NAME, "running"), _container("nginx", "running")]
	try:
		names = {container.name for _, container in dcb._get_available_containers()}
		assert names == {"nginx"}, names
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_three_renderers_put_the_host_in_the_same_place():
	"""
	The creation summary, the listing and the edit screen each build their own
	text. With the host in a different position in each, the same task read
	three different ways round.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	task = {"id": 1, "name": "Limpieza", "cron": "@hourly", "action": "prune",
			"prune_type": "images", "show_output": False, "host": "h_nas",
			"enabled": True}
	captured = {}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: captured.update(kwargs)
	original_all = dcb.schedule_manager.get_all_schedules
	dcb.schedule_manager.get_all_schedules = lambda: [task]
	original_get = dcb.schedule_manager.get_schedule
	dcb.schedule_manager.get_schedule = lambda _name: task
	try:
		texts = {}
		texts["summary"] = dcb._build_schedule_summary(task)
		dcb.show_schedule_menu(1, 1)
		texts["listing"] = captured["message"]
		captured.clear()
		dcb.show_schedule_edit_options(1, "Limpieza")
		texts["edit"] = captured.get("message", "")

		prune_label = dcb.get_text("schedule_label_prune_type")
		host_label_text = dcb.get_text("schedule_label_host")
		output_label = dcb.get_text("schedule_label_show_output")
		for where, text in texts.items():
			assert host_label_text in text, (where, text)
			positions = [text.index(prune_label), text.index(host_label_text),
						text.index(output_label)]
			assert positions == sorted(positions), (where, text)
	finally:
		dcb.send_message = original
		dcb.schedule_manager.get_all_schedules = original_all
		dcb.schedule_manager.get_schedule = original_get
		_restore_hosts()


def test_a_mute_task_belongs_to_no_host():
	"""
	Muting silences the bot's own notifications. Showing it a host would be
	claiming something that means nothing, and the first version of this
	stamped one onto every task alike.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.send_message
	dcb.send_message = lambda *a, **kw: None
	original_save = dcb.save_schedule_state
	dcb.save_schedule_state = lambda *a, **k: None
	try:
		# Even with one stored, the summary does not show it.
		summary = dcb._build_schedule_summary(
			{"name": "silencio", "action": "mute", "minutes": 30, "host": "h_nas"})
		assert dcb.get_text("schedule_label_host") not in summary, summary

		# And confirming clears it rather than pinning the local one.
		state = {"name": "silencio", "action": "mute", "minutes": 30}
		dcb.confirm_schedule_creation(1, state)
		assert state["host"] is None, state

		# While a task that does act on Docker still gets one.
		state = {"name": "limpieza", "action": "prune", "prune_type": "images"}
		dcb.confirm_schedule_creation(1, state)
		assert state["host"] == "h_local", state
	finally:
		dcb.send_message = original
		dcb.save_schedule_state = original_save
		_restore_hosts()


def test_every_schedule_action_is_classified():
	"""
	A new action missing from the set would silently be treated as belonging
	to no host, and its task would run wherever the executor guessed.
	"""
	from config import HOST_SCOPED_SCHEDULE_ACTIONS, SCHEDULE_PATTERNS

	unclassified = set(SCHEDULE_PATTERNS) - HOST_SCOPED_SCHEDULE_ACTIONS - {"mute"}
	assert not unclassified, f"acciones sin clasificar: {sorted(unclassified)}"
	assert HOST_SCOPED_SCHEDULE_ACTIONS <= set(SCHEDULE_PATTERNS)


def test_the_flow_asks_in_the_order_the_summary_lists():
	"""
	So the summary only grows downwards as the answers come in and the last
	thing answered is the last line, instead of appearing in the middle.
	"""
	order = ["name", "cron", "action", "container", "minutes", "prune_type",
			"host", "show_output", "command"]
	state = {key: "x" for key in order}
	state["action"] = "prune"
	state["show_output"] = True
	_with_hosts(HOST_FIXTURE, unreachable=())
	try:
		summary = dcb._build_schedule_summary(state)
		labels = [
			("prune_type", dcb.get_text("schedule_label_prune_type")),
			("host", dcb.get_text("schedule_label_host")),
			("show_output", dcb.get_text("schedule_label_show_output")),
		]
		positions = [summary.index(label) for _, label in labels]
		assert positions == sorted(positions), summary
	finally:
		_restore_hosts()


def test_a_scheduled_prune_is_asked_which_host():
	"""
	It picks no container, so nothing else in the flow would say where it runs.
	Interactive /prune asks the machine before the object type; this follows it
	for the same reason: it deletes things.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	captured = {}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: captured.update(kwargs)
	original_save = dcb.save_schedule_state
	dcb.save_schedule_state = lambda *a, **k: None
	try:
		dcb.ask_schedule_prune_host(1, {"name": "nightly", "action": "prune"})
		callbacks = harness.keyboard_callbacks(captured["reply_markup"])
		assert "scheduleSelectHost|h_local" in callbacks, callbacks
		assert "scheduleSelectHost|h_nas" in callbacks, callbacks
		for data in callbacks:
			callback_registry.parse(data)
	finally:
		dcb.send_message = original
		dcb.save_schedule_state = original_save
		_restore_hosts()


def test_the_prune_type_step_offers_the_four_kinds():
	captured = {}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: captured.update(kwargs)
	original_save = dcb.save_schedule_state
	dcb.save_schedule_state = lambda *a, **k: None
	try:
		dcb.ask_schedule_prune_type(1, {"name": "nightly", "action": "prune"})
		callbacks = harness.keyboard_callbacks(captured["reply_markup"])
		for kind in ("containers", "images", "networks", "volumes"):
			assert f"scheduleSelectPruneType|{kind}" in callbacks, callbacks
	finally:
		dcb.send_message = original
		dcb.save_schedule_state = original_save


def test_a_task_always_ends_up_with_a_host():
	"""
	Confirming records the local host when nothing asked for one, so the
	executor never has to guess.
	"""
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	state = {"name": "nightly", "action": "prune", "prune_type": "images"}
	original = dcb.send_message
	dcb.send_message = lambda **kwargs: None
	original_save = dcb.save_schedule_state
	dcb.save_schedule_state = lambda *a, **k: None
	try:
		dcb.confirm_schedule_creation(1, state)
		assert state["host"] == "h_local", state
	finally:
		dcb.send_message = original
		dcb.save_schedule_state = original_save
		_restore_hosts()


def test_the_schedule_summary_says_which_machine():
	"""
	A prune task picks no container, so nothing implies its host. Leaving it
	blank and falling back silently is what this avoids.
	"""
	_with_hosts(HOST_FIXTURE, unreachable=())
	try:
		summary = dcb._build_schedule_summary(
			{"name": "nightly", "action": "prune", "prune_type": "images", "host": "h_nas"})
		assert "nas" in summary, summary

		# Not defaulted: the step that asks for the host renders this summary
		# above the question, and a fallback would claim one while still asking.
		asking = dcb._build_schedule_summary({"name": "nightly", "action": "prune"})
		assert dcb.get_text("schedule_label_host") not in asking, asking

		# With one host the summary reads as it always did.
		_with_hosts([HOST_FIXTURE[0]], unreachable=())
		summary = dcb._build_schedule_summary(
			{"name": "nightly", "action": "prune", "prune_type": "images"})
		assert "casa" not in summary, summary
	finally:
		_restore_hosts()


def test_the_start_header_counts_every_host():
	_with_hosts(HOST_FIXTURE, unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("plex", "running"), _container("tautulli", "exited")]
		if self.host_id == "h_nas" else [_container("nginx", "running")])
	try:
		summary = dcb._start_summary()
		assert "3" in summary, summary
		# How many machines those containers came from, so the totals can be read.
		assert "<b>2</b>" in summary, summary
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_start_header_keeps_quiet_about_servers_when_there_is_only_one():
	"""Saying "servers: 1" is noise about a concept a single-host owner never had."""
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": [_container("nginx", "running")]
	try:
		summary = dcb._start_summary()
		assert summary == dcb.get_text("start_summary", 1, 1, 0), summary
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_start_header_admits_a_host_it_could_not_reach():
	"""
	The counts are missing that machine's containers: a bare total would show a
	partial count as the whole picture.
	"""
	_with_hosts(HOST_FIXTURE)   # nas caído
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (
		[_container("nginx", "running")] if self.host_id == "h_local"
		else (_ for _ in ()).throw(Exception("no route to host")))
	try:
		summary = dcb._start_summary()
		assert "1/2" in summary, summary
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_the_start_header_says_nothing_when_no_host_answers():
	"""
	Zero containers would read as "everything is gone" rather than "I cannot
	see anything".
	"""
	_with_hosts(HOST_FIXTURE)   # los dos con nas caído...
	original = dcb.DockerManager.list_containers
	dcb.DockerManager.list_containers = lambda self, comando="": (_ for _ in ()).throw(Exception("down"))
	try:
		assert dcb._start_summary() is None
	finally:
		dcb.DockerManager.list_containers = original
		_restore_hosts()


def test_container_output_does_not_break_the_log_message():
	"""
	The logs go inside <pre><code> in a message parsed as HTML, and what a
	container prints is nobody's decision but its own: an unescaped "<" would
	have Telegram reject the message and the user would be told the logs could
	not be read, for a container that is working fine.
	"""
	_with_hosts([HOST_FIXTURE[0]], unreachable=())
	manager = dcb.DockerManager("h_local")
	noisy = MagicMock()
	noisy.logs.return_value = b"<script>alert(1)</script> & </code></pre>"
	manager.client = MagicMock()
	manager.client.containers.get.return_value = noisy
	try:
		text = manager.show_logs("abc123", "plex")
		assert "&lt;script&gt;" in text
		assert "<script>" not in text
		# The markup the template itself puts around the output stays.
		assert text.count("<pre><code>") == 1
		assert text.count("</code></pre>") == 1
	finally:
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


def test_asking_if_a_host_is_there_gives_up_sooner_than_working_on_it():
	"""
	The SDK applies its timeout to every request, not just to connecting, so
	one number cannot serve both jobs: an /exec has to be allowed to run for as
	long as the command takes, while a menu must not spend that long finding
	out a machine is unplugged.
	"""
	import copy
	import docker
	import host_registry

	asked = []

	def sdk(base_url=None, timeout=None, **kwargs):
		asked.append((base_url, timeout))
		return MagicMock()

	docker.DockerClient = sdk
	store.set("hosts", copy.deepcopy(HOST_FIXTURE))
	host_registry.reset()
	try:
		host_registry.ping("h_nas")
		host_registry.client("h_nas")
		timeouts = dict((url, timeout) for url, timeout in asked)
		assert timeouts["tcp://nas:2375"] == host_registry.DEFAULT_TIMEOUT_SECONDS
		assert host_registry.PROBE_TIMEOUT_SECONDS in [t for _, t in asked]
		assert host_registry.PROBE_TIMEOUT_SECONDS < host_registry.DEFAULT_TIMEOUT_SECONDS

		# And the probe is not what callers end up operating through.
		working = host_registry.client("h_nas")
		assert working is not host_registry.probe_client("h_nas")
	finally:
		_restore_hosts()


def test_dropping_a_host_forgets_the_probe_too():
	"""
	Leaving the probe behind means the next check answers about a connection
	the rest of the bot has already given up on.
	"""
	import copy
	import docker
	import host_registry

	docker.DockerClient = lambda **kwargs: MagicMock()
	store.set("hosts", copy.deepcopy(HOST_FIXTURE))
	host_registry.reset()
	try:
		first = host_registry.probe_client("h_nas")
		assert host_registry.probe_client("h_nas") is first, "debe cachearse"
		host_registry.drop("h_nas")
		assert host_registry.probe_client("h_nas") is not first
	finally:
		_restore_hosts()


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


def test_renaming_a_host_does_not_lose_one_added_at_the_same_time():
	"""
	Renaming rewrites the whole host list from a copy it read. Without the
	registry's lock, an add landing in between is written over and the host
	silently disappears from the settings.
	"""
	registry = _with_hosts(HOST_FIXTURE, unreachable=())
	original_set = store.set
	midway = threading.Event()

	def slow_set(key, value):
		# Widens the window between the read and the write, which is the race.
		if key == "hosts" and not midway.is_set():
			midway.set()
			time.sleep(0.05)
		return original_set(key, value)

	adder = threading.Thread(
		target=lambda: (midway.wait(1),
						registry.add_host("nuevo", "tcp://nuevo:2375")))
	store.set = slow_set
	try:
		adder.start()
		registry.rename_host("h_nas", "sinologia")
		adder.join(2)
	finally:
		store.set = original_set

	try:
		aliases = [h["alias"] for h in registry.hosts()]
		assert "sinologia" in aliases, aliases
		assert "nuevo" in aliases, aliases
	finally:
		_restore_hosts()


def test_a_host_name_with_markup_does_not_break_the_message():
	"""
	Aliases are typed by hand, and every screen that names a host is parsed as
	HTML: an unescaped `<` would make Telegram reject the whole message.
	"""
	hosts = [
		HOST_FIXTURE[0],
		{"id": "h_nas", "alias": "<b>nas", "url": "tcp://nas:2375"},
	]
	_with_hosts(hosts, unreachable=())
	try:
		assert dcb.host_alias("h_nas") == "&lt;b&gt;nas"
		assert "<b>nas" not in dcb.host_label("h_nas")
		text, _ = dcb.build_settings_host("h_nas")
		assert "&lt;b&gt;nas" in text
		text, _ = dcb.build_settings_host_remove("h_nas")
		assert "&lt;b&gt;nas" in text
		assert "&lt;b&gt;nas" in dcb.prune_prompt("h_nas")
	finally:
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


def test_a_host_that_never_answers_does_not_pile_up_threads():
	"""
	Giving up on the deadline does not stop the ping, so without remembering
	the one already running every refresh of the menu would leave another
	thread behind on the same unresponsive machine.
	"""
	import host_registry
	release = threading.Event()
	import docker

	def sdk(base_url=None, **kwargs):
		fake = MagicMock()
		fake.ping.side_effect = lambda: release.wait(10)
		return fake

	docker.DockerClient = sdk
	store.set("hosts", [{"id": "h_slow", "alias": "lento", "url": "tcp://slow:2375"}])
	host_registry.reset()
	try:
		before = threading.active_count()
		for _ in range(5):
			statuses = host_registry.status_snapshot(deadline_seconds=0.1)
			assert statuses["h_slow"][0] is False
		assert threading.active_count() <= before + 1, "un hilo por sondeo"
	finally:
		release.set()
		_restore_hosts()


def test_looking_at_one_host_does_not_wait_for_the_others():
	"""
	build_settings_host used to sweep the whole fleet to draw a single screen,
	so opening the local host waited on every unplugged machine.
	"""
	import host_registry
	_with_hosts(HOST_FIXTURE, unreachable=())
	probed = []
	original = host_registry.status_snapshot

	def spy(deadline_seconds=5, entries=None):
		probed.append([e["id"] for e in (entries or host_registry.hosts())])
		return original(deadline_seconds, entries)

	host_registry.status_snapshot = spy
	try:
		dcb.build_settings_host("h_local")
		assert probed == [["h_local"]], probed
	finally:
		host_registry.status_snapshot = original
		_restore_hosts()


# ---------------------------------------------------------------------------
# Robustness of the dispatcher
# ---------------------------------------------------------------------------

def test_a_press_survives_telegram_not_answering():
	"""
	Reported as "error processing request" when opening the hosts menu. The
	cause was a dropped connection while acknowledging the press: answering is
	cosmetic, and letting it abort turned the action into nothing at all.
	"""
	original = dcb.bot.answer_callback_query

	def dropped(*args, **kwargs):
		raise Exception("SSLEOFError: EOF occurred in violation of protocol")

	dcb.bot.answer_callback_query = dropped
	try:
		assert dcb.answer_callback_quietly("whatever") is False, "debe tragarse el fallo"
	finally:
		dcb.bot.answer_callback_query = original

	# And it reports success when Telegram does answer.
	dcb.bot.answer_callback_query = lambda *a, **kw: True
	try:
		assert dcb.answer_callback_quietly("whatever") is True
	finally:
		dcb.bot.answer_callback_query = original


def test_nothing_answers_a_callback_without_the_guard():
	"""
	Every acknowledgement goes through the guard, or one of them can still
	abort a press the next time the network hiccups.
	"""
	for filename in ("core.py", "callbacks.py", "commands.py"):
		source = io.open(os.path.join(harness.REPO, filename), encoding="utf-8").read()
		for line_number, line in enumerate(source.split("\n"), start=1):
			if "answer_callback_query(" in line and "def " not in line:
				assert "answer_callback_quietly" in line or "bot.answer_callback_query(callback_id" in line, \
					f"{filename}:{line_number} responde sin la guarda: {line.strip()}"


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
