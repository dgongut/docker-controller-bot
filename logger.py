"""Simple stdout logger used across the bot modules."""

import os
import sys
from datetime import datetime


def _hide_token(message):
	"""
	The line with the bot's token masked, leaving the bot id in front.

	Here and not where the errors are logged: telebot puts the token in the
	path of every request (/bot123:AAH…/sendMessage), and a ConnectionError
	from requests quotes the URL whole, so every failed send while the
	Internet was down wrote it out. From there it reached /logs, /logfile and
	any log pasted into an issue. One place every line goes through covers the
	call sites there are and the ones still to come.

	Read from the environment, which is where config takes it from, rather
	than imported: every module of the bot imports the logger, and the logger
	depends on none of them.
	"""
	token = os.environ.get("TELEGRAM_TOKEN")
	if not token or token not in message:
		return message
	# Without a ':' there is no id to keep, and what comes before one would be
	# the whole token.
	bot_id, separator, _ = token.partition(":")
	return message.replace(token, f"{bot_id}:***" if separator else "***")


def _log(level, message):
	# One write per line, newline included. print() writes the text and the
	# newline separately, and two threads logging at once —the event monitor
	# and the update daemon starting, say— came out as one line glued to the
	# next and an empty one after them.
	sys.stdout.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - {level}: {_hide_token(str(message))}\n')


def debug(message):
	_log("DEBUG", message)


def error(message):
	_log("ERROR", message)


def warning(message):
	_log("WARNING", message)
