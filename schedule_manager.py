"""
The scheduled tasks: what a valid one holds, and the file they are kept in.

Every rule about a task lives here. The questions of /schedule check each
answer with clean(), the manager checks the whole task again before writing
it, and the scheduler only ever sees tasks that passed: none of them can let
through what another would refuse.

Nothing here knows about Telegram or Docker, so the core can build the
manager while it loads, and the tests can build one on a file of their own.
"""

import json
import os
import threading
from datetime import datetime

from croniter import croniter

import store
from config import (MUTE_MAX_MINUTES, SCHEDULE_ACTION_FIELDS, SCHEDULE_PRUNE_TYPES,
					SHOW_OUTPUT_FAILURES, SPECIAL_CRON_EXPRESSIONS)
from logger import error

# What a task holds besides its name, cron and action. The ones its action
# does not use are kept empty, so a task never carries leftovers of another.
DETAIL_FIELDS = ("host", "container", "minutes", "command", "prune_type", "show_output")

# What update() may change. Not the id, which the buttons carry.
CHANGEABLE = frozenset(("name", "cron", "action", "enabled") + DETAIL_FIELDS)

# Limits for what is typed into the bot: long enough for anything worth
# naming a task, short enough for the screens that show it to stay within one
# Telegram message. Not for a task already in the file, which ran before they
# existed and must not stop running over them.
TYPED_LIMITS = {"name": 64, "command": 1000, "minutes": MUTE_MAX_MINUTES}


class ScheduleError(ValueError):
	"""
	A value a task cannot hold.

	`key` is the text that tells the user why, and `field` the question to ask
	again, when there is one.
	"""

	def __init__(self, key, field=None):
		super().__init__(f"{field}: {key}" if field else key)
		self.key = key
		self.field = field


def fields_of(action):
	"""
	What a task with `action` holds besides its name, cron and action, in the
	order it is asked. A container brings its host, right after it: a name only
	means something on one machine.
	"""
	fields = ()
	for field in SCHEDULE_ACTION_FIELDS.get(action, ()):
		fields += ("container", "host") if field == "container" else (field,)
	return fields


def is_valid_cron(expression):
	"""
	Whether `expression` is a cron the scheduler can run: five fields, or one
	of the @ shortcuts.
	"""
	if expression in SPECIAL_CRON_EXPRESSIONS:
		# @reboot is the scheduler's own, and croniter does not know it.
		return expression == "@reboot" or croniter.is_valid(expression)
	# croniter also takes a sixth field, for seconds, which the scheduler
	# never looks at.
	if len(expression.split()) != 5:
		return False
	try:
		# Parsing is not enough: `0 0 30 2 *` parses, and fires on the 30th of
		# February. Asking for the next run is what tells a date that exists
		# from one that does not.
		croniter(expression).get_next(datetime)
		return True
	except Exception:
		return False


# Each cleaner takes the value and its limit, None for a task read from the
# file, and returns the value as a task holds it or raises ValueError.

def _text(value, limit):
	value = value.strip()
	if not value or (limit and len(value) > limit):
		raise ValueError(value)
	return value


def _one_of(options):
	def cleaner(value, _limit):
		if value not in options:
			raise ValueError(value)
		return value
	return cleaner


def _action(value, limit):
	# Lowercased: a hand-written "Restart" means what it says.
	return _one_of(SCHEDULE_ACTION_FIELDS)(value.strip().lower(), limit)


def _cron(value, _limit):
	value = " ".join(value.split())
	if not is_valid_cron(value):
		raise ValueError(value)
	return value


def _minutes(value, limit):
	if isinstance(value, bool):
		raise ValueError(value)
	minutes = int(value)
	if minutes <= 0 or (limit and minutes > limit):
		raise ValueError(value)
	# The ceiling of /mute even so: past it the timer that gives the bot its
	# voice back cannot be set.
	return min(minutes, MUTE_MAX_MINUTES)


def _show_output(value, _limit):
	# A task from before there was a third value holds a boolean, or nothing
	# at all, which meant no.
	if value == SHOW_OUTPUT_FAILURES:
		return value
	if value is None or isinstance(value, bool):
		return bool(value)
	raise ValueError(value)


_CLEANERS = {
	"name": _text,
	"cron": _cron,
	"action": _action,
	"host": _text,
	"container": _text,
	"minutes": _minutes,
	"command": _text,
	"prune_type": _one_of(SCHEDULE_PRUNE_TYPES),
	"show_output": _show_output,
}

# Why a value typed by hand was refused. The rest are picked from buttons, and
# a refused one is a button that no longer applies.
_TYPED_ERRORS = {
	"name": "schedule_invalid_name",
	"cron": "schedule_invalid_cron",
	"minutes": "schedule_invalid_minutes",
	"command": "schedule_invalid_command",
}


def clean(field, value, typed=False):
	"""
	`value` as a task holds it in `field`. Raises ScheduleError when it cannot.

	`typed` for an answer given in the bot, which is held to TYPED_LIMITS.
	"""
	try:
		return _CLEANERS[field](value, TYPED_LIMITS.get(field) if typed else None)
	except (AttributeError, TypeError, ValueError):
		raise ScheduleError(_TYPED_ERRORS.get(field, "error_invalid_selection"), field) from None


def check(task):
	"""
	`task` with every field cleaned, and emptied the ones its action does not
	use. Raises ScheduleError on the first field it cannot hold.

	Keys this version does not know are left as they are: a task written by a
	newer bot loses nothing by passing through this one.
	"""
	if not isinstance(task, dict):
		raise ScheduleError("error_invalid_selection")
	checked = dict(task)
	checked["action"] = clean("action", task.get("action"))
	checked["name"] = clean("name", task.get("name"))
	checked["cron"] = clean("cron", task.get("cron"))
	used = fields_of(checked["action"])
	for field in DETAIL_FIELDS:
		checked[field] = clean(field, task.get(field)) if field in used else None
	checked["enabled"] = bool(task.get("enabled", True))
	return checked


def _is_id(value):
	return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ScheduleManager:
	"""
	The tasks, held in memory and written through to their file.

	Every method holds the lock from start to end and hands out copies: the
	scheduler's thread and telebot's workers use this at the same time, and a
	caller holding the manager's own dict could change a task without it being
	written, or under another thread.

	A task is named by its id. Its name is what the user reads, and it can
	change under a button that still points at the task.
	"""

	def __init__(self, schedule_path, schedule_file="schedules.json"):
		self.full_path = os.path.join(schedule_path, schedule_file)
		self._lock = threading.RLock()
		self._tasks = []
		# What the file holds that is not a task this version can run. Written
		# back as it was: a typo in a hand-edited task must not cost the task.
		self._kept = []
		# Whatever else the document holds besides its tasks, for the same reason.
		self._document = {}
		self._next_id = 1
		# Where an unreadable file was moved, and what was left out of a
		# readable one, for the start-up message to say so.
		self.set_aside = None
		self.ignored = []
		os.makedirs(schedule_path, exist_ok=True)
		self._load()

	def _load(self):
		if not os.path.exists(self.full_path):
			return
		try:
			with open(self.full_path, encoding="utf-8") as handle:
				document = json.load(handle)
			if not isinstance(document, dict) or not isinstance(document.get("schedules", []), list):
				raise ValueError("not a schedules document")
		except Exception as e:
			# Starting empty is the only way to keep scheduling working, but the
			# next write would replace every task the user had with nothing. So
			# the file is moved aside first, where it can still be fixed.
			aside = f"{self.full_path}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
			try:
				os.replace(self.full_path, aside)
				self.set_aside = aside
				error(f"Cannot read {self.full_path} ({e}): moved to {aside}, starting with no schedules")
			except OSError as move_error:
				error(f"Cannot read {self.full_path} ({e}) nor move it aside: {move_error}")
			return

		entries = document.get("schedules", [])
		self._document = {key: value for key, value in document.items() if key not in ("schedules", "next_id")}
		# Kept in the file rather than worked out from the tasks there: deleting
		# the newest one and restarting gave its id to the next one made, and a
		# details screen of the deleted one still on screen acted on that.
		highest = max((entry["id"] for entry in entries if isinstance(entry, dict) and _is_id(entry.get("id"))),
						default=0)
		stored = document.get("next_id")
		self._next_id = max(highest + 1, stored if _is_id(stored) else 1)
		taken = set()
		for entry in entries:
			try:
				task = check(entry)
			except ScheduleError as e:
				self._kept.append(entry)
				label = entry.get("name") if isinstance(entry, dict) else None
				self.ignored.append(str(label or entry))
				error(f"Schedule {label or entry!r} left out, it cannot run as it is: {e}")
				continue
			# An id is what a button carries. One missing or repeated —by hand,
			# or by a copy and paste— gets a new one, not two tasks one press.
			if not _is_id(task.get("id")) or task["id"] in taken:
				task["id"] = self._next_id
				self._next_id += 1
			taken.add(task["id"])
			self._tasks.append(task)

	def _write(self, tasks, next_id=None):
		"""
		Makes `tasks` the tasks, once they are on disk.

		Through store, which writes a copy and renames it into place: a power
		cut at the wrong moment leaves the old file, not half of the new one.
		And only then kept in memory, so a write that fails raises and leaves
		both as they were, instead of a bot that believes in tasks no file holds.
		"""
		next_id = next_id or self._next_id
		store.write_document(self.full_path,
							dict(self._document, next_id=next_id, schedules=tasks + self._kept))
		self._tasks, self._next_id = tasks, next_id

	def _index(self, task_id):
		for index, task in enumerate(self._tasks):
			if task["id"] == task_id:
				return index
		return None

	def _refuse_taken_name(self, name, task_id=None):
		if self.name_taken(name, task_id):
			raise ScheduleError("schedule_name_exists", "name")

	def all(self):
		"""Every task, in the order they were added."""
		with self._lock:
			return [dict(task) for task in self._tasks]

	def get(self, task_id):
		"""One task, or None."""
		with self._lock:
			index = self._index(task_id)
			return None if index is None else dict(self._tasks[index])

	def name_taken(self, name, task_id=None):
		"""Whether a task other than `task_id` is called `name`."""
		with self._lock:
			return any(task["name"] == name and task["id"] != task_id for task in self._tasks)

	def on_host(self, host_id):
		"""The enabled tasks that act on `host_id`."""
		with self._lock:
			return [dict(task) for task in self._tasks if task["enabled"] and task["host"] == host_id]

	def add(self, fields):
		"""Adds a task, enabled, and returns it. Raises ScheduleError, or OSError when it cannot be written."""
		with self._lock:
			task = check(dict(fields, enabled=True))
			self._refuse_taken_name(task["name"])
			task["id"] = self._next_id
			task["created_at"] = datetime.now().isoformat(timespec="seconds")
			self._write(self._tasks + [task], next_id=task["id"] + 1)
			return dict(task)

	def update(self, task_id, **changes):
		"""
		Changes a task and returns it, or None when there is no such task.
		Raises ScheduleError, or OSError when it cannot be written.
		"""
		if not CHANGEABLE.issuperset(changes):
			raise ValueError(f"Not a field a task can change: {sorted(set(changes) - CHANGEABLE)}")
		with self._lock:
			index = self._index(task_id)
			if index is None:
				return None
			task = check(dict(self._tasks[index], **changes))
			# Only when the name changes: two tasks of the same name, written
			# by hand, could not otherwise be changed at all —not even switched
			# off, by the scheduler or by removing their host.
			if task["name"] != self._tasks[index]["name"]:
				self._refuse_taken_name(task["name"], task_id)
			tasks = list(self._tasks)
			tasks[index] = task
			self._write(tasks)
			return dict(task)

	def delete(self, task_id):
		"""Deletes a task and returns it, or None when there was no such task."""
		with self._lock:
			index = self._index(task_id)
			if index is None:
				return None
			tasks = list(self._tasks)
			removed = tasks.pop(index)
			self._write(tasks)
			return dict(removed)
