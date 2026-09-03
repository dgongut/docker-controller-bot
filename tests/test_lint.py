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


# Callbacks whose argument is a container. Their buttons have to carry a
# reference, not a bare short id: five hex characters name a different
# container on another host, so a bare id resolves against the local machine
# and the press fails with "container does not exist".
CONTAINER_CALLBACKS = (
	"run", "stop", "restart", "confirmDelete", "delete", "logs", "logfile",
	"info", "compose", "checkUpdate", "confirmUpdate", "update",
	"changeTagContainer", "changeTag", "askCommand", "exec", "cancelExec",
	"toggleUpdate",
)

# How a reference is built. Anything interpolated into a container callback
# has to come from one of these, or already be a reference.
REFERENCE_BUILDERS = ("container_ref(", "make_ref(", "_ref(")

# How a bare short id gets built. Five hex characters off a container object
# name a container on the local host and nowhere else, so a function that
# builds container-callback buttons must not do this at all.
BARE_ID_BUILDERS = (".id[:CONTAINER_ID_LENGTH]",)


def _function_spans(tree):
	"""Every function in a file as (first line, last line, name).

	Innermost first, so a nested function wins over the one holding it.
	"""
	spans = [(node.lineno, node.end_lineno, node.name)
			for node in ast.walk(tree)
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
	spans.sort(key=lambda span: span[1] - span[0])
	return spans


def _function_around(spans, line_number):
	"""The innermost function holding a line, or None at module level."""
	for span in spans:
		if span[0] <= line_number <= span[1]:
			return span
	return None


def test_container_buttons_never_carry_a_bare_id():
	"""
	The mistake this catches shipped three times: a button built with
	`container.id[:CONTAINER_ID_LENGTH]` works on the local host and fails on
	every other one, which is invisible until somebody presses it.
	"""
	import re

	problems = []
	# Both quote styles. The repo mixes them, and the version of this check
	# that only read double quotes was blind to eight `callback_data` lines —
	# two of them container buttons, which is how /updateall shipped bare ids.
	pattern = re.compile(r"""callback_data=f?(['"])([^'"]*)\1""")
	for filename in ("core.py", "commands.py", "callbacks.py"):
		path = os.path.join(harness.REPO, filename)
		source = io.open(path, encoding="utf-8").read()
		lines = source.splitlines()
		spans = _function_spans(ast.parse(source))
		for line_number, line in enumerate(lines, start=1):
			for _quote, data in pattern.findall(line):
				name = data.split("|", 1)[0]
				if name not in CONTAINER_CALLBACKS:
					continue
				if "{" not in data:
					continue
				if any(builder in line for builder in REFERENCE_BUILDERS):
					continue
				if re.search(r"\{(containerId|cid|container_id|ref)\}", data):
					# The name says reference and usually is one: it came from
					# a callback the dispatcher resolved. But a name proves
					# nothing on its own, so the function that builds the
					# button has to be clean of bare ids for this to hold.
					span = _function_around(spans, line_number)
					if span is None:
						continue
					body = "\n".join(lines[span[0] - 1:span[1]])
					culprit = next((builder for builder in BARE_ID_BUILDERS
									if builder in body), None)
					if culprit is None:
						continue
					problems.append(
						f"  {filename}:{line_number}  {span[2]}() builds "
						f"`{culprit}` and hands it to a container button:\n"
						f"      {line.strip()}")
				else:
					problems.append(f"  {filename}:{line_number}  {line.strip()}")

	assert not problems, (
		"botones de contenedor con id suelto en vez de referencia:\n" + "\n".join(problems))


# The calls that hand back a message object, and therefore hand back None when
# Telegram is saturated or the network drops. Nothing else the bot sends waits
# for a result.
MESSAGE_SENDERS = ("send_message", "send_message_to_notification_channel")


def _sends_a_message(node):
	"""Whether an expression is a call to something that returns a message."""
	if not isinstance(node, ast.Call):
		return False
	target = node.func
	if isinstance(target, ast.Attribute):
		return target.attr in MESSAGE_SENDERS
	return isinstance(target, ast.Name) and target.id in MESSAGE_SENDERS


def _parents(tree):
	"""Each node's parent, so a check can be looked for above a use."""
	table = {}
	for node in ast.walk(tree):
		for child in ast.iter_child_nodes(node):
			table[child] = node
	return table


def _names_in(node):
	return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _is_guarded(node, name, parents):
	"""
	Whether a use of `name` sits behind a check that it is not None.

	Walks up from the use, so all three shapes the bot writes count: the
	`if x:` block the commands use, the `x.message_id if x else None` of the
	schedule flow, and `x and x.message_id`.
	"""
	child = node
	parent = parents.get(child)
	while parent is not None:
		if isinstance(parent, ast.If) and child in parent.body:
			if name in _names_in(parent.test):
				return True
		elif isinstance(parent, ast.IfExp) and child is parent.body:
			if name in _names_in(parent.test):
				return True
		elif isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
			earlier = parent.values[:parent.values.index(child)] if child in parent.values else []
			if any(name in _names_in(value) for value in earlier):
				return True
		child, parent = parent, parents.get(parent)
	return False


def test_a_message_that_was_never_sent_is_not_dereferenced():
	"""
	send_message returns None when the send fails, and reading .message_id off
	it raises inside whatever was running. That is how a scheduled prune with
	output could lose its whole execution to a moment of Telegram saturation:
	the guard the manual commands had was missing there.
	"""
	problems = []
	for filename in ("core.py", "commands.py", "callbacks.py"):
		path = os.path.join(harness.REPO, filename)
		tree = ast.parse(io.open(path, encoding="utf-8").read())
		parents = _parents(tree)
		for function in ast.walk(tree):
			if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue

			# Names in this function that hold the result of a send.
			held = set()
			for node in ast.walk(function):
				if isinstance(node, ast.Assign) and _sends_a_message(node.value):
					for target in node.targets:
						if isinstance(target, ast.Name):
							held.add(target.id)
			if not held:
				continue

			for node in ast.walk(function):
				if not isinstance(node, ast.Attribute) or node.attr != "message_id":
					continue
				if not isinstance(node.value, ast.Name) or node.value.id not in held:
					continue
				if _is_guarded(node, node.value.id, parents):
					continue
				problems.append(
					f"  {filename}:{node.lineno}  {function.name}() lee "
					f"{node.value.id}.message_id sin comprobar que se envió")

	assert not problems, (
		"mensajes sin enviar usados como si existieran:\n" + "\n".join(problems))


# Locale keys whose text names a container, a project or one of its services.
# A message built from one of these is about a single machine, and the name
# alone does not say which: two hosts can run the same container or the same
# stack. So it has to carry host_label(), which is empty with a single host and
# therefore costs nothing there.
#
# Progress messages with no identity of their own ("loading_file",
# "fetching_image_data") are not on the list: they say nothing about what they
# are working on and are deleted a moment later.
HOST_SCOPED_TEXTS = (
	"stopped_container", "restarted_container", "started_container",
	"error_pulling_image", "container_does_not_exist", "confirm_delete",
	"prompt_enter_command", "confirm_exec", "executed_command",
	"confirm_change_tag", "change_tag", "error_changing_tag",
	"error_getting_tags", "confirm_update", "logs", "compose", "deleting",
	"error_project_not_found", "restarting_project", "starting_project",
	"stopping_project", "project_restarted_success", "project_started_success",
	"project_stopped_success", "deleting_project", "project_deleted_success",
	"deleting_service", "error_deleting_service", "stopping_service",
	"starting_service", "error_stopping_service", "error_starting_service",
)

# What counts as saying it. The label is often computed once at the top of a
# function that sends several of these, so the variable holding it counts too.
HOST_LABELLERS = ("host_label", "{label}", "host_alias")

# Where a message reaches the user.
DELIVERY = ("send_message", "send_message_to_notification_channel",
			"send_document", "edit_message_text")

# The one place with no host to name. A project button carries a short hash of
# the project name to fit Telegram's 64 bytes, and the hash resolves to the
# host as well; when it resolves to nothing, neither the project nor the
# machine is known, and the message names the hash for lack of anything better.
NO_HOST_TO_NAME = {("core.py", "button_controller", "error_project_not_found")}


def test_a_message_about_one_host_says_which():
	"""
	The mistake behind three of the reported bugs: a message naming a container
	or a project, sent from an operation that knew the host and did not mention
	it. With several machines configured the user cannot tell which one
	answered, and the names are not unique across them.
	"""
	import re

	problems = []
	key_pattern = re.compile(r"""get_text\(\s*['"]([a-z_]+)['"]""")
	for filename in ("core.py", "commands.py", "callbacks.py"):
		path = os.path.join(harness.REPO, filename)
		source = io.open(path, encoding="utf-8").read()
		tree = ast.parse(source)
		spans = _function_spans(tree)
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue
			target = node.func
			name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
			if name not in DELIVERY:
				continue
			segment = ast.get_source_segment(source, node) or ""
			keys = set(key_pattern.findall(segment)) & set(HOST_SCOPED_TEXTS)
			if not keys:
				continue
			if any(labeller in segment for labeller in HOST_LABELLERS):
				continue
			span = _function_around(spans, node.lineno)
			sender = span[2] if span else "<module>"
			keys -= {key for key in keys if (filename, sender, key) in NO_HOST_TO_NAME}
			if not keys:
				continue
			problems.append(
				f"  {filename}:{node.lineno}  {sender}() con "
				f"{sorted(keys)} y sin host_label")

	assert not problems, (
		"mensajes de una sola máquina que no dicen cuál:\n" + "\n".join(problems))


# Modules that hold a lock of their own, and what each may depend on. The order
# is what matters: core takes _managers_lock and then calls into the registry,
# which takes _lock. For that to deadlock the registry would have to reach back
# into core, so the rule is stated as a dependency rather than as an ordering —
# a dependency is something a static check can actually see.
LOCK_HOLDERS = {
	"host_registry.py": {"store", "logger", "docker", "paramiko", "shutil",
							"threading", "time", "uuid", "json", "os"},
}


def test_the_module_that_locks_last_depends_on_nothing_above_it():
	"""
	core.manager() calls host_registry.client() while holding _managers_lock,
	and client() takes the registry's own lock. That is core → host_registry
	and it is fine in one direction only: an import of core from the registry
	would open the door to taking the two in the opposite order, which is a
	deadlock nobody would reproduce on demand.
	"""
	problems = []
	for filename, allowed in LOCK_HOLDERS.items():
		path = os.path.join(harness.REPO, filename)
		tree = ast.parse(io.open(path, encoding="utf-8").read())
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				names = [alias.name.split(".")[0] for alias in node.names]
			elif isinstance(node, ast.ImportFrom):
				names = [(node.module or "").split(".")[0]]
			else:
				continue
			for name in names:
				if name and name not in allowed:
					problems.append(f"  {filename}:{node.lineno} importa {name}")

	assert not problems, (
		"un módulo con lock propio depende de otro que se toma antes:\n"
		+ "\n".join(problems))


def test_nothing_the_bot_starts_outlives_it():
	"""
	A non-daemon thread or timer keeps the interpreter from exiting: it waits
	to join it. The two `/mute` timers were the only ones in the project
	missing the flag, so muting for an hour left the container unable to shut
	down until Docker gave up and sent SIGKILL — taking whatever the message
	queue still had in flight with it.
	"""
	problems = []
	for filename in SOURCES:
		path = os.path.join(harness.REPO, filename)
		source = io.open(path, encoding="utf-8").read()
		tree = ast.parse(source)
		spans = _function_spans(tree)
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue
			target = node.func
			name = ast.unparse(target) if isinstance(target, ast.Attribute) else getattr(target, "id", "")
			if name not in ("threading.Thread", "threading.Timer"):
				continue
			# Either passed as an argument, or set on the object right after.
			if any(kw.arg == "daemon" for kw in node.keywords):
				continue
			span = _function_around(spans, node.lineno)
			body = source.splitlines()[node.lineno - 1:(span[1] if span else node.lineno + 4)]
			if any(".daemon = True" in line for line in body[:5]):
				continue
			problems.append(f"  {filename}:{node.lineno}  {name} sin daemon")

	assert not problems, (
		"hilos que impedirían al proceso salir:\n" + "\n".join(problems))
