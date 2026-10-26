"""
Muting the bot from the command line through .muted_until.

A script runs `docker exec <bot> sh -c "echo <epoch> > /app/config/.muted_until"`
while the bot is up; each pass of the watcher applies the file and deletes it.
"""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

dcb, store, _root = harness.load_bot()


def _write(content, age=5):
	path = os.path.join(store.root(), dcb.EXTERNAL_MUTE_FILE)
	with open(path, "w", encoding="utf-8") as handle:
		handle.write(f"{content}\n")
	# Older than the settle time, as a file the shell has finished writing is.
	stamp = time.time() - age
	os.utime(path, (stamp, stamp))
	return path


class _Quiet:
	"""Starts unmuted, with nothing sent to Telegram, and leaves it unmuted."""

	def __enter__(self):
		self.original = dcb.send_message
		dcb.send_message = MagicMock()
		dcb.unmute()
		return dcb.send_message

	def __exit__(self, *exc):
		dcb.unmute()
		dcb.send_message = self.original


def test_the_file_mutes_the_bot_and_is_deleted():
	with _Quiet():
		path = _write(int(time.time()) + 600)
		dcb.check_external_mute()
		assert dcb.is_muted()
		# Rounded up to whole minutes, never shorter than asked.
		assert 595 <= dcb._mute_until() - time.time() <= 660
		assert not os.path.exists(path)
		assert dcb._unmute_timer is not None


def test_no_file_changes_nothing():
	with _Quiet():
		dcb.check_external_mute()
		assert not dcb.is_muted()


def test_the_file_replaces_a_longer_mute_from_telegram():
	"""The latest change wins, shorter too."""
	with _Quiet():
		dcb.mute(480, asked=True)
		_write(int(time.time()) + 120)
		dcb.check_external_mute()
		assert 0 < dcb._mute_until() - time.time() <= 180


def test_a_mute_from_telegram_replaces_the_file():
	with _Quiet():
		_write(int(time.time()) + 7200)
		dcb.check_external_mute()
		dcb.mute(10, asked=True)
		assert 0 < dcb._mute_until() - time.time() <= 600


def test_zero_unmutes():
	with _Quiet():
		dcb.mute(60, asked=True)
		path = _write(0)
		dcb.check_external_mute()
		assert not dcb.is_muted()
		assert not os.path.exists(path)


def test_a_file_still_being_written_is_left_for_the_next_pass():
	with _Quiet():
		path = _write(int(time.time()) + 600, age=0)
		dcb.check_external_mute()
		assert not dcb.is_muted()
		assert os.path.exists(path)
		os.remove(path)


def test_anything_but_an_epoch_second_is_deleted_without_muting():
	with _Quiet():
		for content in ("not-a-number", "inf", "nan"):
			path = _write(content)
			dcb.check_external_mute()
			assert not dcb.is_muted(), content
			assert not os.path.exists(path), content


def test_an_absurd_time_is_capped_like_mute():
	with _Quiet():
		_write(int(time.time()) + 10 ** 12)
		dcb.check_external_mute()
		assert dcb.is_muted()
		assert dcb._mute_until() - time.time() <= dcb.MUTE_MAX_MINUTES * 60
