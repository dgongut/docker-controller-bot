"""
Finding the bot's own container, without being told which one it is.

`CONTAINER_NAME` existed because a process inside a container has no obvious
way to point at itself through the Docker API: the API speaks in ids, and the
process does not know its own. So the bot was told its name and matched on it.

That has three costs. It is one more thing to get right in the docker-compose,
and getting it wrong disables the safeguards silently. It matches by **name**,
so a container called the same on another machine is mistaken for the bot —
which, on the update path, meant pressing update on a remote namesake ran the
self-updater against the local one instead. And a rename breaks it.

There is a way, and it needs neither a variable nor a label.

## How

Docker always bind-mounts three files into every container it starts, from the
container's own directory on the host:

    /var/lib/docker/containers/<id>/hostname   -> /etc/hostname
    /var/lib/docker/containers/<id>/hosts      -> /etc/hosts
    /var/lib/docker/containers/<id>/resolv.conf -> /etc/resolv.conf

`/proc/self/mountinfo` lists every mount with both ends: field 4 is the path
on the host side, field 5 is where it lands inside. So the id is the 64 hex
characters in the host-side path of the line that lands on `/etc/hostname`.

Measured, not assumed. On Docker 29 with cgroup v2 this holds with no volumes
at all, read-only, `--privileged`, `--pid host` and — the one that matters,
because the project's own compose example uses it — `--network host`.

## Why not the obvious ones

- **`/etc/hostname`**, or `socket.gethostname()`. Docker sets the hostname to
  the short id by default, so this looks like the easy answer. It is not: with
  `network_mode: host` the container inherits the *machine's* hostname, and
  anyone who sets `hostname:` breaks it too. Both were checked; both return
  the wrong thing.
- **`/proc/self/cgroup`**, the method every old answer on the internet gives.
  It carried the id under cgroup v1. Under cgroup v2 the whole file reads
  `0::/`. Kept below as a fallback for old kernels, and expected to find
  nothing on anything current.

## Why the id still has to be checked

Nothing stops a mount whose *host-side* path happens to contain 64 hex
characters — that was tried too, and it does produce a second candidate. What
it cannot do is land on `/etc/hostname`, which is where Docker puts the
container's own. And beyond that, whoever uses these ids asks the daemon to
confirm them: a wrong guess resolves to nothing rather than to somebody else's
container.

So this module only ever offers candidates, best first. Deciding is the
caller's job, because only the caller can ask Docker.
"""

import re

from logger import debug

MOUNTINFO_PATH = "/proc/self/mountinfo"
CGROUP_PATH = "/proc/self/cgroup"

# Where Docker lands a container's own files. Ordered by how specific each one
# is: a user could plausibly mount their own /etc/hosts, and much less
# plausibly their own /etc/hostname.
OWN_MOUNT_POINTS = ("/etc/hostname", "/etc/resolv.conf", "/etc/hosts")

_ID = re.compile(r"\b[0-9a-f]{64}\b")


def _ids_from_mountinfo(text):
	"""Container ids from the lines that land on Docker's own files."""
	found = []
	by_point = {}
	for line in text.splitlines():
		fields = line.split()
		if len(fields) < 5:
			continue
		host_side, mount_point = fields[3], fields[4]
		if mount_point not in OWN_MOUNT_POINTS:
			continue
		match = _ID.search(host_side)
		if match:
			by_point.setdefault(mount_point, match.group(0))
	# In the order OWN_MOUNT_POINTS declares, so the most specific wins.
	for point in OWN_MOUNT_POINTS:
		found.append(by_point.get(point))
	return [i for i in dict.fromkeys(found) if i]


def _ids_from_cgroup(text):
	"""
	Container ids from a cgroup v1 file.

	Only reachable on an old kernel: v2 writes `0::/` and there is nothing in
	it to find.
	"""
	found = []
	for line in text.splitlines():
		match = _ID.search(line)
		if match:
			found.append(match.group(0))
	return list(dict.fromkeys(found))


def _read(path):
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as handle:
			return handle.read()
	except OSError:
		return ""


def candidates(mountinfo_path=MOUNTINFO_PATH, cgroup_path=CGROUP_PATH):
	"""
	Ids this process might be running under, best first, possibly empty.

	Empty means "could not tell", not "not in a container": outside one there
	is nothing to find, and on an exotic runtime the files may look different.
	Callers treat both the same way, because the answer they need — which
	container is me — is unavailable either way.
	"""
	found = _ids_from_mountinfo(_read(mountinfo_path))
	if found:
		return found
	# Old kernels only. Worth the four lines: it costs one file read on a path
	# that would otherwise give up.
	fallback = _ids_from_cgroup(_read(cgroup_path))
	if fallback:
		debug("Identified this container through cgroup v1, not mountinfo")
	return fallback
