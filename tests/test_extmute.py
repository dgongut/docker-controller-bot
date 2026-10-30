"""
Muting the bot from the command line through .muted_until.

A script runs `docker exec <bot> sh -c "echo <epoch> > /app/config/.muted_until"`
while the bot is up; each pass of the watcher applies the file and deletes it.
"""

import contextlib
import io
import os
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

dcb, store, _root = harness.load_bot()
import dcb_mute  # noqa: E402  (after the harness, which puts src/ on the path)


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


@contextlib.contextmanager
def _watched():
	"""The bot's watcher running, without the settle time, while dcb-mute waits."""
	settle = dcb.EXTERNAL_MUTE_SETTLE_SECONDS
	dcb.EXTERNAL_MUTE_SETTLE_SECONDS = 0
	stop = threading.Event()

	def watch():
		while not stop.is_set():
			dcb.check_external_mute()
			time.sleep(0.05)

	thread = threading.Thread(target=watch, daemon=True)
	thread.start()
	try:
		yield
	finally:
		stop.set()
		thread.join()
		dcb.EXTERNAL_MUTE_SETTLE_SECONDS = settle


def _dcb_mute(*argv):
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		return dcb_mute.main(list(argv))


def test_dcb_mute_mutes_through_the_bot():
	with _Quiet(), _watched():
		assert _dcb_mute("30") == 0
		assert dcb.is_muted()
		assert 29 * 60 <= dcb._mute_until() - time.time() <= 30 * 60
		assert not os.path.exists(os.path.join(store.root(), dcb.EXTERNAL_MUTE_FILE))


def test_dcb_mute_0_unmutes():
	with _Quiet(), _watched():
		dcb.mute(60, asked=True)
		assert _dcb_mute("0") == 0
		assert not dcb.is_muted()


def test_dcb_mute_rejects_anything_but_minutes_and_writes_nothing():
	path = os.path.join(store.root(), dcb.EXTERNAL_MUTE_FILE)
	for argv in ((), ("x",), ("-1",), ("1.5",), (str(dcb.MUTE_MAX_MINUTES + 1),), ("1", "2")):
		assert _dcb_mute(*argv) == 2, argv
		assert not os.path.exists(path), argv


def test_dcb_mute_fails_when_the_bot_does_not_pick_it_up():
	"""A script has to know it is not muted, and the file stays for later."""
	wait = dcb_mute.WAIT_SECONDS
	dcb_mute.WAIT_SECONDS = 0.2
	try:
		assert _dcb_mute("30") == 1
		path = os.path.join(store.root(), dcb.EXTERNAL_MUTE_FILE)
		assert os.path.exists(path)
		os.remove(path)
	finally:
		dcb_mute.WAIT_SECONDS = wait
	assert not [n for n in os.listdir(store.root()) if n.startswith(".dcb-mute-")]
