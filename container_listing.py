"""
Container listings in one request.

docker-py's `containers.list()` asks the daemon for the list and then
inspects every container on it, one request each, to hand back full
objects. Over unix:// that is a few milliseconds a container. Over ssh://
every request is a round-trip to another machine, and the listing grows by
one of those per container: fourteen containers, fifteen requests, and
nine seconds for a menu that the list alone answers in under one.

The list already says what the menus, /list and the caches read: id, name,
state, labels, image. So the objects here are built from it — the SDK's own
Container class, with `attrs` laid out the way an inspect lays out the
fields both carry — and the daemon is asked about a container individually
only when the list cannot answer for it.

What the list cannot answer is `Config.Image`, the reference the container
was created from, which the update cache is keyed by. The list repeats it
as long as that tag still points at the container's image, and prints the
image id instead once the tag has moved on — which is precisely a container
with an update pending, whose new image the check has already pulled. Those
are inspected, and only those.

Everything else an inspect carries —HostConfig beyond the network mode,
the healthcheck definition, Mounts in full, ImageManifestDescriptor— is not
here. Code that needs it says so, by calling `reload()` on the object or
`containers.get()` with the id, exactly as it does for anything that has
to be current. A listed object answers `listed` so a reader can tell.
"""

import re

import docker.errors
from docker.models.containers import Container

from logger import debug


class ListedContainer(Container):
	"""
	A docker-py Container built from the daemon's list, not from an inspect.

	Reads and acts the same —`name`, `status`, `labels`, `image`, `id`;
	`start()`, `stop()`, `logs()`, `remove()`…— because those go to the
	daemon with the id or read fields the list carries. `reload()` fetches
	the inspect, after which the object is no longer listed.
	"""
	listed = True

	def reload(self):
		super().reload()
		self.listed = False


# What `docker ps` appends to "Up 3 hours" for a container with a
# healthcheck, and the only place the list says it.
_HEALTH_IN_STATUS = re.compile(r"\((healthy|unhealthy|health: starting)\)\s*$")

# An image id, as the list prints it where a reference should be: 64 hex
# characters since 20.10, 12 before.
_IMAGE_ID = re.compile(r"[0-9a-f]{12,64}")


def health_from_status(status):
	"""
	The health the list's human-readable Status carries, or None: for a
	container without a healthcheck, or one that is not running.
	"""
	match = _HEALTH_IN_STATUS.search(status or "")
	if match is None:
		return None
	return "starting" if match.group(1) == "health: starting" else match.group(1)


def image_reference_moved(data):
	"""
	Whether the list prints an image id where the container's image reference
	should be.

	The daemon repeats the reference the container was created from only
	while that tag still points at the container's image; once the tag has
	moved on —a newer image pulled, the old one untagged— it prints the image
	id instead, as `docker ps` does. The reference is then only in the inspect.
	"""
	reference = str(data.get("Image") or "")
	image_id = str(data.get("ImageID") or "")
	bare_reference = reference[len("sha256:"):] if reference.startswith("sha256:") else reference
	bare_id = image_id[len("sha256:"):] if image_id.startswith("sha256:") else image_id
	if not _IMAGE_ID.fullmatch(bare_reference):
		return False
	# Without an ImageID there is nothing to compare with: a reference that
	# is an id is still an id.
	return not bare_id or bare_id.startswith(bare_reference)


def inspect_like(data):
	"""
	The list's entry for a container, with the fields an inspect carries
	added where the list says the same thing under another name.
	"""
	attrs = dict(data)
	# A container linked into another is listed under its own name and under
	# the link ("/web/db"), in no promised order: its own is the one without
	# a path.
	names = [name.lstrip("/") for name in data.get("Names") or [] if isinstance(name, str) and name]
	own = next((name for name in names if "/" not in name), names[0] if names else str(data.get("Id") or "")[:12])
	attrs["Name"] = f"/{own}"
	state = {"Status": data.get("State")}
	health = health_from_status(data.get("Status"))
	if health:
		state["Health"] = {"Status": health}
	attrs["State"] = state
	attrs["Config"] = {"Image": data.get("Image"), "Labels": data.get("Labels") or {}}
	if data.get("ImageID"):
		# An inspect's top-level Image is the image id; the list's is the
		# reference, which Config.Image now holds.
		attrs["Image"] = data["ImageID"]
	return attrs


def list_containers(client, all=False, filters=None, where=""):
	"""
	The containers a daemon lists, as Container objects, in one request —
	plus an inspect for each one whose image reference the list cannot give.

	Takes the arguments `containers.list()` takes. `where` names the host
	for the log line, one per listing rather than one per container.
	"""
	listing = client.api.containers(all=all, filters=filters)
	containers = []
	inspected = 0
	for data in listing:
		if image_reference_moved(data):
			try:
				containers.append(client.containers.get(data["Id"]))
			except docker.errors.NotFound:
				# Gone between the list and now. The SDK's list() raises
				# here and the whole listing fails with it; a container
				# that no longer exists is simply not listed.
				debug(f"Container {str(data.get('Id'))[:12]} was removed while listing{f' {where}' if where else ''}")
				continue
			inspected += 1
			continue
		containers.append(ListedContainer(attrs=inspect_like(data), client=client, collection=client.containers))
	total = len(listing)
	debug(f"Using the container list for {where or 'the host'}: {total} containers in one request, "
			f"{inspected} inspected for a moved image tag, {total - inspected} individual lookups avoided")
	return containers
