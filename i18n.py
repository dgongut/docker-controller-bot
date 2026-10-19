"""
Translations.

Nothing in here depends on the rest of the bot, only on the configured
language, so it is the one piece that can be read and changed in isolation.
Every string the user sees goes through get_text.
"""

import json
import os
import re

import store
from config import SUPPORTED_LANGUAGES
from logger import error, warning

# Cache for locale files to avoid repeated file I/O
_locale_cache = {}

# A placeholder in a translation: $1, $2... All the digits at once, so $1 is
# never read as the start of $10.
_PLACEHOLDER = re.compile(r"\$(\d+)")

def language():
	"""
	Locale in use, lowercased.

	Read on every call rather than captured once, so changing the language from
	/settings takes effect without restarting the container. The locale files
	themselves stay cached, so this costs a dictionary lookup.
	"""
	configured = str(store.get("bot.language") or "ES").lower()
	if configured not in [supported.lower() for supported in SUPPORTED_LANGUAGES]:
		# Once per value: this runs on every text the bot renders, hundreds a
		# screen, and a hand-edited language would bury the log.
		if configured not in _warned_languages:
			_warned_languages.add(configured)
			warning(f"Unsupported language {configured}, falling back to ES")
		return "es"
	return configured

_warned_languages = set()

# Resolved from this file's own location rather than hardcoded to /app, so the
# bot also runs straight from a checkout.
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")

def load_locale(locale):
	"""Load locale with caching to avoid repeated file I/O"""
	if locale not in _locale_cache:
		with open(os.path.join(LOCALE_DIR, f"{locale}.json"), "r", encoding="utf-8") as file:
			_locale_cache[locale] = json.load(file)
	return _locale_cache[locale]

def get_text(key, *args):
	"""Get translated text with caching"""
	locale = language()
	messages = load_locale(locale)
	if key in messages:
		translated_text = messages[key]
	else:
		messages_en = load_locale("en")
		if key in messages_en:
			warning(f"key ['{key}'] is not in locale {locale}")
			translated_text = messages_en[key]
		else:
			error(f"key ['{key}'] is not in locale {locale} or EN")
			return f"key ['{key}'] is not in locale {locale} or EN"

	# In one pass over the translation, not one replace per argument: those went
	# on to replace inside what was already in, so a `$3` in a command run with
	# /exec came out as the whole output of it. Each placeholder of the text is
	# filled once, what goes in is never read again, and one with no argument
	# is left as it is.
	if args:
		def fill(match):
			index = int(match.group(1))
			return str(args[index - 1]) if 0 < index <= len(args) else match.group(0)
		translated_text = _PLACEHOLDER.sub(fill, translated_text)

	return translated_text
