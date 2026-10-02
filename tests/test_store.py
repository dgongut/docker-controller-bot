"""
Storage layer: settings, runtime state and the update cache.

These run without importing the bot, so they are fast and cover the parts that
have to keep working even when everything else is broken: a corrupt file must
not stop the bot from starting, and an upgrade must not lose a setting.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store


def fresh_root():
	root = tempfile.mkdtemp()
	store.CONFIG_ROOT = os.path.join(root, "config")
	store.LEGACY_ROOT = os.path.join(root, "schedule")
	os.makedirs(store.CONFIG_ROOT)
	store._root = None
	store.reload()
	return root


def test_defaults():
	root = fresh_root()
	assert store.get("bot.language") == "ES"
	assert store.get("bot.check_update_every_hours") == 4.0
	assert store.get("bot.button_columns") == 2
	assert store.get("hosts") == []
	# An unknown key is None rather than an error, so a stale caller cannot
	# bring the bot down.
	assert store.get("bot.does_not_exist") is None
	shutil.rmtree(root, ignore_errors=True)


def test_writes_survive_a_reload():
	root = fresh_root()
	store.set("bot.language", "EN")
	store.set("bot.button_columns", 3)
	assert store.toggle("bot.extended_messages") is True
	store.set("hosts", [{"id": "h_a91c", "alias": "nas"}])

	store.reload()
	assert store.get("bot.language") == "EN"
	assert store.get("bot.button_columns") == 3
	assert store.get("bot.extended_messages") is True
	assert store.get("hosts")[0]["id"] == "h_a91c"
	# Keys nobody touched still answer with their default.
	assert store.get("bot.multi_selection") is True
	shutil.rmtree(root, ignore_errors=True)


def test_what_get_hands_out_cannot_change_the_store():
	"""
	The live list went out, so changing an entry in place —pausing a host
	did— changed the document with no lock held, while another thread could
	be writing it out.
	"""
	root = fresh_root()
	store.set("hosts", [{"id": "h_a91c", "alias": "nas"}])
	hosts = store.get("hosts")
	hosts[0]["paused"] = True
	hosts.append({"id": "h_otro"})
	assert store.get("hosts") == [{"id": "h_a91c", "alias": "nas"}]
	shutil.rmtree(root, ignore_errors=True)


def test_a_hand_edited_value_is_read_as_the_type_it_should_be():
	"""`"telemetry": "false"` is a non-empty string, and every caller tests for truth."""
	root = fresh_root()
	for raw, expected in (("false", False), ("0", False), ("no", False), ("", False), ("falso", False),
							("true", True), ("1", True), (" Yes ", True), (0, False), (1, True), (None, True)):
		store.set("bot.telemetry", raw)
		assert store.get("bot.telemetry") is expected, (raw, store.get("bot.telemetry"))
	store.set("bot.button_columns", "3")
	assert store.get("bot.button_columns") == 3
	store.set("bot.button_columns", "tres")
	assert store.get("bot.button_columns") == 2
	shutil.rmtree(root, ignore_errors=True)


def test_unknown_keys_are_preserved():
	"""A downgrade must not throw away what a newer version wrote."""
	root = fresh_root()
	store.set("bot.language", "EN")
	with open(store.settings_path(), encoding="utf-8") as handle:
		document = json.load(handle)
	document["future_section"] = {"whatever": 1}
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		json.dump(document, handle)

	store.reload()
	store.set("bot.language", "GL")
	with open(store.settings_path(), encoding="utf-8") as handle:
		assert json.load(handle)["future_section"] == {"whatever": 1}
	shutil.rmtree(root, ignore_errors=True)


def test_a_corrupt_file_falls_back_to_defaults():
	root = fresh_root()
	store.set("bot.language", "IT")
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		handle.write("{ this is not json")

	store.reload()
	assert store.get("bot.language") == "ES", "un fichero corrupto no debe impedir arrancar"
	shutil.rmtree(root, ignore_errors=True)


def test_an_unreadable_settings_file_is_never_written_over():
	"""
	A trailing comma in a hand edit —the README sends TLS users to this file—
	made the bot start on defaults and then save them: the hosts, the language
	and a telemetry the user had turned off, all replaced. The file stays as it
	was until someone fixes it, and the bot says why.
	"""
	root = fresh_root()
	broken = '{"bot": {"language": "IT",}, "hosts": [{"id": "h_aaaa", "local": true}]}'
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		handle.write(broken)

	store.reload()
	assert store.get("bot.language") == "ES"
	assert store.settings_unreadable(), "no dice por qué no ha podido leerlo"
	store.set("hosts", [{"id": "h_bbbb", "local": True}])
	store.set("bot.language", "EN")
	with open(store.settings_path(), encoding="utf-8") as handle:
		assert handle.read() == broken, "ha escrito encima del fichero del usuario"
	# Within this run the change still holds, so the bot keeps working.
	assert store.get("bot.language") == "EN"

	# Once fixed, a restart reads it and writes normally again.
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		handle.write(broken.replace(",}", "}"))
	store.reload()
	assert store.settings_unreadable() is None
	assert store.get("bot.language") == "IT"
	store.set("bot.language", "DE")
	store.reload()
	assert store.get("bot.language") == "DE"
	shutil.rmtree(root, ignore_errors=True)


def test_an_unreadable_schedules_file_is_set_aside_not_emptied():
	"""Starting empty keeps scheduling usable; the first write must not erase the tasks."""
	from schedule_manager import ScheduleManager
	root = fresh_root()
	path = os.path.join(store.CONFIG_ROOT, "schedules.json")
	with open(path, "w", encoding="utf-8") as handle:
		handle.write('{"schedules": [{"id": 1, "name": "copia",}]}')

	manager = ScheduleManager(store.CONFIG_ROOT, "schedules.json")
	assert manager.get_all_schedules() == []
	assert manager.set_aside and os.path.exists(manager.set_aside), manager.set_aside
	with open(manager.set_aside, encoding="utf-8") as handle:
		assert '"copia"' in handle.read()
	assert manager.add_schedule(name="nueva", cron="@daily", action="restart", container="nginx")
	assert [s["name"] for s in ScheduleManager(store.CONFIG_ROOT, "schedules.json").get_all_schedules()] == ["nueva"]
	shutil.rmtree(root, ignore_errors=True)


def test_runtime_state():
	root = fresh_root()
	assert store.state_get("mute_until") == 0
	store.state_set("mute_until", 1234.5)
	store.reload()
	assert store.state_get("mute_until") == 1234.5
	shutil.rmtree(root, ignore_errors=True)


def test_update_cache_is_a_boolean_per_container():
	root = fresh_root()
	assert store.has_update_cache() is False
	assert store.update_status("h_1", "nginx", "nginx:1.27") is None

	store.set_update_status("h_1", "nginx", "nginx:1.27", True, checked_at="2026-09-02T11:00:00")
	store.set_update_status("h_1", "redis", "redis:7", False)
	assert store.has_update_cache() is True
	assert store.update_status("h_1", "nginx", "nginx:1.27") is True
	assert store.update_status("h_1", "redis", "redis:7") is False
	shutil.rmtree(root, ignore_errors=True)


def test_a_tag_change_invalidates_without_leaving_an_orphan():
	"""
	4.x keyed on the image, so every tag a container had ever run left an entry
	behind for good. Keying on the container and comparing the stored image
	gives the same invalidation with one entry per container.
	"""
	root = fresh_root()
	store.set_update_status("h_1", "nginx", "nginx:1.27", True)
	assert store.update_status("h_1", "nginx", "nginx:1.28") is None

	store.set_update_status("h_1", "nginx", "nginx:1.28", False)
	assert len(store.update_entries()) == 1, store.update_entries()
	assert store.update_status("h_1", "nginx", "nginx:1.28") is False
	shutil.rmtree(root, ignore_errors=True)


def test_the_same_container_name_on_two_hosts():
	root = fresh_root()
	store.set_update_status("h_1", "nginx", "nginx:1.27", True)
	store.set_update_status("h_2", "nginx", "nginx:1.27", False)
	assert len(store.update_entries()) == 2
	assert store.update_status("h_1", "nginx", "nginx:1.27") is True
	assert store.update_status("h_2", "nginx", "nginx:1.27") is False
	shutil.rmtree(root, ignore_errors=True)


def test_forgetting_and_clearing():
	root = fresh_root()
	store.set_update_status("h_1", "nginx", "nginx:1.27", True)
	store.set_update_status("h_1", "redis", "redis:7", False)
	store.forget_update_status("h_1", "redis")
	assert len(store.update_entries()) == 1
	store.reload()
	assert len(store.update_entries()) == 1
	store.clear_update_cache()
	assert store.has_update_cache() is False
	shutil.rmtree(root, ignore_errors=True)


def test_batch_writes_once():
	"""
	An update pass touches every container. Without batching that is one full
	rewrite of the cache per container, which matters on the SD cards this
	tends to run on.
	"""
	root = fresh_root()
	path = os.path.join(store.state_dir(), store.UPDATES_FILE)
	if os.path.exists(path):
		os.remove(path)

	with store.batch():
		for index in range(50):
			store.set_update_status("h_1", f"container{index}", "image:1", index % 2 == 0)
		assert not os.path.exists(path), "batch escribió antes de salir del bloque"

	assert os.path.exists(path)
	store.reload()
	assert len(store.update_entries()) == 50
	assert store.update_status("h_1", "container0", "image:1") is True
	assert store.update_status("h_1", "container1", "image:1") is False
	shutil.rmtree(root, ignore_errors=True)


def test_the_legacy_volume_wins_when_it_is_the_one_with_data():
	"""A compose written for 4.x maps /app/schedule and must keep working."""
	root = tempfile.mkdtemp()
	store.CONFIG_ROOT = os.path.join(root, "config")
	store.LEGACY_ROOT = os.path.join(root, "schedule")
	os.makedirs(store.LEGACY_ROOT)
	with open(os.path.join(store.LEGACY_ROOT, "schedules.json"), "w", encoding="utf-8") as handle:
		handle.write('{"schedules": []}')
	store._root = None
	store.reload()

	assert store.init() == store.LEGACY_ROOT
	assert store.uses_legacy_root() is True
	assert store.schedules_path() == os.path.join(store.LEGACY_ROOT, "schedules.json")
	shutil.rmtree(root, ignore_errors=True)


def test_persistence_looks_at_parent_directories():
	"""
	A volume does not have to be mapped at exactly the storage path: mapping a
	parent works too, which is what the development compose does with /app.
	"""
	root = fresh_root()
	nested = os.path.join(store.CONFIG_ROOT, "deeper", "still")
	os.makedirs(nested, exist_ok=True)
	original = store._is_mounted
	try:
		store._is_mounted = lambda path: path == store.CONFIG_ROOT
		assert store._is_persisted(nested) is True
		store._is_mounted = lambda path: False
		assert store._is_persisted(nested) is False
	finally:
		store._is_mounted = original
	shutil.rmtree(root, ignore_errors=True)


def test_removing_a_host_forgets_its_update_cache():
	"""Nothing else ever cleared those entries: they stayed in updates.json for good."""
	root = fresh_root()
	store.set_update_status("h_nas1", "plex", "plex:latest", True)
	store.set_update_status("h_nas1", "sonarr", "sonarr:latest", False)
	store.set_update_status("h_casa", "plex", "plex:latest", True)
	store.forget_host_updates("h_nas1")
	assert list(store.update_entries()) == ["h_casa:plex"], store.update_entries()
	shutil.rmtree(root, ignore_errors=True)
