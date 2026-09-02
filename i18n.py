"""
Translations.

Nothing in here depends on the rest of the bot, only on the configured
language, so it is the one piece that can be read and changed in isolation.
Every string the user sees goes through get_text.
"""

import json
import os

import store
from config import SUPPORTED_LANGUAGES
from logger import error, warning

# Cache for locale files to avoid repeated file I/O
_locale_cache = {}

def language():
	"""
	Locale in use, lowercased.

	Read on every call rather than captured once, so changing the language from
	/settings takes effect without restarting the container. The locale files
	themselves stay cached, so this costs a dictionary lookup.
	"""
	configured = str(store.get("bot.language") or "ES").lower()
	if configured not in [supported.lower() for supported in SUPPORTED_LANGUAGES]:
		warning(f"Unsupported language {configured}, falling back to ES")
		return "es"
	return configured

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

	# Replace placeholders efficiently
	if args:
		for i, arg in enumerate(args, start=1):
			translated_text = translated_text.replace(f"${i}", str(arg))

	return translated_text
