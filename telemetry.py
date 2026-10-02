"""
Anonymous usage statistics client.

Reference client for https://github.com/dgongut/telemetry. It is one file with
no dependencies beyond the standard library, meant to be copied into a
project as it is: a package would be one more dependency to keep up to date
for a hundred lines.

	stats = Telemetry(
		project="my-project",
		version="1.2.0",
		state_path="/app/config/state/telemetry.json",
		metrics=lambda: {"hosts": 3, "language": "es"},
		enabled=lambda: settings.get("telemetry"),
	)
	stats.start()
	...
	stats.count("cmd_list")

What it guarantees:

	- Nothing is sent, and no id is created, while `enabled()` is False or the
	  TELEMETRY environment variable is set to anything but true. It is the
	  same variable in every project, and it always wins over its settings.
	- One request a day at most, with counters added up locally in between.
	- It never raises into the caller and never blocks it: counting is a
	  dictionary update, sending happens on its own thread.
	- `preview()` returns exactly what the next send would carry, so a
	  project can show it to its users.
"""

import json
import os
import platform
import random
import threading
import time
import urllib.error
import urllib.request
import uuid

CLIENT_VERSION = 1
DEFAULT_ENDPOINT = "https://telemetry.dgongut.com/v1/ping"

# Nothing is sent before the process has been up this long, so a container
# stuck in a restart loop or started once to try something sends nothing.
START_DELAY_SECONDS = 10 * 60
# How often the thread wakes up to see whether a send is due.
WAKE_SECONDS = 10 * 60
# After a failed send, wait this long before trying again.
RETRY_SECONDS = 60 * 60
# Counters are written to disk at most this often, so a restart loses little.
FLUSH_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 10
# Debug mode, for trying a project against a local server: every wait above
# becomes this, and each start sends once regardless of the interval.
DEBUG_WAIT_SECONDS = 60
DEFAULT_INTERVAL_HOURS = 24
MAX_INTERVAL_HOURS = 24 * 7

ARCH_ALIASES = {
	"x86_64": "amd64",
	"amd64": "amd64",
	"aarch64": "arm64",
	"arm64": "arm64",
	"armv7l": "armv7",
	"armv6l": "armv6",
	"i386": "386",
	"i686": "386",
}


def disabled_by_environment():
	"""
	True when TELEMETRY is set to anything but true.

	The documented value is `false`, but any other one turns telemetry off as
	well: someone who writes the variable wants it off, and a spelling the
	client did not expect must not end up sending anyway. Empty counts as not
	set, which is what `- TELEMETRY=` in a compose file gives.

	It can only turn telemetry off, never on over the project's own setting.
	"""
	value = os.environ.get("TELEMETRY", "").strip().lower()
	return value not in ("", "true")


def architecture():
	machine = platform.machine().lower()
	return ARCH_ALIASES.get(machine, machine.replace("-", "_")[:16] or "unknown")


class Telemetry:
	def __init__(self, project, version, state_path, metrics=None, enabled=None,
				endpoint=DEFAULT_ENDPOINT, log=None, debug=False):
		"""
		`metrics` returns the project's metrics block when called; it runs on
		the sending thread, so it may be slow. `enabled` is asked before every
		count and every send, so switching telemetry off takes effect at once.
		`log` receives one-line debug messages.

		`debug` shortens every wait to a minute and sends once on every start,
		so a change can be seen on a local server without waiting a day. The
		project decides where debug pings go, and it should never be the real
		endpoint: they would count as real installations.
		"""
		self.project = project
		self.version = version
		self.state_path = state_path
		self.endpoint = endpoint
		self._metrics = metrics or (lambda: {})
		self._enabled = enabled or (lambda: True)
		self._log = log or (lambda message: None)
		self.debug = debug
		self._start_delay = DEBUG_WAIT_SECONDS if debug else START_DELAY_SECONDS
		self._wake = DEBUG_WAIT_SECONDS if debug else WAKE_SECONDS
		self._retry = DEBUG_WAIT_SECONDS if debug else RETRY_SECONDS
		self._sent_since_start = False

		self._lock = threading.Lock()
		self._started_at = time.monotonic()
		self._thread = None
		self._dirty = False
		self._last_flush = 0.0
		self._last_attempt = 0.0
		self._state = self._load()

	# -- public ---------------------------------------------------------

	def enabled(self):
		"""Whether anything may be counted or sent right now."""
		if disabled_by_environment():
			return False
		try:
			return bool(self._enabled())
		except Exception:
			return False

	def count(self, key, amount=1):
		"""Adds `amount` to a usage counter. A no-op while disabled."""
		if not self.enabled():
			return
		with self._lock:
			pending = self._state["pending"]
			pending[key] = pending.get(key, 0) + amount
			self._dirty = True

	def preview(self):
		"""
		Exactly what the next send would carry. The install id is None until
		the first send creates it.
		"""
		with self._lock:
			usage = dict(self._state["pending"])
			install_id = self._state.get("install_id")
		return self._payload(install_id, usage, self._collect_metrics())

	def forget(self):
		"""
		Drops the install id and every pending counter.

		Called when the user switches telemetry off, so switching it back on
		later starts as a new installation with nothing carried over.
		"""
		with self._lock:
			self._state = self._empty_state()
			self._dirty = True
		self._flush(force=True)

	def start(self):
		"""Starts the background thread. Safe to call more than once."""
		if self._thread is None:
			self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
			self._thread.start()

	# -- sending --------------------------------------------------------

	def _run(self):
		# Spread first sends so a fleet of containers updated at the same
		# time does not arrive in the same second.
		time.sleep(0 if self.debug else random.uniform(0, 60))
		while True:
			try:
				self._tick()
			except Exception as e:
				self._log(f"Telemetry tick failed: {e}")
			time.sleep(self._wake)

	def _tick(self, now=None):
		now = now if now is not None else time.time()
		self._flush()
		if not self.enabled():
			return False
		if time.monotonic() - self._started_at < self._start_delay:
			return False
		with self._lock:
			interval = self._state.get("interval_hours", DEFAULT_INTERVAL_HOURS) * 3600
			due = now - self._state.get("last_sent", 0) >= interval
			due = due or (self.debug and not self._sent_since_start)
			paused = now < self._state.get("paused_until", 0) and not self.debug
		if not due or paused or now - self._last_attempt < self._retry:
			return False
		self._last_attempt = now
		return self._send(now)

	def _send(self, now):
		with self._lock:
			if not self._state.get("install_id"):
				self._state["install_id"] = str(uuid.uuid4())
			install_id = self._state["install_id"]
			usage = dict(self._state["pending"])
		payload = self._payload(install_id, usage, self._collect_metrics())

		request = urllib.request.Request(
			self.endpoint,
			data=json.dumps(payload).encode("utf-8"),
			headers={"Content-Type": "application/json",
					"User-Agent": f"{self.project}/{self.version} telemetry/{CLIENT_VERSION}"},
			method="POST")
		try:
			with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
				answer = json.loads(response.read() or b"{}")
			# Anything but an object is a server answering something else —a
			# proxy page, a bare list—. The ping got there all the same, so it
			# is taken as delivered: failing on answer.get() after the POST left
			# the counters pending, and they went out again on the next ping.
			if not isinstance(answer, dict):
				answer = {}
		except urllib.error.HTTPError as e:
			self._log(f"Telemetry rejected: HTTP {e.code}")
			return False
		except Exception as e:
			self._log(f"Telemetry not sent: {e}")
			return False

		interval = answer.get("next_ping_h", DEFAULT_INTERVAL_HOURS)
		if not isinstance(interval, (int, float)) or isinstance(interval, bool):
			interval = DEFAULT_INTERVAL_HOURS
		interval = min(max(interval, 1), MAX_INTERVAL_HOURS)

		with self._lock:
			# Only what was sent is taken off: anything counted while the
			# request was in flight stays for the next one.
			pending = self._state["pending"]
			for key, value in usage.items():
				left = pending.get(key, 0) - value
				if left > 0:
					pending[key] = left
				else:
					pending.pop(key, None)
			self._state["last_sent"] = now
			self._state["interval_hours"] = interval
			# The server can ask a project to stop sending; it is asked again
			# after one interval, in case that changed.
			self._state["paused_until"] = 0 if answer.get("enabled", True) else now + interval * 3600
			self._dirty = True
			self._sent_since_start = True
		self._flush(force=True)
		self._log(f"Telemetry sent to {self.endpoint} ({len(usage)} counters)")
		return True

	def _payload(self, install_id, usage, metrics):
		return {
			"schema": 1,
			"project": self.project,
			"install_id": install_id,
			"version": self.version,
			"arch": architecture(),
			"metrics": metrics,
			"usage": usage,
		}

	def _collect_metrics(self):
		try:
			metrics = self._metrics()
			return metrics if isinstance(metrics, dict) else {}
		except Exception as e:
			self._log(f"Telemetry metrics failed: {e}")
			return {}

	# -- state on disk --------------------------------------------------

	@staticmethod
	def _empty_state():
		return {"install_id": None, "last_sent": 0, "interval_hours": DEFAULT_INTERVAL_HOURS,
				"paused_until": 0, "pending": {}}

	def _load(self):
		state = self._empty_state()
		try:
			with open(self.state_path, "r", encoding="utf-8") as handle:
				stored = json.load(handle)
			if isinstance(stored, dict):
				state.update({key: stored[key] for key in state if key in stored})
			if not isinstance(state["pending"], dict):
				state["pending"] = {}
		except FileNotFoundError:
			pass
		except Exception as e:
			self._log(f"Telemetry state unreadable, starting over: {e}")
		return state

	def _flush(self, force=False):
		now = time.monotonic()
		with self._lock:
			if not self._dirty or (not force and now - self._last_flush < FLUSH_SECONDS):
				return
			document = json.dumps(self._state, indent=4)
			self._dirty = False
			self._last_flush = now
		temporary = f"{self.state_path}.tmp"
		try:
			os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
			with open(temporary, "w", encoding="utf-8") as handle:
				handle.write(document)
				handle.flush()
				os.fsync(handle.fileno())
			os.replace(temporary, self.state_path)
		except Exception as e:
			self._log(f"Telemetry state not written: {e}")
