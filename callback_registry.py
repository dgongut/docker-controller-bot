"""
Registry of inline-button callbacks.

Each handler declares, right next to itself, what the dispatcher needs to know
about it: the arguments its `callback_data` carries, whether pressing it keeps
the message it came from, and whether its argument is a hashed project name.

Those facts used to live in four separate dictionaries in another file, which
is how a handler could be written complete and correct and still not work: the
dispatcher read a list nobody remembered to update, two thousand lines away.
The failure showed up as "the buttons do nothing", which points nowhere near
the cause. Declaring them here makes that mistake hard to make, because the
declaration is on the line you are already writing.
"""

import threading


class CallbackSpec:
	"""One registered callback: its handler and everything the dispatcher needs."""

	__slots__ = ("name", "handler", "params", "keeps_message", "project_arg", "multi_action", "answer_immediately")

	def __init__(self, name, handler, params, keeps_message, project_arg, multi_action, answer_immediately):
		self.name = name
		self.handler = handler
		self.params = tuple(params)
		# The dispatcher deletes the message a press came from unless this is
		# set, which is what any callback that repaints in place needs.
		self.keeps_message = keeps_message
		# The single argument carries a short hash of a project name rather
		# than the name itself, to stay under Telegram's 64-byte limit.
		self.project_arg = project_arg
		# Belongs to a menu left open for multi-selection.
		self.multi_action = multi_action
		# Answered before running, to stop Telegram's spinner. A handler that
		# gives its own feedback in the answer sets this to False.
		self.answer_immediately = answer_immediately

	def __repr__(self):
		return f"<CallbackSpec {self.name} params={self.params}>"


class Context:
	"""
	Everything a handler may need about the press that reached it.

	One shape for every handler, so the dispatcher does not have to know which
	fields any particular one reads.
	"""

	__slots__ = (
		"call", "comando", "messageId", "chatId", "userId",
		# Which Docker host this press is about, resolved by the dispatcher
		# from the container reference or the project hash the button carries.
		"hostId",
		"containerId", "containerName", "tag", "action", "containerIdx",
		"originalMessageId", "commandId", "scheduleHash", "field",
		"scheduleId", "value", "pruneType", "multiAction",
	)

	def __init__(self, **fields):
		for name in self.__slots__:
			setattr(self, name, fields.get(name))

	def __repr__(self):
		return f"<Context {self.comando} chat={self.chatId} msg={self.messageId}>"


_lock = threading.Lock()
_registry = {}


def callback(name, params=(), keeps_message=False, project_arg=False,
			multi_action=False, answer_immediately=True):
	"""
	Registers the decorated function as the handler for `name`.

	Raises on a duplicate name: two handlers for one callback would mean the
	button silently does whichever was defined last.
	"""
	def decorator(handler):
		spec = CallbackSpec(
			name=name,
			handler=handler,
			params=params,
			keeps_message=keeps_message,
			project_arg=project_arg,
			multi_action=multi_action,
			answer_immediately=answer_immediately,
		)
		with _lock:
			if name in _registry:
				raise ValueError(f"Callback {name} is already registered")
			_registry[name] = spec
		return handler
	return decorator


def register(name, handler, **options):
	"""
	Registers a handler without using the decorator syntax.

	Used by the factories that generate whole families of callbacks, where the
	handler is built rather than written out.
	"""
	return callback(name, **options)(handler)


def get(name):
	"""The spec for `name`, or None when nothing is registered under it."""
	return _registry.get(name)


def names():
	"""Every registered callback name."""
	return set(_registry)


def specs():
	"""Every registered spec, keyed by name."""
	return dict(_registry)


def parse(call_data):
	"""
	Splits raw `callback_data` into its name and arguments.

	Returns (spec, {param: value}). Raises ValueError when the callback is not
	registered or carries the wrong number of arguments, which is what a stale
	button from an older version of the bot looks like.
	"""
	parts = call_data.split("|")
	name = parts[0]
	args = parts[1:]

	spec = _registry.get(name)
	if spec is None:
		raise ValueError(f"COMMAND NOT IN PATTERN: {name}")
	if len(args) != len(spec.params):
		raise ValueError(
			f"INCORRECT LENGTH CALLBACK DATA FOR '{name}': IT WAS EXPECTED {len(spec.params)}"
		)
	return spec, dict(zip(spec.params, args))


def reset():
	"""Empties the registry. For tests only."""
	with _lock:
		_registry.clear()
