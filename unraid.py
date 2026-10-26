"""
Telling Unraid that a container the bot updated is up to date.

Unraid remembers, per image, the digest it last pulled, in
/var/lib/docker/unraid-update-status.json, and writes it only when it pulls
the image itself. After the bot updated a container the entry kept the digest
from before, and Unraid's Docker page said "update ready" for good: its own
check for updates reuses the stored digest, and reads the image only when
there is no entry. The page also caches that verdict per container, in
docker.json, and drops it only when Unraid removes the container itself.

So after an update on an Unraid host, the bot writes what Unraid writes after
one of its own: the image's entry with the new digest as both local and
remote, and the container gone from the page's cache, which the page then
works out again. Read off Unraid 7.3.4's DockerClient.php.

## How it gets to those files

They are on the machine Docker runs on, which is not where the bot is: the bot
runs in a container, or on another machine. Docker can reach them, though:
the source of a bind mount is a path on Docker's own machine, whichever way
the bot talks to it — the local socket, tcp:// or ssh://. So a helper
container is created with the two directories mounted, and the files are read
and written through the API `docker cp` uses, which works on a container that
never started. Measured on Docker 24.0.2 and 29.

The helper never starts: nothing in it runs, it has no network, and it is
removed straight after. It is made from the image just pulled, the one image
certain to be on that host, with a tmpfs over each VOLUME the image declares
so that no anonymous volume is created for it.

## What it never does

Being Unraid's file, every way of getting it wrong is cut short before
writing:

- The mounts are strict: Docker refuses a source that does not exist instead
  of creating it, so an Unraid that moved its files gets nothing new on disk.
- Only the entry of the image just updated changes, and only when Unraid
  already has one. A file that does not read as Unraid's is not written.
- Docker writes the file anew rather than in place. Unraid reads a missing or
  broken file as empty and works every entry out again from the images, which
  is the state this was going to write anyway.
- Unraid does not lock the file. Its own check landing in the same instant
  wins or loses against this one: the mark of one image is wrong until the
  next check, as it was before any of this. The bot's own writes to one host
  go one at a time.

Nothing here is ever raised: the update it follows went through already.
"""

import io
import json
import re
import secrets
import tarfile
import threading
import time
from datetime import datetime, timezone

import docker.errors
import docker.types

STATUS_DIR = "/var/lib/docker"
STATUS_FILE = "unraid-update-status.json"
INFO_DIR = "/usr/local/emhttp/state/plugins/dynamix.docker.manager"
INFO_FILE = "docker.json"

# Unraid keeps the update status only of the containers it created from a
# template; one from Compose it marks "composeman" and leaves alone.
MANAGED_LABEL = "net.unraid.docker.managed"
MANAGED_BY_UNRAID = "dockerman"

# What makes a container one of these helpers: the event monitor says nothing
# about it, and a leftover is swept away. Name and label both, so the sweep
# never takes a container of the user's for one.
HELPER_LABEL = "dcb.helper"
HELPER_KIND = "unraid"
HELPER_PREFIX = "dcb-unraid-"
# A helper lives a second or two. One older than this was left behind by a bot
# that stopped halfway, and not by another bot at work on the same host.
HELPER_ABANDONED_SECONDS = 600

_STATUS_MOUNT = "/dcb/status"
_INFO_MOUNT = "/dcb/info"

# Both files are a few KB. Anything far bigger is not what this expects.
MAX_FILE_BYTES = 4 * 1024 * 1024

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")

_host_locks = {}
_host_locks_guard = threading.Lock()


def is_unraid(client):
	"""Whether a host is Unraid, which says so as its operating system."""
	return str(client.info().get("OperatingSystem") or "").startswith("Unraid")


def is_helper(name, labels):
	"""Whether a container is one of these helpers, from its name and labels."""
	return (labels or {}).get(HELPER_LABEL) == HELPER_KIND and str(name or "").startswith(HELPER_PREFIX)


def status_key(image):
	"""
	The key Unraid files an image under, or None for one it cannot be.

	DockerUtil::ensureImageTag: `library/` in front of a name with no slash,
	`:latest` after one with no tag, and nothing else — `docker.io/` stays if
	it was written. An image pinned by digest is left out.
	"""
	image = str(image or "").strip()
	if not image or "@" in image or image.startswith("sha256:"):
		return None
	if "/" not in image:
		image = f"library/{image}"
	head, slash, last = image.rpartition("/")
	parts = last.split(":")
	tag = parts[-1] if len(parts) > 1 else ""
	return f"{head}{slash}{parts[0]}:{tag or 'latest'}"


def _same_repository(a, b):
	"""Whether two repository names are the same one, however Docker Hub is spelt."""
	def plain(name):
		for prefix in _DOCKER_HUB_PREFIXES:
			if name.startswith(prefix):
				name = name[len(prefix):]
				break
		return name[len("library/"):] if name.startswith("library/") else name
	return plain(a) == plain(b)


def pulled_digest(repo_digests, key):
	"""
	The digest the image was pulled as, for the repository of `key`, or None.

	It is what Unraid compares with the registry: the pull says it as
	`Digest: …`, which is what Unraid stores, and Docker keeps it in
	RepoDigests. An image pulled from more than one repository has one per
	repository, so the one of the container's is taken.
	"""
	repository = key.rpartition(":")[0]
	for entry in reversed(repo_digests or []):
		name, _, digest = str(entry).partition("@")
		if _same_repository(name, repository) and _DIGEST.match(digest):
			return digest
	return None


def patch_status(status, key, digest):
	"""
	The status file with `key` marked as running `digest`, as Unraid's
	setUpdateStatus does after a pull of its own; None when there is nothing
	to change, or the file is not what Unraid writes.
	"""
	if not isinstance(status, dict) or not isinstance(status.get(key), dict):
		return None
	wanted = {"local": digest, "remote": digest, "status": "true"}
	if all(status[key].get(field) == value for field, value in wanted.items()):
		return None
	patched = dict(status)
	patched[key] = wanted
	return patched


def patch_info(info, name):
	"""
	The page's cache without `name`, as Unraid's removeContainer leaves it,
	so that the page works out its mark again; None when there is nothing to
	drop. A helper the page happened to see goes too: Unraid drops an entry
	only when it removes the container itself.
	"""
	if not isinstance(info, dict):
		return None
	drop = [entry for entry in info if entry == name or str(entry).startswith(HELPER_PREFIX)]
	if not drop:
		return None
	return {entry: value for entry, value in info.items() if entry not in drop}


def _read_json(helper, path):
	"""A JSON file through the helper, or None when it is not there or not JSON."""
	try:
		stream, stat = helper.get_archive(path)
	except docker.errors.NotFound:
		return None
	if (stat or {}).get("size", 0) > MAX_FILE_BYTES:
		return None
	data = b"".join(stream)
	with tarfile.open(fileobj=io.BytesIO(data)) as tar:
		member = next((m for m in tar.getmembers() if m.isfile()), None)
		if member is None:
			return None
		content = tar.extractfile(member).read()
	try:
		return json.loads(content)
	except ValueError:
		return None


def _write_json(helper, directory, name, data):
	"""
	Writes a JSON file through the helper, formatted the way Unraid's
	saveJSON does (JSON_PRETTY_PRINT, slashes as they are).
	"""
	content = json.dumps(data, indent=4).encode()
	buffer = io.BytesIO()
	with tarfile.open(fileobj=buffer, mode="w") as tar:
		member = tarfile.TarInfo(name)
		member.size = len(content)
		member.mode = 0o644
		member.mtime = int(time.time())
		tar.addfile(member, io.BytesIO(content))
	helper.put_archive(directory, buffer.getvalue())


def _host_lock(host_id):
	with _host_locks_guard:
		return _host_locks.setdefault(host_id, threading.Lock())


def _created_seconds_ago(attrs):
	"""How long ago a container was created, from its inspect; 0 when unknown."""
	created = str((attrs or {}).get("Created") or "")[:19]
	try:
		moment = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
	except ValueError:
		return 0
	return (datetime.now(timezone.utc) - moment).total_seconds()


def _sweep(client, debug):
	"""Removes the helpers a bot that stopped halfway left on this host."""
	for leftover in client.containers.list(all=True, filters={"label": f"{HELPER_LABEL}={HELPER_KIND}"}):
		if not is_helper(leftover.name, leftover.labels):
			continue
		if _created_seconds_ago(leftover.attrs) < HELPER_ABANDONED_SECONDS:
			continue
		try:
			leftover.remove(v=True, force=True)
			debug(f"[UNRAID] Removed {leftover.name}, a helper left behind")
		except Exception as e:
			debug(f"[UNRAID] Could not remove {leftover.name}, a helper left behind: {e}")


def _create_helper(client, image, with_info):
	"""The helper container, created and never started."""
	mounts = [docker.types.Mount(_STATUS_MOUNT, STATUS_DIR, type="bind")]
	if with_info:
		mounts.append(docker.types.Mount(_INFO_MOUNT, INFO_DIR, type="bind"))
	volumes = ((image.attrs or {}).get("Config") or {}).get("Volumes") or {}
	mounts += [docker.types.Mount(path, None, type="tmpfs") for path in volumes
				if path not in (_STATUS_MOUNT, _INFO_MOUNT)]
	return client.containers.create(
		image.id,
		name=f"{HELPER_PREFIX}{secrets.token_hex(4)}",
		labels={HELPER_LABEL: HELPER_KIND},
		entrypoint=["true"],
		command=[],
		network_disabled=True,
		mounts=mounts,
	)


def sync_after_update(client, host_id, container, debug):
	"""
	Tells Unraid that `container`, just updated, runs its newest image.

	Does nothing on a host that is not Unraid or for a container Unraid does
	not track, and never raises: whatever goes wrong is logged and left.
	"""
	name = getattr(container, "name", None) or "?"
	try:
		_sync(client, host_id, container, name, debug)
	except Exception as e:
		debug(f"[UNRAID] Could not tell Unraid that {name} is up to date: {e}")


def _sync(client, host_id, container, name, debug):
	config = (container.attrs or {}).get("Config") or {}
	if (config.get("Labels") or {}).get(MANAGED_LABEL) != MANAGED_BY_UNRAID:
		return
	if not is_unraid(client):
		return
	key = status_key(config.get("Image"))
	if key is None:
		debug(f"[UNRAID] {name} runs {config.get('Image')}, which Unraid does not keep an update status for")
		return
	image = client.images.get(container.attrs["Image"])
	digest = pulled_digest(image.attrs.get("RepoDigests"), key)
	if digest is None:
		debug(f"[UNRAID] The image of {name} says no digest for {key}; Unraid is left as it is")
		return

	with _host_lock(host_id):
		_sweep(client, debug)
		with_info = True
		try:
			helper = _create_helper(client, image, with_info)
		except docker.errors.APIError as e:
			# The page's cache is the one that may not be there: it is in
			# RAM, under the web interface's directory. The status file is
			# what matters, and the page works its mark out from it.
			debug(f"[UNRAID] Without the page's cache for {name}: {e}")
			with_info = False
			helper = _create_helper(client, image, with_info)
		try:
			status = _read_json(helper, f"{_STATUS_MOUNT}/{STATUS_FILE}")
			patched = patch_status(status, key, digest)
			if patched is not None:
				_write_json(helper, _STATUS_MOUNT, STATUS_FILE, patched)
				debug(f"[UNRAID] {key} marked as running {digest[:19]}")
			elif not isinstance(status, dict) or key not in status:
				debug(f"[UNRAID] Unraid has no status for {key} yet; it will read it off the image")
			if with_info:
				info = _read_json(helper, f"{_INFO_MOUNT}/{INFO_FILE}")
				cleaned = patch_info(info, name)
				if cleaned is not None:
					_write_json(helper, _INFO_MOUNT, INFO_FILE, cleaned)
		finally:
			helper.remove(v=True, force=True)
