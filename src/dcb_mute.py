#!/usr/bin/env python3
"""
Mutes the running bot from a script, without going through Telegram.

	docker exec docker-controller-bot dcb-mute 60    mutes for 60 minutes
	docker exec docker-controller-bot dcb-mute 0     unmutes

It writes the epoch second the mute ends at into EXTERNAL_MUTE_FILE, the same
file 4.x read and a script can still write by hand, and waits for the bot to
pick it up, so a script knows from the exit code whether it is muted.
"""

import os
import sys
import tempfile
import time

import store
from config import EXTERNAL_MUTE_FILE, MUTE_MAX_MINUTES

# The bot looks every 2 s and leaves a file younger than 1 s for the next pass.
WAIT_SECONDS = 10

USAGE = f"usage: dcb-mute <minutes>   (0 unmutes, at most {MUTE_MAX_MINUTES})"


def main(argv):
	if len(argv) != 1:
		print(USAGE, file=sys.stderr)
		return 2
	try:
		minutes = int(argv[0])
	except ValueError:
		minutes = -1
	if not 0 <= minutes <= MUTE_MAX_MINUTES:
		print(f"{argv[0]!r} is not a number of minutes\n{USAGE}", file=sys.stderr)
		return 2

	# Resolved the way the bot does, without init(): that one logs, and a
	# 4.x volume would say so on every call.
	root = store._resolve_root()[0]
	path = os.path.join(root, EXTERNAL_MUTE_FILE)
	until = int(time.time()) + minutes * 60 if minutes else 0
	# Written next to it and moved into place, so the bot never reads half.
	try:
		handle, temporary = tempfile.mkstemp(dir=root, prefix=".dcb-mute-")
		with os.fdopen(handle, "w", encoding="utf-8") as stream:
			stream.write(f"{until}\n")
		os.replace(temporary, path)
	except OSError as e:
		print(f"Could not write {path}: {e}", file=sys.stderr)
		return 1

	deadline = time.time() + WAIT_SECONDS
	while os.path.exists(path):
		if time.time() > deadline:
			print(f"The bot has not picked up {path} in {WAIT_SECONDS} s; it will when it can.", file=sys.stderr)
			return 1
		time.sleep(0.5)

	print(f"Muted for {minutes} minute{'' if minutes == 1 else 's'}." if minutes else "Unmuted.")
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
