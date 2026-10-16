"""Simple stdout logger used across the bot modules."""

import sys
from datetime import datetime


def _log(level, message):
	# One write per line, newline included. print() writes the text and the
	# newline separately, and two threads logging at once —the event monitor
	# and the update daemon starting, say— came out as one line glued to the
	# next and an empty one after them.
	sys.stdout.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - {level}: {message}\n')


def debug(message):
	_log("DEBUG", message)


def error(message):
	_log("ERROR", message)


def warning(message):
	_log("WARNING", message)
