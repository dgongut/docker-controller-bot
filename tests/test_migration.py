"""
The 4.x to 5.0 migration.

The promise is that updating changes nothing the user can see: every value
their compose set is the value they keep, and it only moves house. These tests
are the proof of that promise.
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402  (adds the repo to sys.path)
import migration
import store


def test_a_4_x_compose_is_imported_verbatim():
	store_, root = harness.temp_storage(env={
		"LANGUAGE": "IT",
		"BUTTON_COLUMNS": "3",
		"EXTENDED_MESSAGES": "1",
		"MULTI_SELECTION": "0",
		"CHECK_UPDATES": "1",
		"CHECK_UPDATE_EVERY_HOURS": "6",
		"CHECK_UPDATE_STOPPED_CONTAINERS": "0",
		"TELEGRAM_NOTIFICATION_CHANNEL": "-100999",
	})
	result = migration.run()

	assert store_.get("bot.language") == "IT"
	assert store_.get("bot.button_columns") == 3
	assert store_.get("bot.extended_messages") is True
	assert store_.get("bot.multi_selection") is False
	assert store_.get("bot.check_updates") is True
	assert store_.get("bot.check_update_every_hours") == 6.0
	assert store_.get("bot.check_update_stopped_containers") is False
	assert store_.get("bot.notification_channel") == "-100999"
	# The language was there, so there is nothing to ask.
	assert result.ask_for_language is False
	shutil.rmtree(root, ignore_errors=True)


def test_the_language_is_normalised():
	"""
	The variable was accepted in any case. Stored verbatim it would leave "es"
	for someone upgrading and "ES" for someone who picked it from the menu.
	"""
	store_, root = harness.temp_storage(env={"LANGUAGE": "es"})
	migration.run()
	assert store_.get("bot.language") == "ES"
	shutil.rmtree(root, ignore_errors=True)


def test_the_environment_never_wins_after_the_first_run():
	"""
	Otherwise the user changes a setting from Telegram, restarts, and watches it
	revert because their compose still sets it.
	"""
	store_, root = harness.temp_storage(env={"LANGUAGE": "IT"})
	first = migration.run()

	store_.set("bot.language", "GL")
	store_.reload()
	second = migration.run()

	assert store_.get("bot.language") == "GL"
	assert second.host_id == first.host_id, "el id del host local debe ser estable"
	assert second.ask_for_language is False
	assert len(store_.get("hosts")) == 1
	shutil.rmtree(root, ignore_errors=True)


def test_a_new_install_is_asked_for_a_language():
	store_, root = harness.temp_storage()
	result = migration.run()
	assert result.ask_for_language is True
	assert store_.get("bot.language") == "ES"
	assert store_.get("bot.check_update_every_hours") == 4.0
	shutil.rmtree(root, ignore_errors=True)


def test_a_new_install_that_set_a_language_is_not_asked():
	store_, root = harness.temp_storage(env={"LANGUAGE": "DE"})
	result = migration.run()
	assert result.ask_for_language is False
	assert store_.get("bot.language") == "DE"
	shutil.rmtree(root, ignore_errors=True)


def test_the_local_host_is_registered_once():
	store_, root = harness.temp_storage()
	host_id = migration.run().host_id
	assert host_id.startswith("h_")
	hosts = store_.get("hosts")
	assert len(hosts) == 1
	assert hosts[0]["local"] is True
	assert hosts[0]["url"] == migration.LOCAL_SOCKET_URL
	shutil.rmtree(root, ignore_errors=True)


def test_the_4_x_pickle_cache_is_discarded():
	"""
	It is not migrated: it lived outside any mapped volume, and it stored the
	translated status message rather than a boolean, so its entries became
	unreadable the moment the language changed.
	"""
	store_, root = harness.temp_storage()
	os.makedirs("cache", exist_ok=True)
	for name in ("nginx_latest_nginx", "redis_7_redis", "update_data_1_2"):
		with open(os.path.join("cache", name), "wb") as handle:
			handle.write(b"pickled junk")

	migration.run()
	assert not os.path.isdir("cache")
	shutil.rmtree(root, ignore_errors=True)


def test_an_active_mute_is_carried_over():
	"""
	Someone who silences the bot for six hours and updates it meanwhile would
	otherwise get every notification back at once, with nothing to explain why.
	"""
	expires = time.time() + 3600
	store_, root = harness.temp_storage(legacy=True, seed_files={
		"schedules.json": '{"schedules": []}',
		".muted_until": str(expires),
	})
	migration.run()

	assert store_.root() == store_.LEGACY_ROOT
	assert abs(float(store_.state_get("mute_until")) - expires) < 1
	assert not os.path.exists(os.path.join(store_.LEGACY_ROOT, ".muted_until"))

	migration.run()  # idempotente
	assert abs(float(store_.state_get("mute_until")) - expires) < 1
	shutil.rmtree(root, ignore_errors=True)


def test_an_expired_or_absent_mute_is_dropped():
	for value in (str(time.time() - 3600), "0", "not-a-number"):
		store_, root = harness.temp_storage(legacy=True, seed_files={
			"schedules.json": '{"schedules": []}',
			".muted_until": value,
		})
		migration.run()
		assert store_.state_get("mute_until") == 0, f"con .muted_until={value!r}"
		shutil.rmtree(root, ignore_errors=True)
