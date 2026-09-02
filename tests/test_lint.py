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
			"docker-controller-bot.py")


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
