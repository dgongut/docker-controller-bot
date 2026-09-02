"""
Static checks over the source, for the mistakes that only show up at runtime.

A function that reads a name the module does not define compiles fine and fails
the day someone presses the button that reaches it. That is exactly what a
mechanical refactor risks leaving behind, so it is worth checking every time
rather than hoping.
"""

import ast
import builtins
import importlib
import io
import os
import symtable
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

# Every module of the bot, and what each may legitimately read from elsewhere.
SOURCES = ("core.py", "commands.py", "callbacks.py", "i18n.py",
			"store.py", "migration.py", "callback_registry.py",
			"host_registry.py", "docker-controller-bot.py")


def _module_globals(tree, extra):
	"""Names a module defines at its top level, imports included."""
	names = set(dir(builtins)) | set(extra)

	def collect(body):
		for node in body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
				names.add(node.name)
			elif isinstance(node, ast.Assign):
				for target in node.targets:
					for sub in ast.walk(target):
						if isinstance(sub, ast.Name):
							names.add(sub.id)
			elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
				for sub in ast.walk(node.target):
					if isinstance(sub, ast.Name):
						names.add(sub.id)
			elif isinstance(node, (ast.Import, ast.ImportFrom)):
				for alias in node.names:
					if alias.name == "*":
						# `from config import *`: everything public in it.
						module = importlib.import_module(node.module)
						names.update(n for n in dir(module) if not n.startswith("_"))
					else:
						names.add(alias.asname or alias.name.split(".")[0])
			elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
				collect(node.body)
				collect(getattr(node, "orelse", []))
				collect(getattr(node, "finalbody", []))
				for handler in getattr(node, "handlers", []):
					collect(handler.body)

	collect(tree.body)
	return names


def test_no_function_reads_a_name_its_module_does_not_define():
	sys.path.insert(0, harness.REPO)
	problems = []

	for filename in SOURCES:
		path = os.path.join(harness.REPO, filename)
		source = io.open(path, encoding="utf-8").read()
		known = _module_globals(ast.parse(source), extra=())

		def walk(table):
			for child in table.get_children():
				walk(child)
			if table.get_type() != "function":
				return
			for symbol in table.get_symbols():
				if symbol.is_global() and not symbol.is_assigned():
					if symbol.get_name() not in known:
						problems.append((filename, table.get_name(), symbol.get_name()))

		walk(symtable.symtable(source, filename, "exec"))

	assert not problems, "referencias a nombres inexistentes:\n" + "\n".join(
		f"  {f}: {func} lee {name}" for f, func, name in sorted(set(problems)))


def test_the_entry_point_imports_everything_that_registers():
	"""
	Commands and callbacks register by being imported. If the entry point stops
	importing one of them the bot starts fine and half its buttons do nothing.
	"""
	path = os.path.join(harness.REPO, "docker-controller-bot.py")
	source = io.open(path, encoding="utf-8").read()
	for module in ("core", "commands", "callbacks"):
		assert f"import {module}" in source, f"el punto de entrada no importa {module}"


def test_the_registrars_are_the_ones_the_tests_load():
	"""The harness has to load what the entry point loads, or it tests less."""
	path = os.path.join(harness.REPO, "docker-controller-bot.py")
	source = io.open(path, encoding="utf-8").read()
	imported = {line.split()[1] for line in source.split("\n")
				if line.startswith("import ") and not line.startswith("import core")}
	assert set(harness.REGISTRARS) == imported, (harness.REGISTRARS, imported)

# Characters that are emoji but default to a text presentation: without U+FE0F
# after them, platforms are free to draw them as a monochrome glyph, and some
# draw nothing at all. Every emoji the project already used carried the
# selector, so this keeps new strings to that convention.
TEXT_DEFAULT_EMOJI = {
	"\u23f1": "stopwatch",
	"\u23f9": "stop button",
	"\u23fa": "record button",
	"\u2b06": "up arrow",
	"\u2b07": "down arrow",
	"\u2699": "gear",
	"\u2139": "information",
	"\u2764": "heart",
	"\u26a0": "warning",
	"\u2714": "check mark",
	"\u2716": "multiplication",
	"\u2712": "pen",
	"\U0001f5d1": "wastebasket",
	"\U0001f3f7": "label",
	"\U0001f5a5": "desktop computer",
	"\U0001f5b1": "mouse",
	"\U0001f5a8": "printer",
	"\U0001f570": "mantelpiece clock",
	"\U0001f579": "joystick",
	"\u2328": "keyboard",
}

# Characters with no emoji presentation at all in common fonts. No selector
# saves these: they have to be replaced with something else that means the
# same thing. \ud83d\udda7 is the one that caught us out, showing as an empty box in
# Telegram while looking perfectly fine in the editor.
NO_EMOJI_FORM = {
	"\U0001f5a7": "networked computers",
}


def _locale_strings():
	import glob
	import json

	for path in sorted(glob.glob(os.path.join(harness.REPO, "locale", "*.json"))):
		with io.open(path, encoding="utf-8") as handle:
			for key, value in json.load(handle).items():
				if isinstance(value, str):
					yield os.path.basename(path), key, value


def test_emoji_carry_their_variation_selector():
	"""
	An emoji drawn as a monochrome glyph — or as an empty box — is invisible in
	the interface and impossible to spot from the source, since the character
	is right there and looks fine in an editor.
	"""
	problems = []
	for filename, key, value in _locale_strings():
		for char, name in TEXT_DEFAULT_EMOJI.items():
			index = value.find(char)
			while index != -1:
				following = value[index + len(char):index + len(char) + 1]
				if following != "\ufe0f":
					problems.append(f"  {filename}: {key} usa {name} sin U+FE0F")
					break
				index = value.find(char, index + 1)

	assert not problems, "emoji sin selector de variación:\n" + "\n".join(sorted(set(problems)))


def test_no_string_uses_a_character_with_no_emoji_form():
	"""These render as a box on most platforms, selector or not."""
	problems = []
	for filename, key, value in _locale_strings():
		for char, name in NO_EMOJI_FORM.items():
			if char in value:
				problems.append(f"  {filename}: {key} usa {name}")

	assert not problems, "caracteres sin forma emoji:\n" + "\n".join(sorted(set(problems)))
