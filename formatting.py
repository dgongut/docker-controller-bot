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


# How many networks, ports or mounts /info lists before saying how many more:
# a container with dozens would push the message past Telegram's limit.
INFO_LIST_LIMIT = 8


def human_bytes(num):
	"""
	1536 → "1.5 KiB". Binary, like `docker stats` and Docker's own limits: an
	8m memory limit read in decimal units came out as "8.4 MB".
	"""
	num = float(num or 0)
	for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
		if abs(num) < 1024 or unit == "TiB":
			return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
		num /= 1024.0


def human_duration(seconds):
	"""The largest unit that says how long, in the user's language: "3 días", "25 min"."""
	seconds = max(0, int(seconds))
	units = ((86400, "info_day", "info_days"), (3600, "info_hour", "info_hours"),
				(60, "info_minute", "info_minutes"), (1, "info_second", "info_seconds"))
	for size, one, many in units:
		if seconds >= size or size == 1:
			count = seconds // size
			return get_text(one if count == 1 else many, count)


def _capped(items, render):
	"""Rendered lines for a list, cut at INFO_LIST_LIMIT with a count of the rest."""
	lines = [render(item) for item in items[:INFO_LIST_LIMIT]]
	if len(items) > INFO_LIST_LIMIT:
		lines.append(f"   {get_text('info_more', len(items) - INFO_LIST_LIMIT)}")
	return lines


def _code(value):
	return f"<code>{html.escape(str(value))}</code>"


def _join(parts):
	return "  ·  ".join(part for part in parts if part)


def render_container_info(info):
	"""
	The /info message, from what core gathered about a container.

	Sections in the order someone looking at a container asks: is it up, what
	runs, is there anything newer, how does the bot treat it, what does it
	use, where is it reachable, what does it keep, what is it part of. One
	that has nothing to say is left out rather than shown empty.
	"""
	e = lambda value: html.escape(str(value))
	sections = []

	header = f"📦 <b>{e(info['name'])}</b>"
	if info.get("host"):
		header += f"  ·  🖥️ <b>{e(info['host'])}</b>"
	sections.append(header)

	# State
	status = info["status"]
	emoji, key = {"running": ("🟢", "info_running"), "paused": ("🟠", "info_paused"),
					"restarting": ("🟡", "info_restarting"), "created": ("🔵", "info_created")
					}.get(status, ("🔴", "info_stopped"))
	if info.get("own"):
		emoji = "👑"
	line = f"{emoji} <b>{get_text(key)}</b>"
	since = info.get("since")
	if since:
		line += " " + get_text("info_since", human_duration(info["since_seconds"]), e(since))
	state = [line]
	if status not in ("running", "paused", "restarting", "created"):
		if info.get("oom"):
			state.append(f"   💥 {get_text('info_oom')} · {get_text('info_exit_code')} {_code(info['exit_code_text'])}")
		elif info.get("exit_code_text") is not None:
			state.append(f"   {get_text('info_exit_code').capitalize()} {_code(info['exit_code_text'])}")
	health = {"healthy": f"💚 {get_text('health_healthy')}", "unhealthy": f"💔 {get_text('health_unhealthy')}",
				"starting": f"🟡 {get_text('health_starting')}"}.get(info.get("health"))
	restarts = f"🔁 {get_text('info_restarts', info.get('restarts', 0))}"
	state.append(f"   {_join([health, restarts])}")
	if info.get("restart_policy"):
		state.append(f"   🔄 {get_text('info_restart_policy')}: {_code(info['restart_policy'])}")
	sections.append("\n".join(state))

	# Image
	image = [f"🏷️ <b>{get_text('info_image')}</b>", f"   {_code(info['image'])}"]
	if info.get("version"):
		image.append(f"   {get_text('update_version')}: {_code(info['version'])}")
	details = _join([f"{get_text('update_created')}: {e(info['image_created'])}" if info.get("image_created") else "",
						e(info["image_size"]) if info.get("image_size") else "",
						_code(info["image_digest"]) if info.get("image_digest") else ""])
	if details:
		image.append(f"   {details}")
	if info.get("foreign_platform"):
		image.append(f"   🔄 {get_text('info_architecture')}: "
						f"{get_text('info_architecture_foreign', _code(info['foreign_platform']), _code(info['host_architecture']))}")
	links = []
	if info.get("release_notes_url"):
		# The page of this version when there is one; otherwise the list of
		# releases, and the text has to say that is what it is.
		key = 'info_releases' if info['release_notes_url'].endswith("/releases") else 'info_release_notes'
		links.append(f"📋 <a href=\"{html.escape(info['release_notes_url'], quote=True)}\">{get_text(key)}</a>")
	if info.get("registry_url"):
		links.append(f"🔗 <a href=\"{html.escape(info['registry_url'], quote=True)}\">{e(info.get('registry_name') or info['registry_url'])}</a>")
	if links:
		image.append(f"   {_join(links)}")
	sections.append("\n".join(image))

	# Pending update
	if info.get("has_update"):
		old, new = info.get("update_versions") or (None, None)
		sections.append(f"⬆️ <b>{get_text('info_update_available')}</b>{version_lines(old, new)}")

	# How the bot treats it
	bot = [f"🤖 <b>{get_text('info_bot')}</b>"]
	if info.get("ignore_checks"):
		bot.append(f"   🚫 {get_text('info_ignore_checks')}")
	else:
		bot.append(f"   {get_text('info_auto_update')}: {get_text('schedule_yes') if info.get('auto_update') else get_text('schedule_no')}")
		if info.get("last_check_seconds") is not None:
			bot.append(f"   🕐 {get_text('info_last_check')}: {get_text('info_ago', human_duration(info['last_check_seconds']))}")
	for name, cron in info.get("schedules") or []:
		bot.append(f"   ⏰ {get_text('info_schedule')}: <i>{e(name)}</i> · {_code(cron)}")
	sections.append("\n".join(bot))

	# Resources
	resources = []
	stats = info.get("stats")
	if stats:
		cpu = f"CPU: {stats['cpu']:.1f}%"
		if info.get("cpu_limit"):
			cpu += f" ({get_text('info_limit')}: {info['cpu_limit']:g} CPU)"
		ram = None
		if stats.get("memory") is not None:
			ram = f"RAM: {human_bytes(stats['memory'])}"
			if stats.get("memory_limit"):
				ram += f" / {human_bytes(stats['memory_limit'])} ({stats['memory'] / stats['memory_limit'] * 100:.0f}%)"
		resources.append(f"   {_join([cpu, ram])}")
		io = _join([f"{get_text('info_network_io')}: ↓ {human_bytes(stats['rx'])}  ↑ {human_bytes(stats['tx'])}" if stats.get("rx") is not None else "",
					f"{get_text('info_disk')}: {human_bytes(stats['read'])} {get_text('info_read')}, {human_bytes(stats['write'])} {get_text('info_written')}" if stats.get("read") is not None else "",
					f"{get_text('info_processes')}: {stats['pids']}" if stats.get("pids") else ""])
		if io:
			resources.append(f"   {io}")
	elif info.get("cpu_limit") or info.get("memory_limit"):
		resources.append(f"   {get_text('info_limits')}: " + _join([
			f"CPU {info['cpu_limit']:g}" if info.get("cpu_limit") else f"CPU {get_text('info_no_limit')}",
			f"RAM {human_bytes(info['memory_limit'])}" if info.get("memory_limit") else f"RAM {get_text('info_no_limit')}"]))
	if info.get("gpu"):
		resources.append(f"   🎮 {get_text('info_gpu')} ({e(info['gpu'])})")
	if info.get("privileged"):
		resources.append(f"   ⚠️ {get_text('info_privileged')}")
	elif info.get("all_capabilities"):
		resources.append(f"   ⚠️ {get_text('info_all_capabilities')}")
	if resources:
		sections.append("\n".join([f"📊 <b>{get_text('info_resources')}</b>"] + resources))

	# Network
	network = []
	if info.get("network_of"):
		network.append(f"   🔒 {get_text('info_uses_network_of')}: {_code(info['network_of'])}")
	elif info.get("network_mode"):
		network.append(f"   {_code(info['network_mode'])}")
	network += _capped(info.get("networks") or [], lambda n: f"   {_code(n[0])}: {e(n[1])}" if n[1] else f"   {_code(n[0])}")
	if info.get("shared_by"):
		network.append(f"   🔒 {get_text('info_network_shared_by')}: " + ", ".join(_code(n) for n in info["shared_by"]))
	ports = info.get("ports") or []
	if ports:
		shown = ", ".join(_code(p) for p in ports[:INFO_LIST_LIMIT])
		if len(ports) > INFO_LIST_LIMIT:
			shown += f" {get_text('info_more', len(ports) - INFO_LIST_LIMIT)}"
		network.append(f"   {get_text('ports')}: {shown}")
	if network:
		sections.append("\n".join([f"🌐 <b>{get_text('info_network')}</b>"] + network))

	# Storage
	mounts = info.get("mounts") or []
	if mounts:
		def mount_line(mount):
			source, target, read_only = mount
			line = f"   {_code(source)} → {_code(target)}"
			return f"{line} ({get_text('info_read_only')})" if read_only else line
		sections.append("\n".join([f"💽 <b>{get_text('info_storage')}</b>"] + _capped(mounts, mount_line)))

	# Compose
	if info.get("compose_project"):
		compose = [f"🧩 <b>Compose</b>: {get_text('info_compose_project', _code(info['compose_project']), _code(info.get('compose_service') or '?'))}"]
		relations = _join([
			f"🔗 {get_text('info_depends_on')}: " + ", ".join(_code(d) for d in info["depends_on"]) if info.get("depends_on") else "",
			f"{get_text('info_dependents')}: " + ", ".join(_code(d) for d in info["dependents"]) if info.get("dependents") else ""])
		if relations:
			compose.append(f"   {relations}")
		sections.append("\n".join(compose))

	sections.append(f"🆔 {_code(info['short_id'])}" + (f"  ·  {get_text('info_container_created', e(info['created']))}" if info.get("created") else ""))
	return "\n\n".join(sections)
