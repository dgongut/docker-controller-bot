"""
Fragments of the bot's messages that depend only on what they show.

Versions from and to, a container's line in a list of updates, the hosts
those lists are grouped under, the comparison's notes about versions, a
block of log lines, an exit code with the signal it stands for. Pure
functions of their arguments — and of the language and the configured
hosts — so they are tested without a bot and reused by every message
that shows the same thing, instead of each writing its own.
"""

import html
import re
import signal

import requests

import host_registry
from docker_update import is_major_upgrade
from i18n import get_text
from logger import debug

# The last lines of a container that failed, shown under the notice: enough
# to read the error, short enough not to bury the chat.
EVENT_LOG_LINES = 10


EVENT_LOG_MAX_CHARS = 1500


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def log_excerpt(text):
	"""
	A block of log lines for a message: colours stripped, long lines cut, the
	whole kept to the end that matters — the error is last — and escaped.
	None when nothing is left.
	"""
	lines = [_ANSI_ESCAPE.sub("", line).rstrip() for line in str(text or "").splitlines()]
	lines = [line if len(line) <= 300 else line[:300] + "…" for line in lines if line.strip()]
	if not lines:
		return None
	excerpt = "\n".join(lines[-EVENT_LOG_LINES:])
	if len(excerpt) > EVENT_LOG_MAX_CHARS:
		excerpt = "…" + excerpt[-EVENT_LOG_MAX_CHARS:]
	return f"<pre>{html.escape(excerpt)}</pre>"


def describe_exit_code(code):
	"""
	An exit code, with the signal it means when it means one: 137 is SIGKILL,
	139 a segmentation fault — the number alone tells most people nothing.
	"""
	try:
		number = int(code)
	except (TypeError, ValueError):
		return html.escape(str(code))
	if 128 < number < 160:
		try:
			return f"{number} ({signal.Signals(number - 128).name})"
		except ValueError:
			pass
	return str(number)


def comparison_version_line(version):
	"""The version line under a tag in the comparison, or nothing."""
	if not version:
		return ""
	return f"\n   {get_text('update_version')}: <code>{html.escape(version)}</code>"


def comparison_version_change(comparison):
	"""
	The version step as one of the comparison's changes, or None when there
	is none to show. A bullet like the others: on its line when short, and
	one version under the other, lined up with the bullet's text, when long.
	"""
	old, new = comparison.get('current_version'), comparison.get('new_version')
	if not (old and new and old != new):
		return None
	label = get_text('update_version')
	if len(old) + len(new) > VERSION_STACK_THRESHOLD:
		# Under the text of "   • ", which starts in the sixth column.
		return f"{label}:{version_lines(old, new, indent=' ' * 6)}"
	return f"{label}: {format_versions(old, new)}"


def comparison_version_notes(comparison):
	"""
	What the comparison says about the versions below its changes: a warning
	when the major version goes up — where the breaking changes are — and
	where to read what is new.
	"""
	old, new = comparison.get('current_version'), comparison.get('new_version')
	notes = ""
	if old and new and old != new and is_major_upgrade(old, new):
		notes += f"\n\n{get_text('update_major_warning')}"
	if comparison.get('release_notes_url'):
		notes += (f"\n\n📋 <a href=\"{html.escape(comparison['release_notes_url'], quote=True)}\">"
				f"{get_text('update_release_notes', html.escape(new or ''))}</a>")
	return notes


def release_notes_url(source, version):
	"""
	The page of a release on GitHub, from the repository an image names as its
	source; or its list of releases when the version has no page of its own;
	or None when the source is not on GitHub.

	Checked, because the tag is not always the version as the image writes
	it: `3.2.4` may be released as `v3.2.4`, and a link that 404s is worse
	than the list.
	"""
	match = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+)", str(source or ""))
	if not match:
		return None
	owner, repository = match.group(1), match.group(2)
	if repository.endswith(".git"):
		repository = repository[:-len(".git")]
	base = f"https://github.com/{owner}/{repository}"
	if version:
		candidates = [version] + ([version[1:]] if version.startswith("v") else [f"v{version}"])
		for tag in candidates:
			url = f"{base}/releases/tag/{tag}"
			try:
				if requests.head(url, timeout=5, allow_redirects=True).status_code == 200:
					return url
			except Exception as e:
				debug(f"Could not check {url}: {e}")
				break
	return f"{base}/releases"


def format_versions(old, new):
	"""
	`old → new` for a message, or as much of it as is known; "" for nothing.

	The same version on both sides is a rebuild of it — nginx:latest again
	with a patched base — and reads as just that version.
	"""
	old, new = (html.escape(v) if v else None for v in (old, new))
	if old and new and old != new:
		return f"<code>{old}</code> → <b><code>{new}</code></b>"
	if old or new:
		return f"<code>{old or new}</code>"
	return ""


# Two versions longer than this together go one under the other: side by
# side, linuxserver's `1.43.4.10903-e5521bd8c-ls326 → …-ls327` wrapped into a
# line nobody could read on a phone.
VERSION_STACK_THRESHOLD = 24


def version_lines(old, new, indent="   "):
	"""
	The versions under a line: `old → new` on one line when they are short,
	the old one, an arrow and the new one on three when they are not; ""
	when the image says nothing. `indent` lines them up under what they
	belong to — a container's name, or a bullet's text.
	"""
	if old and new and old != new and len(old) + len(new) > VERSION_STACK_THRESHOLD:
		return (f"\n{indent}<code>{html.escape(old)}</code>\n{indent} ↓"
				f"\n{indent}<b><code>{html.escape(new)}</code></b>")
	text = format_versions(old, new)
	return f"\n{indent}{text}" if text else ""


def container_line(name, old=None, new=None):
	"""
	One container in a list of updates: the whale, its name, and its
	versions below. The host is the heading it goes under; see by_host.
	"""
	return f"🐳 <b>{html.escape(str(name))}</b>{version_lines(old, new)}"


def by_host(entries):
	"""
	(host_id, line) pairs as one text, under a heading per host — or just
	the lines with a single host, where there is no host to speak of. In the
	order given: the lists are already walked host by host.
	"""
	if host_registry.is_single_host():
		return "\n".join(line for _, line in entries)
	groups = {}
	for host_id, line in entries:
		groups.setdefault(host_id, []).append(line)
	return "\n\n".join(f"🖥️ <b>{html.escape(host_registry.alias(host_id))}</b>\n" + "\n".join(lines)
						for host_id, lines in groups.items())
