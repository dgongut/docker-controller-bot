#!/usr/bin/env python3
"""
Runs every test. No test framework: the bot has no dev dependencies and these
only need the ones it already ships with.

	python3 tests/run_all.py            todo
	python3 tests/run_all.py bot        solo los módulos que casen con "bot"

Each test file is a module of `test_*` functions that assert. A failure prints
the assertion and the run ends non-zero.
"""

import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# test_store and test_migration expect a clean import; test_bot loads the bot
# module, which is global state, so it goes last.
MODULES = ("test_lint", "test_store", "test_migration", "test_hosts", "test_bot", "test_monitors")


def load(name):
	spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


def run(selector=None):
	sys.path.insert(0, REPO)
	sys.path.insert(0, HERE)
	# The tests chdir into temporary directories; come back afterwards.
	original_cwd = os.getcwd()

	passed = failed = 0
	failures = []
	try:
		for name in MODULES:
			if selector and selector not in name:
				continue
			print(f"\n\033[1m{name}\033[0m")
			module = load(name)
			for attribute in sorted(vars(module)):
				if not attribute.startswith("test_"):
					continue
				test = getattr(module, attribute)
				if not callable(test):
					continue
				try:
					test()
				except Exception:
					failed += 1
					failures.append((name, attribute, traceback.format_exc()))
					print(f"  \033[31mFALLA\033[0m  {attribute}")
				else:
					passed += 1
					print(f"  \033[32mok\033[0m     {attribute}")
	finally:
		os.chdir(original_cwd)

	print()
	for name, attribute, trace in failures:
		print(f"\033[31m{'=' * 60}\n{name}.{attribute}\n{'=' * 60}\033[0m")
		print(trace)

	total = passed + failed
	colour = "\033[31m" if failed else "\033[32m"
	print(f"{colour}{passed}/{total} tests OK\033[0m")
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
