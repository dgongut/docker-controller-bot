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

import callback_registry

dcb, store, _root = harness.load_bot(env={"LANGUAGE": "ES", "BUTTON_COLUMNS": "3"})

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
	assert dcb.get_text("check_for_updates") == dcb.load_locale("en")["check_for_updates"]
	store.set("bot.language", "IT")
	assert dcb.get_text("check_for_updates") == dcb.load_locale("it")["check_for_updates"]
	# An unsupported value falls back rather than crashing on a missing file.
	store.set("bot.language", "XX")
	assert dcb.get_text("check_for_updates") == dcb.load_locale("es")["check_for_updates"]
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
	assert dcb.update_status_text(True) == dcb.load_locale("de")["NEED_UPDATE_CONTAINER_TEXT"]
	store.set("bot.language", "ES")
	assert dcb.update_status_text(True) == dcb.load_locale("es")["NEED_UPDATE_CONTAINER_TEXT"]
	assert dcb.update_status_text(False) == dcb.load_locale("es")["UPDATED_CONTAINER_TEXT"]
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


def test_mute_asks_for_its_argument_when_pressed_as_a_button():
	asked = {}
	original = dcb.ask_text_input
	dcb.ask_text_input = lambda user, field, prompt, back_to="main": asked.update(
		field=field, back=back_to)
	try:
		dcb.cmd_mute(user_id=1)
	finally:
		dcb.ask_text_input = original
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
		keys = dcb.load_locale(locale)
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
	replaces_message = {"settingsAskInterval", "settingsAskChannel", "cancelTextInput",
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
	generated = {f"enter{a}Project" for a in dcb.PROJECT_NAVIGATION_ACTIONS}
	generated |= {f"backTo{a}Level1" for a in dcb.PROJECT_NAVIGATION_ACTIONS}
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
