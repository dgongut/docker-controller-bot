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
	assert manager.all() == []
	assert manager.set_aside and os.path.exists(manager.set_aside), manager.set_aside
	with open(manager.set_aside, encoding="utf-8") as handle:
		assert '"copia"' in handle.read()
	assert manager.add({"name": "nueva", "cron": "@daily", "action": "restart", "container": "nginx", "host": "h_local"})
	assert [s["name"] for s in ScheduleManager(store.CONFIG_ROOT, "schedules.json").all()] == ["nueva"]
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


def test_a_batch_of_one_document_does_not_hold_back_the_others():
	"""
	The update check holds the cache back for a whole host, minutes over ssh.
	A setting changed from /settings meanwhile has to reach the disk at once.
	"""
	root = fresh_root()
	updates = os.path.join(store.state_dir(), store.UPDATES_FILE)
	with store.batch("updates"):
		store.set_update_status("h_1", "plex", "plex:1", True)
		store.set("bot.language", "EN")
		with open(store.settings_path(), encoding="utf-8") as handle:
			assert '"EN"' in handle.read(), "el ajuste esperó al lote de la caché"
		assert not os.path.exists(updates) or "plex" not in open(updates).read()
	assert "plex" in open(updates).read()
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


def test_an_unsupported_language_is_warned_about_once():
	"""It is read on every text rendered: hundreds of identical warnings a screen."""
	import i18n
	root = fresh_root()
	warned = []
	original = i18n.warning
	i18n.warning = warned.append
	i18n._warned_languages.clear()
	try:
		store.set("bot.language", "klingon")
		for _ in range(50):
			assert i18n.language() == "es"
		assert len(warned) == 1, warned
	finally:
		i18n.warning = original
		shutil.rmtree(root, ignore_errors=True)


def test_a_container_setting_is_kept_by_host_and_name():
	"""
	By name, as the update cache is: every update gives the container a new
	id. Dots are allowed in a name, and a dotted path would have split it.
	"""
	root = fresh_root()
	assert store.set_container_update_mode("h_casa", "my.app", "auto") is True
	assert store.set_container_update_mode("h_nas1", "my.app", "ignore") is True
	store.reload()
	assert store.container_update_mode("h_casa", "my.app") == "auto"
	assert store.container_update_mode("h_nas1", "my.app") == "ignore"
	assert store.container_update_mode("h_casa", "other") is None
	# Back to the default, nothing is left of it: not the entry, not the host.
	store.set_container_update_mode("h_casa", "my.app", None)
	assert store.container_settings() == {"h_nas1": {"my.app": {"updates": "ignore"}}}
	shutil.rmtree(root, ignore_errors=True)


def test_a_hand_written_container_setting_is_read_for_what_it_says():
	"""Anything but the two modes is the default, rather than something truthy."""
	root = fresh_root()
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		json.dump({"containers": {"h_casa": {"plex": {"updates": " AUTO "}, "radarr": {"updates": "sí"},
											"sonarr": "auto"}}}, handle)
	store.reload()
	assert store.container_update_mode("h_casa", "plex") == "auto"
	assert store.container_update_mode("h_casa", "radarr") is None
	assert store.container_update_mode("h_casa", "sonarr") is None
	shutil.rmtree(root, ignore_errors=True)


def test_a_container_setting_is_refused_while_the_settings_cannot_be_read():
	"""Changed only in memory, it would be gone at the next restart without a word."""
	root = fresh_root()
	broken = '{"containers": {"h_casa": {"plex": {"updates": "auto"},}}}'
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		handle.write(broken)
	store.reload()
	assert store.set_container_update_mode("h_casa", "plex", "ignore") is False
	assert store.reconcile_containers({"h_casa": ["nginx"]}, 10 ** 9, 0) == []
	with open(store.settings_path(), encoding="utf-8") as handle:
		assert handle.read() == broken
	shutil.rmtree(root, ignore_errors=True)


def test_forgetting_a_host_takes_its_container_settings_and_nothing_else():
	root = fresh_root()
	store.set_container_update_mode("h_nas1", "plex", "auto")
	store.set_container_update_mode("h_casa", "plex", "ignore")
	assert store.forget_host_containers("h_nas1") is True
	assert store.forget_host_containers("h_nas1") is False
	assert store.container_settings() == {"h_casa": {"plex": {"updates": "ignore"}}}
	assert store.forget_container_settings("h_casa", "plex") is True
	assert store.container_settings() == {}
	shutil.rmtree(root, ignore_errors=True)


def test_a_container_setting_goes_only_when_it_has_been_missing_long_enough():
	"""
	Recreating a container is removing it and creating it again: a compose
	up, Watchtower, or a down that lasts a weekend. One listing without it
	says nothing; the same absence a grace period later does.
	"""
	root = fresh_root()
	store.set_container_update_mode("h_casa", "plex", "auto")
	store.set_container_update_mode("h_casa", "sonarr", "ignore")
	store.set_container_update_mode("h_nas1", "radarr", "auto")
	day = 24 * 60 * 60

	# plex missing: noted, kept. h_nas1 not listed (down or paused): untouched.
	assert store.reconcile_containers({"h_casa": ["sonarr"]}, 0, 30 * day) == []
	assert store.state_get("containers_absent") == {"h_casa:plex": 0}
	# Back before the grace ran out: the count starts again.
	assert store.reconcile_containers({"h_casa": ["plex", "sonarr"]}, 10 * day, 30 * day) == []
	assert store.state_get("containers_absent") == {}
	# Missing again, and long enough this time. A restart in between does not
	# reset the count, which lives in state/.
	store.reconcile_containers({"h_casa": ["sonarr"]}, 20 * day, 30 * day)
	store.reload()
	assert store.reconcile_containers({"h_casa": ["sonarr"]}, 49 * day, 30 * day) == []
	assert store.reconcile_containers({"h_casa": ["sonarr"]}, 50 * day, 30 * day) == [("h_casa", "plex")]
	assert store.container_update_mode("h_casa", "plex") is None
	assert store.state_get("containers_absent") == {}
	# A daemon answering with nothing is more likely the wrong one.
	assert store.reconcile_containers({"h_nas1": []}, 999 * day, 30 * day) == []
	assert store.container_update_mode("h_nas1", "radarr") == "auto"
	shutil.rmtree(root, ignore_errors=True)


def test_the_first_update_check_is_remembered_beyond_the_cache():
	"""
	An empty cache was the sign of a first pass, which announces nothing. The
	cache also empties when its last entry is forgotten, and the pass after
	that took real news for old news and saved it as announced.
	"""
	root = fresh_root()
	assert store.update_check_done() is False
	store.set_update_status("h_casa", "plex", "plex:latest", False)
	assert store.update_check_done() is True
	store.mark_update_check_done()
	store.forget_update_status("h_casa", "plex")
	store.reload()
	assert store.update_check_done() is True
	shutil.rmtree(root, ignore_errors=True)


def test_an_absence_mark_goes_with_its_setting():
	"""
	Left behind, it counted for the next setting: a container set again weeks
	later lost it the first time it was recreated, said to be a month gone.
	"""
	root = fresh_root()
	day = 24 * 60 * 60
	store.set_container_update_mode("h_casa", "app", "auto")
	store.reconcile_containers({"h_casa": ["other"]}, 0, 30 * day)
	assert store.state_get("containers_absent") == {"h_casa:app": 0}
	store.set_container_update_mode("h_casa", "app", None)
	assert store.state_get("containers_absent") == {}
	store.set_container_update_mode("h_casa", "app", "auto")
	assert store.reconcile_containers({"h_casa": ["other"]}, 60 * day, 30 * day) == []
	store.forget_host_containers("h_casa")
	assert store.state_get("containers_absent") == {}
	shutil.rmtree(root, ignore_errors=True)


def test_hand_written_settings_of_the_wrong_shape_count_as_none():
	"""
	A host left as `true`, or a container as a string, crashed the screens
	that count them, the host removal among them; and a mark written without
	a host made the cleanup raise on every pass.
	"""
	root = fresh_root()
	with open(store.settings_path(), "w", encoding="utf-8") as handle:
		json.dump({"containers": {"h_bad": True, "h_list": ["x"], "h_ok": {"plex": {"updates": "auto"}, "junk": "auto"}}}, handle)
	os.makedirs(store.state_dir(), exist_ok=True)
	with open(os.path.join(store.state_dir(), store.STATE_FILE), "w", encoding="utf-8") as handle:
		json.dump({"containers_absent": {"sin-host": 5, "h_bad:x": 5, "h_ok:plex": "ayer"}}, handle)
	store.reload()
	assert store.container_settings() == {"h_ok": {"plex": {"updates": "auto"}}}
	assert store.reconcile_containers({"h_ok": ["plex"], "h_bad": ["x"]}, 100, 50) == []
	assert store.state_get("containers_absent") == {}
	shutil.rmtree(root, ignore_errors=True)
