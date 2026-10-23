"""
Docker Container Update Logic
Handles the complex logic of updating a Docker container with a new image
while preserving all configuration, mounts, networks, and resources.
"""

import docker
import docker.types
import docker.errors
import copy
import re
import time
import threading

# The containers being updated right now, so the same one is never updated
# twice at once. A set that empties as updates finish, rather than a lock per
# id kept for good: every update gives the container a new id, so that map
# gained an entry each time and never lost one.
_updating = set()
_updating_lock = threading.Lock()

def _start_updating(container_id):
	"""Claims a container for an update; False when one is already running."""
	with _updating_lock:
		if container_id in _updating:
			return False
		_updating.add(container_id)
		return True

def _done_updating(container_id):
	with _updating_lock:
		_updating.discard(container_id)

def updates_in_progress():
	"""How many containers are being updated right now."""
	with _updating_lock:
		return len(_updating)


# What Docker waits before killing a container that set no stop_grace_period.
DEFAULT_STOP_SECONDS = 10


def stop_container(container):
	"""
	Stops a container giving it the grace period it asked for.

	Passed explicitly because of how the SDK times the call: with no `t` the
	daemon waits the container's own StopTimeout, but the client still gives
	up after its request timeout. In 4.x that was 60 s; with the 30 s hosts
	have now, a container with `stop_grace_period: 60s` came back as a timeout
	halfway through stopping, and the update gave up on it. With `t` the SDK
	adds the wait to the request timeout itself.
	"""
	grace = ((container.attrs or {}).get("Config") or {}).get("StopTimeout")
	if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
		grace = DEFAULT_STOP_SECONDS
	container.stop(timeout=grace)


def _get_list(data, key, default=None):
	"""Safely get a list value from a dict. Returns default if None or missing."""
	if data is None:
		return default if default is not None else []
	val = data.get(key)
	if val is None:
		return default if default is not None else []
	return val

def _get_dict(data, key, default=None):
	"""Safely get a dict value from a dict. Returns default if None or missing."""
	if data is None:
		return default if default is not None else {}
	val = data.get(key)
	if val is None:
		return default if default is not None else {}
	return val

def _get_val(data, key, default=None):
	"""Safely get any value from a dict. Returns default if None or missing."""
	if data is None:
		return default
	val = data.get(key)
	return val if val is not None else default


def _normalize_command(value):
	"""Normalize Entrypoint/Cmd values for comparison (None, [] and '' are equivalent)."""
	if value is None or value == '':
		return []
	if isinstance(value, str):
		return [value]
	return list(value)


def image_repository(reference):
	"""
	The repository of an image reference, without its tag or digest.

	The tag is whatever follows the last colon *after the last slash*: the
	first colon may be a registry's port. Splitting on it turned
	`nas:5000/app:latest` into `nas`, and a /changetag to v2 into a pull of
	`nas:v2`.
	"""
	reference = str(reference or '').split('@', 1)[0]
	colon = reference.rfind(':')
	return reference[:colon] if colon > reference.rfind('/') else reference


def pull_checked(client, image, platform=None):
	"""
	Pulls an image and returns it, like images.pull(), but fails when the
	pull does.

	The daemon answers 200 as soon as a pull starts, and whatever goes wrong
	after that — a full disk, a layer that does not verify, a registry that
	drops the connection — comes as an `error` inside the stream. The SDK
	reads the stream without looking at it and returns the image that was
	there already: a check for updates took the old image for the new one and
	stored the container as up to date, with nothing in the log.
	"""
	name, tag = docker.utils.parse_repository_tag(str(image))
	# `repo:tag@sha256:…` is pulled by its digest, and the tag on it says
	# nothing more: Docker keeps only the digest of such a reference. Looked
	# up as `repo@sha256:…`, which is what the pull records.
	repository = image_repository(name)
	tag = tag or 'latest'
	failure = None
	for chunk in client.api.pull(repository, tag=tag, platform=platform, stream=True, decode=True):
		if not isinstance(chunk, dict) or not (chunk.get('error') or chunk.get('errorDetail')):
			continue
		detail = chunk.get('errorDetail')
		failure = failure or chunk.get('error') or (detail.get('message') if isinstance(detail, dict) else None) or str(detail)
		# Read to the end all the same, as images.pull() does: a stream left
		# half read holds on to its connection until it is collected.
	if failure:
		raise docker.errors.APIError(f"Pull of {image} failed: {failure}")
	separator = '@' if '@' in str(image) else ':'
	return client.images.get(f"{repository}{separator}{tag}")


# Label namespaces that describe an image, and only ever come from one.
IMAGE_METADATA_LABEL_PREFIXES = ("org.opencontainers.image.", "org.label-schema.")

# Where an image says which version of its program it carries. The OCI label
# is the standard and most images set it; label-schema is what came before.
VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version")

# Docker's official images (nginx, postgres, redis…) set neither, and say it
# in an environment variable instead: NGINX_VERSION, PG_VERSION. Only the one
# named after the image counts — the same images also carry GOSU_VERSION or
# NODE_VERSION, which are the version of something else.
_VERSION_ENV_ALIASES = {"postgres": ("pg",)}


def image_version(config, repository=None):
	"""
	The version an image declares, from its Config, or None.

	`config` is the Config of an image or of a container (which carries its
	image's labels and environment). Shown as the image wrote it:
	`1.43.4.10903-e5521bd8c-ls326` is what linuxserver calls that release.
	"""
	config = config if isinstance(config, dict) else {}
	labels = config.get('Labels') if isinstance(config.get('Labels'), dict) else {}
	for key in VERSION_LABELS:
		value = labels.get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()
	if not repository:
		return None
	name = str(repository).rsplit('/', 1)[-1].lower()
	accepted = {name} | set(_VERSION_ENV_ALIASES.get(name, ()))
	for entry in config.get('Env') or []:
		key, _, value = str(entry).partition('=')
		if key.endswith('_VERSION') and value.strip() and key[:-len('_VERSION')].lower() in accepted:
			return value.strip()
	return None


def _major(version):
	"""The leading number of a version, when it is a semantic one."""
	match = re.match(r"v?(\d+)(?:[.\-_+]|$)", str(version or "").strip(), re.IGNORECASE)
	if not match:
		return None
	number = int(match.group(1))
	# 2026.9.4 is a date: its first number changes every January, and that
	# says nothing about compatibility.
	return None if number >= 1000 else number


def is_major_upgrade(old, new):
	"""Whether going from `old` to `new` raises the major version."""
	before, after = _major(old), _major(new)
	return before is not None and after is not None and after > before


def container_platform(container):
	"""
	The platform a container actually runs, as `os/arch[/variant]`, or None.

	Not the host's: `platform: linux/amd64` on an Apple Silicon Mac or a
	Raspberry Pi, or a 32-bit image on a 64-bit Pi. A pull without a platform
	brings the host's own, and the update quietly turned an x86_64 container
	into an aarch64 one. Read from the manifest the container was created
	from (the containerd image store, where an image's id is the same for
	every architecture), or from the image itself (the classic store).
	"""
	descriptor = _get_dict(container.attrs, 'ImageManifestDescriptor')
	platform = _get_dict(descriptor, 'platform')
	os_name, architecture, variant = platform.get('os'), platform.get('architecture'), platform.get('variant')
	if not architecture:
		try:
			image = container.image.attrs or {}
		except Exception:
			return None
		os_name, architecture, variant = image.get('Os'), image.get('Architecture'), image.get('Variant')
	if not isinstance(os_name, str) or not isinstance(architecture, str) or not os_name or not architecture:
		return None
	return "/".join(part for part in (os_name, architecture, variant if isinstance(variant, str) else None) if part)


def _link_pairs(links):
	"""
	Links as (container, alias) pairs, the only shape the SDK takes.

	Docker reports them in two others: `/db:/web/alias` in HostConfig.Links,
	and `db:alias` on a network endpoint. Both used to be handed back as
	strings, which the SDK unpacks as pairs: any container with `links:`
	failed to update, every time.
	"""
	pairs = []
	for link in links or []:
		if isinstance(link, (list, tuple)) and len(link) == 2:
			pairs.append((str(link[0]), str(link[1])))
			continue
		name, _, alias = str(link).partition(':')
		name = name.lstrip('/')
		alias = alias.rsplit('/', 1)[-1] if alias else name
		if name:
			pairs.append((name, alias))
	return list(dict.fromkeys(pairs))


# How long the new container has to stay up, and how long it is given to
# get there, before the original is deleted.
VERIFY_STABLE_SECONDS = 2
VERIFY_TIMEOUT_SECONDS = 15
VERIFY_POLL_SECONDS = 0.5


def _container_logs(container):
	try:
		return container.logs(tail=50).decode('utf-8', errors='ignore')
	except Exception as log_error:
		return f"[Could not retrieve logs: {log_error}]"


def _verify_stays_up(container, container_name, debug_func):
	"""Raises unless the container stays running, unrestarted, for a while."""
	deadline = time.time() + VERIFY_TIMEOUT_SECONDS
	up_since = None
	restarts = None
	while True:
		try:
			container.reload()
		except docker.errors.NotFound:
			raise Exception("Container was removed by external process during verification")
		state = _get_dict(container.attrs, 'State')
		status = container.status
		count = _get_val(container.attrs, 'RestartCount', 0)
		if restarts is None:
			restarts = count
		elif count != restarts:
			# A restart policy hides a crash behind a container that is
			# "running" again by the next look.
			raise Exception(f"Container restarted during verification. Last logs: {_container_logs(container)}")
		debug_func(f"[VERIFY_CONTAINER] Container status: {status}")
		if status in ('exited', 'dead'):
			raise Exception(f"Container exited with code {_get_val(state, 'ExitCode')}. Last logs: {_container_logs(container)}")
		now = time.time()
		if status == 'running':
			up_since = up_since or now
			if now - up_since >= VERIFY_STABLE_SECONDS:
				debug_func(f"[VERIFY_CONTAINER] ✅ Container {container_name} stayed up for {VERIFY_STABLE_SECONDS}s")
				return
		else:
			up_since = None
		if now >= deadline:
			raise Exception(f"Container did not stay running (status: {status}). Last logs: {_container_logs(container)}")
		time.sleep(VERIFY_POLL_SECONDS)


def _bind_target(bind):
	"""Where a `source:target[:mode]` bind lands in the container."""
	parts = str(bind).split(':')
	return parts[1] if len(parts) >= 2 else None


def _get_old_image_config(container):
	"""
	Returns the Config dict of the image the container was created from,
	or None when it cannot be resolved (e.g. the image is no longer present).
	"""
	try:
		image_attrs = container.image.attrs or {}
		return _get_dict(image_attrs, 'Config')
	except Exception:
		return None


def _strip_old_image_defaults(config, container_attrs, container):
	"""
	Removes from `config` every value that was inherited from the OLD image
	instead of being explicitly set by the user/compose (same approach as
	Watchtower's GetCreateConfig). Docker merges the image Config into the
	container Config at creation time, so anything that matches the old image
	default was NOT set by the user and must not be pinned on recreation;
	otherwise an update that changes ENTRYPOINT/CMD/ENV/HEALTHCHECK/... in the
	new image would leave the new container running stale values (boot loops).

	If the old image config cannot be resolved, `config` is left untouched
	(previous behaviour).
	"""
	image_config = _get_old_image_config(container)
	if image_config is None:
		return

	# Entrypoint/Cmd: inherit from the new image when they match the old
	# image defaults. Cmd is only cleared when Entrypoint is also inherited:
	# a user-defined entrypoint changes the meaning of Cmd.
	if _normalize_command(config['entrypoint']) == _normalize_command(image_config.get('Entrypoint')):
		config['entrypoint'] = None
		if _normalize_command(config['command']) == _normalize_command(image_config.get('Cmd')):
			config['command'] = None

	# Env: drop variables that came verbatim from the old image (PATH,
	# version pins, etc.). Variables the user overrode have a different
	# value and are kept.
	image_env = set(_get_list(image_config, 'Env'))
	config['environment'] = [env for env in config['environment'] if env not in image_env]

	# Labels: drop label pairs that came verbatim from the old image
	# (LABEL instructions in its Dockerfile). Compose/user labels are kept.
	image_labels = _get_dict(image_config, 'Labels')
	config['labels'] = {k: v for k, v in config['labels'].items() if image_labels.get(k) != v}

	# User/WorkingDir/StopSignal: inherit when identical to the old image
	# default ('' and None are equivalent for Docker).
	if (_get_val(container_attrs, 'User') or '') == (image_config.get('User') or ''):
		config['user'] = None
	if (config['working_dir'] or '') == (image_config.get('WorkingDir') or ''):
		config['working_dir'] = None
	if (config['stop_signal'] or '') == (image_config.get('StopSignal') or ''):
		config['stop_signal'] = None

	# Healthcheck: when it is exactly the old image's HEALTHCHECK, inherit
	# the new image's one. A healthcheck defined in compose differs from the
	# image default and is kept.
	if config['healthcheck'] is not None and config['healthcheck'] == image_config.get('Healthcheck'):
		config['healthcheck'] = None


# Endpoint settings, as named by extract_container_config, mapped to the
# keyword arguments Network.connect() expects.
_ENDPOINT_TO_CONNECT_KWARGS = {
	'ipv4_address': 'ipv4_address',
	'ipv6_address': 'ipv6_address',
	'aliases': 'aliases',
	'links': 'links',
	'link_local_ips': 'link_local_ips',
	'driver_opts': 'driver_opt',
	# Given back as reported, assigned or set by hand —a DHCP reservation on
	# macvlan—: Docker copies no other network's into Config.MacAddress, and
	# /compose never writes these, so nothing gets pinned (unlike the main
	# network's; see _primary_mac_to_keep). A container stopped after running
	# reports none; one never started, only those set by hand.
	'mac_address': 'mac_address',
}


def _main_network_name(container, host_config, network_settings):
	"""
	The container's network mode, with a network given by its id named.

	`docker run --network <id>` keeps the id as the network mode, and so does
	Compose 2.29, while the networks it is attached to are reported by name:
	the main one then matched none of them, and its static IP, aliases and
	MAC were lost on every update. A container never started reports no
	network ids, so then the daemon is asked.
	"""
	mode = _get_val(host_config, 'NetworkMode')
	networks = _get_dict(network_settings, 'Networks')
	if not mode or len(mode) < 12 or ':' in mode:
		return mode
	# Asked for with endpoint options, Docker lists it twice: under the id as
	# well as under the name (see _id_named_duplicates). With more than one
	# network the mode is then the full id, which both start with.
	duplicates = _id_named_duplicates(networks)
	for name, endpoint in networks.items():
		network_id = _get_val(endpoint, 'NetworkID') or ''
		if name != mode and name not in duplicates and network_id.startswith(mode):
			return name
	if mode in networks:
		return mode
	try:
		name = container.client.api.inspect_network(mode)['Name']
	except Exception:
		return mode
	return name if name in networks else mode


def _id_named_duplicates(networks):
	"""
	The endpoints Docker lists a second time under the network's id.

	`docker run --network <short id> --ip ...` has Docker 25 and later report
	the network under that id and under its name. Created again with both,
	the container was attached twice to the same network, would not start,
	and the old one —which Docker itself no longer restarts— was left
	stopped. The name stays.
	"""
	names_by_id = {}
	for name, endpoint in networks.items():
		network_id = _get_val(endpoint, 'NetworkID')
		if network_id:
			names_by_id.setdefault(network_id, []).append(name)
	duplicates = set()
	for network_id, names in names_by_id.items():
		by_id = {name for name in names if len(name) >= 12 and network_id.startswith(name)}
		if by_id and len(by_id) < len(names):
			duplicates |= by_id
	return duplicates


def _api_version(container):
	"""The API version the container's client agreed with its daemon, or None."""
	version = getattr(getattr(getattr(container, 'client', None), 'api', None), '_version', None)
	return version if isinstance(version, str) else None


def _derived_from_ipv4(mac, endpoint):
	"""
	Whether a MAC is the one Docker 25 to 27 assign from the endpoint's IPv4
	address: 02:42 and its four octets.

	Given back at create, it outlived the address it came from. A container
	on a network without a fixed IP got another address from the update and
	kept the old one's MAC, and the next container to get that address was
	assigned the same MAC: two of them answering for it on the network.
	Docker 28 assigns them at random, and older ones ignore it at create.
	"""
	try:
		octets = [int(octet) for octet in (_get_val(endpoint, 'IPAddress') or '').split('.')]
	except ValueError:
		return False
	if len(octets) != 4 or not mac:
		return False
	return mac.lower() == '02:42:' + ':'.join(f'{octet:02x}' for octet in octets)


def _assigned_from_ipv4(container, mac, endpoint):
	"""_derived_from_ipv4, on the daemons that derive it (API 1.44 to 1.47)."""
	version = _api_version(container)
	if version is None or docker.utils.version_lt(version, '1.44') or not docker.utils.version_lt(version, '1.48'):
		return False
	return _derived_from_ipv4(mac, endpoint)


def _mac_set_on_a_stopped_container(container):
	"""
	The main network's MAC set by hand on a stopped container, on Docker 29.

	Stopped, a container reports no MAC on its endpoints, and from API 1.52
	there is no Config.MacAddress either: a stopped Pi-hole on macvlan lost
	its DHCP reservation on the first update. The daemon still gives the
	field to clients of 1.51, from what was set by hand. None when it says
	nothing, or no longer serves 1.51.
	"""
	version = _api_version(container)
	if version is None or docker.utils.version_lt(version, '1.52'):
		return None
	try:
		api = container.client.api
		old_view = api._result(api._get(f"{api.base_url}/v1.51/containers/{container.id}/json"), json=True)
	except Exception:
		return None
	return _get_val(_get_dict(old_view, 'Config'), 'MacAddress') or None


def _primary_mac_to_keep(config, api_version):
	"""
	The main network's MAC to give the new container, or None to let Docker
	assign one.

	Given back as it was —set by hand, or assigned— so an update does not
	change it. Except one Docker assigned, on Docker 25 to 28 (API 1.44 to
	1.51): those copy any MAC given at create into Config.MacAddress, where it
	then reads as set by hand, and /compose wrote it out as the container's
	`mac_address`. On those, Config.MacAddress says which it was —there only
	when set by hand—, and an assigned one is left to Docker, which gives
	another the way `docker compose up` does on every recreate. Before 1.44
	the container's own MAC is what counts (see the create), and from Docker
	29 (API 1.52) nothing is copied there.

	By the API version the client agreed with the daemon, not by whether the
	field is in the inspect: it is missing both for an assigned MAC on Docker
	28 and for any MAC on Docker 29.

	A container an update already pinned this way on 25 to 28 cannot be told
	from one set by hand, and keeps its MAC: recreating it once from its
	compose is what unpins it.
	"""
	if not isinstance(api_version, str):
		return config.get('network_mac_address')
	if docker.utils.version_lt(api_version, '1.44'):
		# The container's own MAC carries it there (see the create).
		return config.get('network_mac_address')
	# A stopped container reports no MAC on its endpoints, while its
	# Config.MacAddress still holds the one set by hand on Docker 25 to 28:
	# it was lost from the update of a stopped Pi-hole on macvlan.
	mac = config.get('network_mac_address') or config.get('mac_address')
	if config.get('mac_address_unsaid'):
		# Config.MacAddress stays empty even for one set by hand, and says
		# nothing (see extract_container_config). Kept, as the create keeps
		# the id, so nothing gets copied this time either. Docker 25 to 27
		# make theirs from the address, and those are left out already.
		return mac
	copies_into_config = docker.utils.version_lt(api_version, '1.52')
	if mac and copies_into_config and not config.get('mac_address'):
		return None
	return mac


def _extra_endpoints_at_create(client, config):
	"""
	Every network beyond the main one, as endpoint settings for the create.

	From API 1.44 (Docker 25) a container can be created on all of its
	networks at once, as `docker run --network a --network b` and compose
	do. Connected afterwards instead, Docker 25 to 28 ignore the MAC asked
	for: one set by hand on a second network —a DHCP reservation— came back
	different from every update. And the container starts with every
	interface, never with just the main one.

	Only when the main network is among those reported: under `default` the
	daemon names it `bridge`, and the two could not be told apart. What is
	left out is still connected afterwards by _connect_extra_networks, which
	skips whatever is already attached.
	"""
	networks = config.get('networks') or {}
	primary = config.get('network_mode')
	if len(networks) <= 1 or primary not in networks:
		return {}
	endpoints = {}
	for network_name, endpoint in networks.items():
		if network_name == primary or str(network_name).lower() in ('host', 'none') or str(network_name).startswith('container:'):
			continue
		endpoint = endpoint or {}
		endpoint_config = client.api.create_endpoint_config(**{
			connect_key: endpoint[endpoint_key]
			for endpoint_key, connect_key in _ENDPOINT_TO_CONNECT_KWARGS.items() if endpoint.get(endpoint_key)})
		if endpoint.get('gw_priority'):
			# The same gateway priority the connect would give it.
			endpoint_config['GwPriority'] = endpoint['gw_priority']
		endpoints[network_name] = endpoint_config
	return endpoints


def _connect_extra_networks(client, new_container, config, debug_func, error_func):
	"""
	Attaches the container to every network beyond the primary one that the
	create did not already.

	Before API 1.44 (Docker 25) a create takes a single network, so a
	container sitting on several used to come back from an update attached to
	just one of them; from 1.44 they all go in at create (see
	_extra_endpoints_at_create), and this only connects what that left out.
	They are connected before the container is started, the same way
	`compose up` does it, so it comes up with all of its interfaces at once.

	Raises on failure: a half-connected container is worse than a rolled back
	update, and the caller restores the old one.
	"""
	networks = config.get('networks') or {}
	if len(networks) <= 1:
		return

	# Whatever create() already attached (the primary network, under whichever
	# name the engine reports it: 'bridge', 'default'...) must not be redone.
	try:
		new_container.reload()
		already_connected = set(_get_dict(_get_dict(new_container.attrs, 'NetworkSettings'), 'Networks'))
	except Exception as e:
		debug_func(f"[EXTRA_NETWORKS] Could not read the new container's networks: {e}")
		already_connected = {config.get('network_mode')}

	for network_name, endpoint in networks.items():
		if network_name in already_connected:
			continue
		if str(network_name).lower() in ('host', 'none') or str(network_name).startswith('container:'):
			continue

		connect_kwargs = {}
		for endpoint_key, connect_key in _ENDPOINT_TO_CONNECT_KWARGS.items():
			value = (endpoint or {}).get(endpoint_key)
			if value:
				connect_kwargs[connect_key] = value

		try:
			network = client.networks.get(network_name)
			gw_priority = (endpoint or {}).get('gw_priority')
			if gw_priority:
				# The request Network.connect() sends, plus the gateway
				# priority it has no argument for: without it every network
				# came back at 0, and the default route could move to another.
				api = client.api
				endpoint_config = api.create_endpoint_config(**connect_kwargs)
				endpoint_config['GwPriority'] = gw_priority
				response = api._post_json(api._url("/networks/{0}/connect", network.id),
											data={"Container": new_container.id, "EndpointConfig": endpoint_config})
				api._raise_for_status(response)
			else:
				network.connect(new_container.id, **connect_kwargs)
			debug_func(f"[EXTRA_NETWORKS] Reattached to network {network_name} ({connect_kwargs or 'no endpoint settings'})")
		except Exception as connect_error:
			error_func(f"[EXTRA_NETWORKS] Could not reattach to network {network_name}: {connect_error}")
			raise Exception(f"Failed to reattach network {network_name}: {connect_error}")


# HostConfig fields copied verbatim into the new container's request: GPUs
# (`--gpus`, compose's device reservations) and OCI annotations.
HOST_CONFIG_PASSTHROUGH = ('DeviceRequests', 'Annotations')


def _create_container(client, stop_timeout, exposed_ports, image, host_config_extra=None, **kwargs):
	"""
	containers.create(), plus what it has no argument for.

	The SDK's create() does not forward a stop timeout; it derives the exposed
	ports from the published ones, so a port that was only exposed has no way
	in; and some HostConfig fields it either does not know (Annotations) or
	only takes in a shape of its own (DeviceRequests, the GPUs of compose's
	`deploy.resources.reservations.devices`). Those are copied into the
	request as the daemon reported them. When the container has none of it,
	this is create() itself; otherwise it does what create() does inside,
	with all of that added.

	The healthcheck too, when it has a `start_interval`: the SDK rebuilds it
	from the fields it knows, and that one is not among them, so the new
	container went back to probing every `interval` while it started up and
	took that much longer to be healthy.
	"""
	healthcheck = kwargs.get('healthcheck')
	raw_healthcheck = healthcheck if isinstance(healthcheck, dict) and healthcheck.get('StartInterval') else None
	if not stop_timeout and not exposed_ports and not host_config_extra and raw_healthcheck is None:
		return client.containers.create(image, **kwargs)
	from docker.models.containers import _create_container_args
	kwargs['image'] = image
	kwargs.setdefault('command', None)
	kwargs['version'] = client.api._version
	create_kwargs = _create_container_args(kwargs)
	if stop_timeout:
		create_kwargs['stop_timeout'] = stop_timeout
	if exposed_ports:
		ports = list(create_kwargs.get('ports') or [])
		for port in exposed_ports:
			number, _, protocol = str(port).partition('/')
			entry = (number, protocol or 'tcp')
			if entry not in ports:
				ports.append(entry)
		create_kwargs['ports'] = ports
	for key, value in (host_config_extra or {}).items():
		create_kwargs['host_config'][key] = copy.deepcopy(value)
	if raw_healthcheck is None:
		response = client.api.create_container(**create_kwargs)
	else:
		# What api.create_container() does, with the healthcheck put back as
		# the daemon reported it once the SDK has built the request: it
		# rebuilds whatever it is handed, so there is no passing it in.
		api = client.api
		name = create_kwargs.pop('name', None)
		platform = create_kwargs.pop('platform', None)
		if create_kwargs.pop('use_config_proxy', True):
			create_kwargs['environment'] = api._proxy_configs.inject_proxy_environment(
				create_kwargs.get('environment')) or None
		container_config = api.create_container_config(**create_kwargs)
		container_config['Healthcheck'] = copy.deepcopy(raw_healthcheck)
		response = api.create_container_from_config(container_config, name, platform)
	return client.containers.get(response['Id'])


def _api_1_44_hostname_alias(container, container_attrs):
	"""
	The hostname, when the inspect lists it among the aliases without anyone
	having set it there.

	Only API 1.44 (Docker 25) does that: from 1.45 the inspect shows the
	aliases that were asked for, and up to 1.43 the hostname is no DNS name
	at all, so an alias with its value is one somebody set to make it one.
	Carried over, it would come back set by hand on every network, and
	/compose would write it under `aliases:`.
	"""
	api_version = _api_version(container)
	hostname = _get_val(container_attrs, 'Hostname')
	if not hostname or api_version is None:
		return ()
	if docker.utils.version_lt(api_version, '1.44') or not docker.utils.version_lt(api_version, '1.45'):
		return ()
	return (hostname,)


def extract_container_config(container, tag=None):
	"""
	Extract all configuration from a container for recreation.
	Returns a dictionary with all container settings.
	"""
	container_attrs = _get_dict(container.attrs, 'Config')
	host_config = _get_dict(container.attrs, 'HostConfig')
	network_settings = _get_dict(container.attrs, 'NetworkSettings')

	# Basic configuration
	config = {
		'command': _get_list(container_attrs, 'Cmd'),
		'environment': _get_list(container_attrs, 'Env'),
		'working_dir': _get_val(container_attrs, 'WorkingDir'),
		'entrypoint': _get_val(container_attrs, 'Entrypoint'),
		'user': _get_val(container_attrs, 'User', 'root'),
		'stdin_open': _get_val(container_attrs, 'OpenStdin', False),
		'tty': _get_val(container_attrs, 'Tty', False),
		'stop_signal': _get_val(container_attrs, 'StopSignal'),
		'labels': _get_dict(container_attrs, 'Labels'),
		'healthcheck': _get_val(container_attrs, 'Healthcheck'),
	}

	# Drop values inherited from the old image so the new image's defaults apply
	_strip_old_image_defaults(config, container_attrs, container)
	# And the image's own metadata, whatever its value. A container recreated
	# by 4.x got every label of its image copied onto it as if the user had
	# set it, the version among them, and once the image moved on they no
	# longer matched it: carried over on every update since, so a container
	# on d860cc34-ls92 still said c1012ade-ls54 — to the bot, to /compose, to
	# Portainer. Nobody sets these on a container meaning it.
	config['labels'] = {key: value for key, value in config['labels'].items()
						if not key.startswith(IMAGE_METADATA_LABEL_PREFIXES)}

	# compose's `stop_grace_period`. An image cannot set it, so it is always
	# the user's — and stop_container reads it on the next update.
	config['stop_timeout'] = _get_val(container_attrs, 'StopTimeout')

	# Volumes and mounts
	config['volumes'] = _get_list(host_config, 'Binds')
	config['ports'] = _get_dict(host_config, 'PortBindings')
	# `-P`: every exposed port published on a random one.
	config['publish_all_ports'] = _get_val(host_config, 'PublishAllPorts', False)
	# compose's `expose:` (or `--expose`): exposed without being published. The
	# ones the old image exposed are left to the new image, the same as its
	# other defaults, and the published ones are already in `ports`.
	image_exposed = set(_get_dict(_get_old_image_config(container), 'ExposedPorts'))
	config['exposed_ports'] = sorted(
		port for port in _get_dict(container_attrs, 'ExposedPorts')
		if port not in image_exposed and port not in config['ports'])
	# `--tmpfs` and compose's short `tmpfs:` land here; `--mount` and compose's
	# long syntax in HostConfig.Mounts, which is handed back to Docker exactly
	# as Docker gave it. It used to be rebuilt field by field through the SDK's
	# Mount type, which knows a subset of the spec: read-only was read under
	# the wrong name and came back writable, and a volume's `subpath`, a
	# bind's `create_host_path` and a tmpfs's `mode` were dropped. Passing the
	# spec through keeps whatever the daemon knows about, including what is
	# added to it later.
	config['tmpfs_mounts'] = dict(_get_dict(host_config, 'Tmpfs'))
	config['mounts_list'] = [copy.deepcopy(mount) for mount in _get_list(host_config, 'Mounts')
							if isinstance(mount, dict) and mount.get('Target')]
	# An anonymous volume declared as a mount — `--mount type=volume,dst=/data`,
	# or compose's short `- /var/lib/mysql`, which compose sends as one — is in
	# that list with no Source, and handed back as it is it asks Docker for a
	# new, empty volume: a mariadb came back from every update with an empty
	# datadir and initialised a fresh database. The volume it got is named in
	# the container's Mounts, and the update creates the new container with
	# that name as the Source, as compose does when it recreates one. Kept
	# apart from `mounts_list`, which the compose generator writes out: `- /data`
	# is what the user declared, not a random 64-character name.
	sourceless = {mount['Target'] for mount in config['mounts_list']
				  if mount.get('Type') == 'volume' and not mount.get('Source')}
	config['mount_volume_names'] = {
		_get_val(mount, 'Destination'): _get_val(mount, 'Name') for mount in _get_list(container.attrs, 'Mounts')
		if _get_val(mount, 'Type') == 'volume' and _get_val(mount, 'Name') and _get_val(mount, 'Destination') in sourceless}

	# Anonymous volumes: `-v /data`, or a VOLUME in the image nobody mapped —
	# the database directory of postgres, mariadb or mongo, more often than
	# not. They appear in neither Binds nor HostConfig.Mounts, so the new
	# container got a fresh, empty one and the data stayed behind in a volume
	# nothing used any more. `docker compose up` carries them over to the
	# recreated container; so does this, by name.
	#
	# Kept apart from `volumes`: the update adds them to the new container,
	# and the compose generator only declares the ones the user asked for —
	# a volume the image declares is the image's to create, and a random
	# 64-character name is no use in a docker-compose.
	claimed = {target for target in (_bind_target(bind) for bind in config['volumes']) if target}
	claimed |= {mount['Target'] for mount in config['mounts_list']}
	claimed |= set(config['tmpfs_mounts'])
	# `volumes_from`. What it brings in is in the Mounts list too, unmarked,
	# and is the other container's: claimed here, or it would be bound a
	# second time and /compose would declare it as a volume of this one.
	#
	# Compose records the donor by id, and an update gives the donor a new
	# one: the next update of this container failed on a container that no
	# longer existed, every time. So a donor that is there is referred to by
	# name, which a recreation keeps; one that is gone is dropped, and the
	# volumes it lent — still mounted here — are carried over by name below
	# like any other, which is the same data.
	config['volumes_from'] = []
	for source in _get_list(host_config, 'VolumesFrom'):
		reference, _, mode = str(source).partition(':')
		try:
			donor = container.client.containers.get(reference)
		except docker.errors.NotFound:
			continue
		except Exception:
			config['volumes_from'].append(source)
			continue
		claimed |= {_get_val(m, 'Destination') for m in _get_list(donor.attrs, 'Mounts')}
		config['volumes_from'].append(f"{donor.name}:{mode}" if mode else donor.name)
	image_volumes = set(_get_dict(_get_old_image_config(container), 'Volumes'))
	config['anonymous_volumes'] = []
	config['anonymous_targets'] = []
	for mount in _get_list(container.attrs, 'Mounts'):
		destination = _get_val(mount, 'Destination')
		name = _get_val(mount, 'Name')
		if _get_val(mount, 'Type') != 'volume' or not name or not destination or destination in claimed:
			continue
		mode = 'rw' if _get_val(mount, 'RW', True) else 'ro'
		config['anonymous_volumes'].append(f"{name}:{destination}:{mode}")
		if destination not in image_volumes:
			config['anonymous_targets'].append(destination)

	# Network configuration
	config['network_mode'] = _main_network_name(container, host_config, network_settings)
	# The id it was given as, for the create to give it again (see
	# _primary_mac_to_keep). Everything else goes by the name.
	_given_mode = _get_val(host_config, 'NetworkMode')
	config['network_mode_by_id'] = _given_mode if _given_mode != config['network_mode'] else None
	# Docker 25 to 28 copy the main network's MAC into Config.MacAddress by
	# looking the network mode up among the endpoints: an id they are not
	# listed under finds nothing, and the field then says nothing, set by
	# hand or not. A short id they are listed under does find it.
	config['mac_address_unsaid'] = bool(config['network_mode_by_id']) and \
		config['network_mode_by_id'] not in _get_dict(network_settings, 'Networks')
	# Docker rejects hostname/domainname/mac_address when network_mode is host,
	# container:<id> or none ("conflicting options: hostname and the network mode").
	_nm = (config['network_mode'] or '').lower()
	_nm_conflicts_with_hostname = _nm == 'host' or _nm == 'none' or _nm.startswith('container:')
	# Don't pin the auto-generated hostname (the old container's short id):
	# the new container must get its own, like `docker compose up` would.
	_old_short_id = (container.id or '')[:12]
	_hostname = _get_val(container_attrs, 'Hostname')
	if _hostname == _old_short_id:
		_hostname = None
	config['hostname'] = None if _nm_conflicts_with_hostname else _hostname
	_implicit_aliases = {_old_short_id, *_api_1_44_hostname_alias(container, container_attrs)}
	config['domainname'] = None if _nm_conflicts_with_hostname else _get_val(container_attrs, 'Domainname')
	config['dns'] = _get_list(host_config, 'Dns')
	config['dns_opt'] = _get_list(host_config, 'DnsOptions')
	config['dns_search'] = _get_list(host_config, 'DnsSearch')
	config['extra_hosts'] = _get_list(host_config, 'ExtraHosts')
	# `--mac-address`, compose's `mac_address`. It is in Config: it used to be
	# read from HostConfig, which has no such field, so it was always None and
	# daemons that only take the container's MAC (see the create) lost it. It
	# also conflicts with host/container:/none network modes.
	config['mac_address'] = None if _nm_conflicts_with_hostname else (_get_val(container_attrs, 'MacAddress') or None)
	config['network_disabled'] = _get_val(host_config, 'NetworkDisabled', False)
	# In host/container:/none network modes port bindings are meaningless
	# (Docker ignores them and may emit warnings). Drop them.
	if _nm_conflicts_with_hostname:
		config['ports'] = {}

	# Network endpoint configuration - extract static IP, MAC, aliases, etc.
	ipv4_address = None
	ipv6_address = None
	network_aliases = None
	network_links = None
	network_driver_opts = None
	network_mac_address = None
	link_local_ips = None

	if network_settings and config['network_mode']:
		networks = _get_dict(network_settings, 'Networks')
		network_config = _get_dict(networks, config['network_mode'])
		if not network_config and config['network_mode'].lower() == 'default':
			# Docker 25 and older keep `default` as the mode and report the
			# endpoint as `bridge`. The create takes `default` back.
			network_config = _get_dict(networks, 'bridge')
		if network_config:
			# IPAM config for static IPs
			ipam_config = _get_dict(network_config, 'IPAMConfig')
			# Filter empty strings - Docker returns "" for empty addresses
			ipv4 = _get_val(ipam_config, 'IPv4Address')
			ipv6 = _get_val(ipam_config, 'IPv6Address')
			ipv4_address = ipv4 if ipv4 else None  # Convert "" to None
			ipv6_address = ipv6 if ipv6 else None  # Convert "" to None

			# Other endpoint configuration - filter empty values
			# Drop the alias with the old container's short id (added by the
			# engine on some versions); it would become a stale DNS name.
			aliases = _get_val(network_config, 'Aliases')
			if aliases:
				aliases = [a for a in aliases if a not in _implicit_aliases]
			network_aliases = aliases if aliases else None
			links = _link_pairs(_get_val(network_config, 'Links'))
			network_links = links if links else None
			driver_opts = _get_val(network_config, 'DriverOpts')
			network_driver_opts = driver_opts if driver_opts else None
			mac = _get_val(network_config, 'MacAddress')
			if _assigned_from_ipv4(container, mac, network_config):
				mac = None
			if not mac and not _nm_conflicts_with_hostname:
				mac = _mac_set_on_a_stopped_container(container)
			network_mac_address = mac if mac else None
			# Docker reports them only inside IPAMConfig: read from the
			# endpoint itself they were always empty and lost on update.
			link_local = _get_val(ipam_config, 'LinkLocalIPs')
			link_local_ips = link_local if link_local else None

	# Every network endpoint, not only the primary one. The create takes them
	# all from API 1.44 on (`_extra_endpoints_at_create`); older daemons only
	# take the primary, and `_connect_extra_networks` reattaches the rest. The
	# compose generator uses it to declare them all.
	config['networks'] = {}
	_duplicates = _id_named_duplicates(_get_dict(network_settings, 'Networks'))
	for _net_name, _net_config in _get_dict(network_settings, 'Networks').items():
		if _net_name in _duplicates:
			continue
		_endpoint = {}
		_ipam = _get_dict(_net_config, 'IPAMConfig')
		for _key, _api_key in (('ipv4_address', 'IPv4Address'), ('ipv6_address', 'IPv6Address'),
								('link_local_ips', 'LinkLocalIPs')):
			_value = _get_val(_ipam, _api_key)
			if _value:
				_endpoint[_key] = _value
		# The engine adds an alias with the container's short id on some
		# versions; it would become a stale DNS name.
		_aliases = [a for a in (_get_val(_net_config, 'Aliases') or []) if a not in _implicit_aliases]
		if _aliases:
			_endpoint['aliases'] = _aliases
		_links = _link_pairs(_get_val(_net_config, 'Links'))
		if _links:
			_endpoint['links'] = _links
		for _key, _api_key in (('driver_opts', 'DriverOpts'), ('gw_priority', 'GwPriority'),
								('mac_address', 'MacAddress')):
			_value = _get_val(_net_config, _api_key)
			if _key == 'mac_address' and _assigned_from_ipv4(container, _value, _net_config):
				continue
			if _value:
				_endpoint[_key] = _value
		config['networks'][_net_name] = _endpoint

	# For /compose, which writes Config.MacAddress: where that says nothing,
	# on Docker 25 to 27 the endpoint's MAC still tells, since theirs come
	# from the address and are left out above. A container Compose 2.29 had
	# created by the network's id lost its MAC from the second /compose on.
	_version = _api_version(container)
	_endpoint_tells = (_version is not None and not docker.utils.version_lt(_version, '1.44')
						and docker.utils.version_lt(_version, '1.48'))
	config['mac_address_by_hand'] = (network_mac_address if config['mac_address_unsaid'] and _endpoint_tells
									 else None)

	config['ipv4_address'] = ipv4_address
	config['ipv6_address'] = ipv6_address
	config['network_aliases'] = network_aliases
	config['network_links'] = network_links
	config['network_driver_opts'] = network_driver_opts
	config['network_mac_address'] = network_mac_address
	config['link_local_ips'] = link_local_ips

	# Resource limits
	config['restart_policy'] = _get_dict(host_config, 'RestartPolicy')
	# `--cpus` and compose's `cpus:` are stored as NanoCpus, not as a
	# quota/period pair: without it the CPU limit was lost on every recreation.
	config['nano_cpus'] = _get_val(host_config, 'NanoCpus')
	config['cpu_quota'] = _get_val(host_config, 'CpuQuota')
	config['cpu_period'] = _get_val(host_config, 'CpuPeriod')
	config['cpu_shares'] = _get_val(host_config, 'CpuShares')
	config['cpu_rt_period'] = _get_val(host_config, 'CpuRealtimePeriod')
	config['cpu_rt_runtime'] = _get_val(host_config, 'CpuRealtimeRuntime')
	config['cpuset_cpus'] = _get_val(host_config, 'CpusetCpus')
	config['cpuset_mems'] = _get_val(host_config, 'CpusetMems')
	config['mem_limit'] = _get_val(host_config, 'Memory')
	config['mem_reservation'] = _get_val(host_config, 'MemoryReservation')
	config['mem_swappiness'] = _get_val(host_config, 'MemorySwappiness')
	config['memswap_limit'] = _get_val(host_config, 'MemorySwap')
	config['kernel_memory'] = _get_val(host_config, 'KernelMemory')
	config['oom_kill_disable'] = _get_val(host_config, 'OomKillDisable', False)
	config['oom_score_adj'] = _get_val(host_config, 'OomScoreAdj')
	config['pids_limit'] = _get_val(host_config, 'PidsLimit')

	# Security
	config['privileged'] = _get_val(host_config, 'Privileged', False)
	config['cap_add'] = _get_list(host_config, 'CapAdd')
	config['cap_drop'] = _get_list(host_config, 'CapDrop')
	config['security_opt'] = _get_list(host_config, 'SecurityOpt')

	# Convert devices from API format to SDK format
	# API: [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/xvda", "CgroupPermissions": "rwm"}]
	# SDK: ["/dev/sda:/dev/xvda:rwm"]
	raw_devices = _get_list(host_config, 'Devices')
	config['devices'] = []
	for device in raw_devices:
		if isinstance(device, dict):
			host_path = _get_val(device, 'PathOnHost', '')
			container_path = _get_val(device, 'PathInContainer', '')
			perms = _get_val(device, 'CgroupPermissions', 'rwm')
			if host_path and container_path:
				config['devices'].append(f"{host_path}:{container_path}:{perms}")
		elif isinstance(device, str):
			# Already in correct format
			config['devices'].append(device)

	config['device_cgroup_rules'] = _get_list(host_config, 'DeviceCgroupRules')

	# I/O and storage
	config['blkio_weight'] = _get_val(host_config, 'BlkioWeight')
	config['blkio_weight_device'] = _get_list(host_config, 'BlkioWeightDevice')
	config['device_read_bps'] = _get_list(host_config, 'BlkioDeviceReadBps')
	config['device_read_iops'] = _get_list(host_config, 'BlkioDeviceReadIOps')
	config['device_write_bps'] = _get_list(host_config, 'BlkioDeviceWriteBps')
	config['device_write_iops'] = _get_list(host_config, 'BlkioDeviceWriteIOps')
	config['storage_opt'] = _get_dict(host_config, 'StorageOpt')
	config['log_config'] = _get_dict(host_config, 'LogConfig')
	config['shm_size'] = _get_val(host_config, 'ShmSize')

	# Namespaces and cgroups
	config['ipc_mode'] = _get_val(host_config, 'IpcMode')
	config['pid_mode'] = _get_val(host_config, 'PidMode')
	config['uts_mode'] = _get_val(host_config, 'UTSMode')
	config['userns_mode'] = _get_val(host_config, 'UsernsMode')
	config['cgroup_parent'] = _get_val(host_config, 'CgroupParent')
	config['cgroupns'] = _get_val(host_config, 'CgroupnsMode')

	# Other
	config['init'] = _get_val(host_config, 'Init', False)
	config['read_only'] = _get_val(host_config, 'ReadonlyRootfs', False)
	config['sysctls'] = _get_dict(host_config, 'Sysctls')
	config['ulimits'] = _get_list(host_config, 'Ulimits')
	config['group_add'] = _get_list(host_config, 'GroupAdd')
	config['links'] = _link_pairs(_get_list(host_config, 'Links'))
	# Handed back to the daemon untouched; see _create_container.
	config['host_config_extra'] = {key: copy.deepcopy(host_config[key])
									for key in HOST_CONFIG_PASSTHROUGH if host_config.get(key)}
	# `--security-opt systempaths=unconfined` is the CLI's: the daemon is
	# handed empty MaskedPaths and ReadonlyPaths instead, and that is all the
	# inspect output shows. Recreated without them, /proc came back masked —
	# and the option itself is refused by the API. Empty, not missing: missing
	# means Docker's defaults.
	for key in ('MaskedPaths', 'ReadonlyPaths'):
		if host_config.get(key) == [] and not config['privileged']:
			config['host_config_extra'][key] = []
	config['runtime'] = _get_val(host_config, 'Runtime')

	# Image
	image_with_tag = _get_val(container_attrs, 'Image', '')
	if tag:
		image_with_tag = f'{image_repository(image_with_tag)}:{tag}'
	config['image'] = image_with_tag
	config['platform'] = container_platform(container)

	# Status
	# Not 'created': a container that was never started is not started by
	# updating it either.
	STATES_TO_STOP = ['running', 'restarting', 'paused']
	config['is_running'] = container.status in STATES_TO_STOP
	# Comes back paused: it has to run for the update to be verified, and is
	# paused again once it is.
	config['was_paused'] = container.status == 'paused'

	return config


def _mounts_for_create(config):
	"""
	`mounts_list` as the new container is created with: a copy, with the
	anonymous volumes it had given back by name (see `mount_volume_names`).
	"""
	names = config.get('mount_volume_names') or {}
	mounts = []
	for mount in config.get('mounts_list') or []:
		mount = copy.deepcopy(mount)
		if mount.get('Type') == 'volume' and not mount.get('Source') and names.get(mount.get('Target')):
			mount['Source'] = names[mount['Target']]
		mounts.append(mount)
	return mounts


def _name_if_still_there(client, container, fallback_name):
	"""
	The name a container goes by now, or None once Docker has deleted it.

	A delete that timed out on the client may still finish on the daemon. One
	whose fate cannot be checked counts as still there: being told to remove
	something already gone costs a look, while a leftover nobody mentions
	stops the next update.
	"""
	try:
		return client.containers.get(container.id).name or fallback_name
	except docker.errors.NotFound:
		return None
	except Exception:
		return fallback_name


def _report_updated(config, container_name, left_behind, get_text_func, save_status_func, notify_func,
					debug_func, error_func):
	"""
	The end of an update the new container went through.

	Returns exactly get_text("updated_container"): the caller tells success
	from failure by comparing with it. An original that could not be deleted
	is therefore said in a message of its own.
	"""
	debug_func(f"[UPDATE_SUCCESS] ✅ Update completed successfully for {container_name}")
	# The container now runs the image it was just updated to, so it is up
	# to date. The status is stored as a boolean and rendered for display
	# later, in whichever language is configured at that point. Not storing
	# it only means the next check says it again.
	try:
		save_status_func(config['image'], container_name, False)
	except Exception as e:
		error_func(f"[UPDATE_SUCCESS] Could not save the update status of {container_name}: {e}")
	if left_behind:
		error_func(f"[DELETE_OLD] ⚠️ {container_name} was updated; the original is left as {left_behind} and has to be removed by hand")
		if notify_func is not None:
			try:
				notify_func(get_text_func("update_old_left_behind", container_name, left_behind))
			except Exception as e:
				error_func(f"[DELETE_OLD] Could not tell that {left_behind} was left behind: {e}")
	return get_text_func("updated_container", container_name)


def perform_update(client, container, config, container_name, message, edit_message_func,
				   debug_func, error_func, get_text_func, save_status_func,
				   container_id_length, telegram_group, skip_pull=False, notify_func=None):
	"""
	Perform the actual container update with the extracted configuration.
	Uses a lock to prevent concurrent updates of the same container.

	Args:
		client: Docker client
		container: Current container object
		config: Configuration dictionary from extract_container_config()
		container_name: Name of the container
		message: Telegram message object for updates
		edit_message_func: Function to edit Telegram messages
		debug_func: Debug logging function
		error_func: Error logging function
		get_text_func: Text translation function
		save_status_func: Function to save update status
		container_id_length: Length of container ID to display
		telegram_group: Telegram group ID
		skip_pull: When True, skip the image pull step. Used for in-place
			recreation with the same image (e.g. when a dependent must be
			recreated to point at a new parent container id).
		notify_func: Called with a text for the user when the update went
			through but the original could not be deleted, and is left under
			another name. The returned text is the plain success either way.

	Returns:
		str: Success or error message
	"""
	# Claim this container to prevent concurrent updates
	if not _start_updating(container.id):
		error_msg = f"Container {container_name} is already being updated. Please wait."
		debug_func(f"[UPDATE_START] ❌ {error_msg}")
		error_func(error_msg)
		return error_msg

	try:
		return _perform_update_locked(client, container, config, container_name, message, edit_message_func,
									   debug_func, error_func, get_text_func, save_status_func,
									   container_id_length, telegram_group, skip_pull=skip_pull,
									   notify_func=notify_func)
	finally:
		_done_updating(container.id)


def _perform_update_locked(client, container, config, container_name, message, edit_message_func,
						   debug_func, error_func, get_text_func, save_status_func,
						   container_id_length, telegram_group, skip_pull=False, notify_func=None):
	"""
	Internal function that performs the actual update (called with lock held).
	"""
	new_container = None
	old_container_name = f'{container_name}_old'
	old_container_id = container.id[:container_id_length]

	debug_func(f"[UPDATE_START] Container: {container_name} (ID: {old_container_id})")
	debug_func(f"[UPDATE_START] Old container will be named: {old_container_name}")

	# A container already called <name>_old is almost always what an update cut
	# short left behind. The rename below would fail on it, so nothing is
	# touched: that leftover may be the copy the user needs to recover, which
	# is theirs to look at and not ours to delete.
	try:
		leftover = client.containers.get(old_container_name)
	except docker.errors.NotFound:
		leftover = None
	except Exception as e:
		debug_func(f"[UPDATE_START] Could not check for {old_container_name}: {e}")
		leftover = None
	if leftover is not None and leftover.id != container.id:
		error_func(f"[UPDATE_START] ❌ {old_container_name} already exists (ID: {leftover.id[:container_id_length]}); not touching {container_name}")
		return get_text_func("error_update_leftover_old", container_name, old_container_name)

	# `--rm`: Docker deletes the container the moment it stops, so the stop
	# below would destroy the original and leave nothing to roll back to — a
	# failed update would lose it for good. Measured: that is what happened.
	if _get_dict(container.attrs, 'HostConfig').get('AutoRemove'):
		error_func(f"[UPDATE_START] ❌ {container_name} was started with --rm; not touching it")
		return get_text_func("error_update_auto_remove", container_name)

	# Whether the original was renamed to _old, so the rollback knows if there
	# is a name to give back.
	renamed = False
	new_container_id = None
	# Set once the new container has proved itself, and from then on it is
	# the one to keep. A failure past that point is housekeeping: rolling back
	# deleted a container that worked to bring back one that might be half
	# deleted. A DELETE that outlasted the client's timeout on a slow disk did
	# exactly that — the daemon then finished removing the original, and there
	# was no container left at all.
	verified = False
	# The name the original is left under when it could not be deleted.
	left_behind = None

	try:
		# Pull new image with timeout validation
		if skip_pull:
			debug_func(f"[PULL_IMAGE] Skipping pull for {container_name} (in-place recreation)")
		else:
			if message:
				edit_message_func(get_text_func("updating_pulling_image", container_name), telegram_group, message.message_id)

			try:
				debug_func(f"[PULL_IMAGE] Starting pull of {config['image']}")
				pulled_image = pull_checked(client, config['image'], platform=config.get('platform') or None)
				if not pulled_image or not pulled_image.id:
					raise Exception("Image pull returned invalid image object")
				debug_func(f"[PULL_IMAGE] Image pulled successfully: {pulled_image.id[:container_id_length]}")
			except Exception as pull_error:
				error_func(f"[PULL_IMAGE] ❌ Could not pull {config['image']}: {pull_error}")
				raise Exception(f"Failed to pull image {config['image']}: {pull_error}")

		# Stop container
		if message:
			edit_message_func(get_text_func("updating_stopping", container_name), telegram_group, message.message_id)
		debug_func(f"[STOP_CONTAINER] Stopping container {container_name} (ID: {old_container_id})")
		stop_container(container)
		debug_func(f"[STOP_CONTAINER] Container stopped successfully")

		# Rename to _old
		if message:
			edit_message_func(get_text_func("updating_renaming", container_name), telegram_group, message.message_id)
		debug_func(f"[RENAME_OLD] Renaming {container_name} (ID: {old_container_id}) to {old_container_name}")
		container.rename(old_container_name)
		renamed = True
		debug_func(f"[RENAME_OLD] Successfully renamed to {old_container_name}")

		# Create new container
		if message:
			edit_message_func(get_text_func("updating_creating", container_name), telegram_group, message.message_id)

		try:
			# Build networking config with EndpointConfig for static IP and network settings
			networking_config = None

			# For macvlan and similar networks, MAC should be in EndpointConfig, not in containers.create()
			# Otherwise the MAC gets lost
			effective_mac = None  # Will only be used for non-network-specific MAC
			# Except before API 1.44 (Docker 25), which has no MAC per endpoint:
			# those daemons ignore the endpoint's and only take the container's.
			# A Pi-hole on macvlan with a DHCP reservation, on a Synology with
			# Docker 24 or a Debian 12 with docker.io, got a random MAC from
			# every update, and with it another address.
			api_version = getattr(client.api, '_version', None)
			container_wide_mac = isinstance(api_version, str) and docker.utils.version_lt(api_version, '1.44')
			primary_mac = _primary_mac_to_keep(config, api_version)
			# As it was given: by name, or by id (see _primary_mac_to_keep).
			# docker-py drops the networking config unless its main network
			# goes under this same key.
			main_network = config.get('network_mode_by_id') or config['network_mode']
			has_network_config = (
				config['ipv4_address'] or config['ipv6_address'] or
				config['network_aliases'] or config['network_links'] or
				config['network_driver_opts'] or config['link_local_ips'] or
				primary_mac  # Include MAC in network config check
			)
			# From API 1.44 every network goes in at create; before it, only
			# the main one can, and the rest are connected afterwards.
			extra_endpoints = {} if container_wide_mac else _extra_endpoints_at_create(client, config)

			if config['network_mode'] and (has_network_config or extra_endpoints):
				from docker.types import EndpointConfig
				# Build endpoint config with all network parameters
				# EndpointConfig requires version parameter
				endpoint_kwargs = {'version': '1.44'}  # Docker API version

				if config['ipv4_address']:
					endpoint_kwargs['ipv4_address'] = config['ipv4_address']
				if config['ipv6_address']:
					endpoint_kwargs['ipv6_address'] = config['ipv6_address']
				if config['network_aliases']:
					endpoint_kwargs['aliases'] = config['network_aliases']
				if config['network_links']:
					endpoint_kwargs['links'] = config['network_links']
				if config['network_driver_opts']:
					endpoint_kwargs['driver_opt'] = config['network_driver_opts']
				if config['link_local_ips']:
					endpoint_kwargs['link_local_ips'] = config['link_local_ips']
				if primary_mac:
					# MAC address goes in EndpointConfig for network-specific MAC (e.g., macvlan)
					endpoint_kwargs['mac_address'] = primary_mac

				endpoint_config = EndpointConfig(**endpoint_kwargs)
				# Which network the default route goes through, when there are
				# several (compose's `gw_priority`). The SDK has no argument
				# for it, and EndpointConfig is the API's dict underneath.
				gw_priority = (config.get('networks') or {}).get(config['network_mode'], {}).get('gw_priority')
				if gw_priority:
					endpoint_config['GwPriority'] = gw_priority
				networking_config = {main_network: endpoint_config, **extra_endpoints}
				debug_func(f"[CREATE_CONTAINER] Network config: IPv4={config['ipv4_address']}, IPv6={config['ipv6_address']}, MAC={primary_mac}, aliases={config['network_aliases']}"
						   + (f", and at create too: {', '.join(extra_endpoints)}" if extra_endpoints else ""))
				if container_wide_mac and config['mac_address']:
					effective_mac = config['mac_address']
					debug_func(f"[CREATE_CONTAINER] Container MAC address: {effective_mac} (API {api_version} has no MAC per endpoint)")
			else:
				# Only use container-level MAC if there's no network-specific config
				effective_mac = config['mac_address']
				if effective_mac:
					debug_func(f"[CREATE_CONTAINER] Container MAC address: {effective_mac}")

			debug_func(f"[CREATE_CONTAINER] Creating new container with name: {container_name}")
			new_container = _create_container(
				client,
				config.get('stop_timeout'),
				config.get('exposed_ports'),
				config['image'],
				host_config_extra=config.get('host_config_extra'),
				platform=config.get('platform'),
				name=container_name,
				command=config['command'] if config['command'] else None,
				entrypoint=config['entrypoint'],
				environment=config['environment'],
				working_dir=config['working_dir'],
				user=config['user'],
				volumes=list(config['volumes'] or []) + list(config.get('anonymous_volumes') or []),
				mounts=_mounts_for_create(config) or None,
				# docker-py only applies networking_config when `network` is also
				# passed (_create_container_args drops it silently otherwise, losing
				# aliases, static IPs, links... - including the compose service-name
				# DNS alias other containers rely on). When `network` is set it also
				# becomes HostConfig.NetworkMode, so both paths keep the same mode.
				network=main_network if networking_config else None,
				network_mode=main_network,
				networking_config=networking_config,
				hostname=config['hostname'],
				domainname=config['domainname'],
				dns=config['dns'] if config['dns'] else None,
				dns_opt=config['dns_opt'] if config['dns_opt'] else None,
				dns_search=config['dns_search'] if config['dns_search'] else None,
				extra_hosts=config['extra_hosts'] if config['extra_hosts'] else None,
				mac_address=effective_mac,
				network_disabled=config['network_disabled'],
				stdin_open=config['stdin_open'],
				tty=config['tty'],
				stop_signal=config['stop_signal'],
				labels=config['labels'],
				healthcheck=config['healthcheck'],
				restart_policy=config['restart_policy'] if config['restart_policy'] else None,
				nano_cpus=config['nano_cpus'],
				cpu_quota=config['cpu_quota'],
				cpu_period=config['cpu_period'],
				cpu_shares=config['cpu_shares'],
				cpu_rt_period=config['cpu_rt_period'],
				cpu_rt_runtime=config['cpu_rt_runtime'],
				cpuset_cpus=config['cpuset_cpus'],
				cpuset_mems=config['cpuset_mems'],
				mem_limit=config['mem_limit'],
				mem_reservation=config['mem_reservation'],
				mem_swappiness=config['mem_swappiness'],
				memswap_limit=config['memswap_limit'],
				kernel_memory=config['kernel_memory'],
				oom_kill_disable=config['oom_kill_disable'],
				oom_score_adj=config['oom_score_adj'],
				pids_limit=config['pids_limit'],
				privileged=config['privileged'],
				cap_add=config['cap_add'] if config['cap_add'] else None,
				cap_drop=config['cap_drop'] if config['cap_drop'] else None,
				security_opt=config['security_opt'] if config['security_opt'] else None,
				devices=config['devices'] if config['devices'] else None,
				device_cgroup_rules=config['device_cgroup_rules'] if config['device_cgroup_rules'] else None,
				blkio_weight=config['blkio_weight'],
				blkio_weight_device=config['blkio_weight_device'] if config['blkio_weight_device'] else None,
				device_read_bps=config['device_read_bps'] if config['device_read_bps'] else None,
				device_read_iops=config['device_read_iops'] if config['device_read_iops'] else None,
				device_write_bps=config['device_write_bps'] if config['device_write_bps'] else None,
				device_write_iops=config['device_write_iops'] if config['device_write_iops'] else None,
				storage_opt=config['storage_opt'] if config['storage_opt'] else None,
				log_config=config['log_config'] if config['log_config'] else None,
				shm_size=config['shm_size'],
				ipc_mode=config['ipc_mode'],
				pid_mode=config['pid_mode'],
				uts_mode=config['uts_mode'],
				userns_mode=config['userns_mode'],
				cgroup_parent=config['cgroup_parent'],
				cgroupns=config.get('cgroupns'),
				init=config['init'] if config['init'] else None,
				read_only=config['read_only'],
				sysctls=config['sysctls'] if config['sysctls'] else None,
				ulimits=config['ulimits'] if config['ulimits'] else None,
				group_add=config['group_add'] if config['group_add'] else None,
				links=config['links'] if config['links'] else None,
				volumes_from=config['volumes_from'] if config['volumes_from'] else None,
				runtime=config['runtime'],
				tmpfs=config['tmpfs_mounts'] if config['tmpfs_mounts'] else None,
				ports=config['ports'] if config['ports'] else None,
				publish_all_ports=bool(config.get('publish_all_ports')),
			)
			new_container_id = new_container.id
			debug_func(f"[CREATE_CONTAINER] New container created successfully (ID: {new_container.id[:container_id_length]})")
		except Exception as create_error:
			error_func(get_text_func("error_creating_container", container_name, str(create_error)))
			raise Exception(f"Failed to create new container: {create_error}")

		# Reattach any network beyond the primary one before starting
		_connect_extra_networks(client, new_container, config, debug_func, error_func)

		# Start new container only if original was running
		if config['is_running']:
			if message:
				edit_message_func(get_text_func("updating_starting", container_name), telegram_group, message.message_id)

			try:
				debug_func(f"[START_CONTAINER] Starting new container {container_name} (ID: {new_container.id[:container_id_length]})")
				new_container.start()
				debug_func(f"[START_CONTAINER] New container started successfully")
			except Exception as start_error:
				error_func(f"[START_CONTAINER] ❌ Could not start the new {container_name}: {start_error}")
				raise Exception(f"Failed to start new container: {start_error}")
		else:
			debug_func(f"[START_CONTAINER] Original container was not running, keeping new container stopped")

		# Verify container state - CRITICAL: Only delete old container after verification
		debug_func(f"[VERIFY_CONTAINER] Starting verification of new container {container_name} (ID: {new_container.id[:container_id_length]})")

		if config['is_running']:
			# Container should be running - verify it stays up. Running at the
			# first look is not enough: a process that dies a moment after
			# starting was seen running, the original was deleted, and there
			# was nothing left to roll back to. It has to stay up for
			# VERIFY_STABLE_SECONDS in a row, without Docker restarting it.
			_verify_stays_up(new_container, container_name, debug_func)
			verified = True
			debug_func(f"[DELETE_OLD] New container verified and running. Now safe to delete old container {old_container_name}")
			if config.get('was_paused'):
				try:
					new_container.pause()
					debug_func(f"[VERIFY_CONTAINER] Paused again, as the original was")
				except Exception as pause_error:
					debug_func(f"[VERIFY_CONTAINER] Could not pause the new container: {pause_error}")
		else:
			# Container was stopped - just verify it exists
			try:
				new_container.reload()
				debug_func(f"[VERIFY_CONTAINER] ✅ Container {container_name} created successfully (kept stopped as original)")
			except docker.errors.NotFound:
				raise Exception("Container was removed by external process during verification")
			verified = True
			debug_func(f"[DELETE_OLD] New container verified. Now safe to delete old container {old_container_name}")

		# Save old image ID BEFORE deleting container (container object becomes invalid after delete)
		old_image_id = None
		try:
			old_image_id = container.image.id
		except Exception as e:
			debug_func(f"[DELETE_OLD] Warning: Could not get old image ID: {e}")

		# Delete old container. Whatever goes wrong here leaves the new one in
		# place, and not only an APIError: a DELETE that outlasts the client's
		# timeout is a requests ReadTimeout. If the original is still there —
		# the daemon may yet finish deleting it, or leave it Dead because
		# something else holds its files — it stays under its _old name for
		# the user to remove.
		try:
			if message:
				edit_message_func(get_text_func("updating_deleting_old", container_name), telegram_group, message.message_id)
			debug_func(f"[DELETE_OLD] Removing old container {old_container_name} (ID: {old_container_id})")
			container.remove()
			debug_func(f"[DELETE_OLD] ✅ Old container {old_container_name} deleted successfully")
		except Exception as e:
			error_func(get_text_func("error_deleting_container_with_error", old_container_name, e))
			left_behind = _name_if_still_there(client, container, old_container_name)

		# Delete old image (using saved ID, not container.image.id which is now invalid).
		# In skip_pull mode the "old" image is the same image the new container
		# was just created from, so attempting to remove it would fail (or worse,
		# leave dangling references); skip the delete entirely. So does an
		# original that is still there, which is still using it.
		if old_image_id and not skip_pull and not left_behind:
			try:
				debug_func(f"[DELETE_IMAGE] Removing old image {old_image_id[:container_id_length]}")
				client.images.remove(old_image_id)
				debug_func(f"[DELETE_IMAGE] ✅ Old image deleted successfully")
			except Exception as e:
				debug_func(f"[DELETE_IMAGE] Warning: Could not delete old image: {e}")

		return _report_updated(config, container_name, left_behind, get_text_func, save_status_func,
							   notify_func, debug_func, error_func)

	except Exception as e:
		if verified:
			# Nothing to roll back to: the new container is the good one. Only
			# the original is in question, which may or may not have gone.
			error_func(f"[UPDATE_SUCCESS] ⚠️ {container_name} was updated, but what came after failed: {e}")
			return _report_updated(config, container_name, _name_if_still_there(client, container, old_container_name),
								   get_text_func, save_status_func, notify_func, debug_func, error_func)

		# Rollback with validation - CRITICAL: Must restore old container
		debug_func(f"[ROLLBACK_START] ❌ Update failed with exception: {str(e)}")
		debug_func(get_text_func("debug_rollback_update", container_name))
		rollback_successful = False

		try:
			# STEP 1: Clean up new container FIRST (to free up the name)
			debug_func(f"[ROLLBACK_STEP1] Cleaning up new container (if it exists)")
			if new_container is not None:
				try:
					debug_func(f"[ROLLBACK_STEP1] New container exists (ID: {new_container.id[:container_id_length]})")
					debug_func(f"[ROLLBACK_STEP1] Reloading new container state...")
					try:
						new_container.reload()
					except docker.errors.NotFound:
						debug_func(f"[ROLLBACK_STEP1] New container already removed by external process")
						new_container = None

					if new_container is not None:
						debug_func(f"[ROLLBACK_STEP1] New container status: {new_container.status}")
						if new_container.status not in ['exited', 'dead']:
							try:
								debug_func(f"[ROLLBACK_STEP1] Stopping new container with 10s timeout...")
								new_container.stop(timeout=10)
								debug_func(f"[ROLLBACK_STEP1] New container stopped successfully")
							except Exception as stop_error:
								debug_func(f"[ROLLBACK_STEP1] Could not stop new container: {stop_error}")
						debug_func(f"[ROLLBACK_STEP1] Removing new container with force=True...")
						new_container.remove(force=True)
						debug_func(f"[ROLLBACK_STEP1] ✅ Failed new container {container_name} removed successfully")
				except Exception as cleanup_error:
					error_func(f"[ROLLBACK_STEP1] ❌ Failed to clean up new container: {cleanup_error}")
					# Continue anyway - we need to restore the old container
			else:
				debug_func(f"[ROLLBACK_STEP1] New container was never created, skipping cleanup")

			# STEP 2: Restore the original, found by its id. By name it would be
			# whatever is called <name>_old right now, which is not necessarily
			# the container this update renamed — and restoring a stranger over
			# the original is how the original used to get deleted.
			debug_func(f"[ROLLBACK_STEP2] Attempting to restore original container (ID: {old_container_id})")
			try:
				try:
					original = client.containers.get(container.id)
				except docker.errors.NotFound:
					error_func(f"[ROLLBACK_STEP2] ❌ CRITICAL: Original container {container_name} (ID: {old_container_id}) not found - CONTAINER LOST!")
					raise Exception(f"Original container {container_name} not found - cannot rollback. Container may be permanently lost!")

				if renamed or original.name != container_name:
					debug_func(f"[ROLLBACK_STEP2] Renaming {original.name} back to {container_name}")
					try:
						original.rename(container_name)
					except docker.errors.APIError as rename_error:
						# Only the container this update created may be moved out
						# of the way. Anything else holding the name is not ours.
						if "already in use" not in str(rename_error) or new_container_id is None:
							raise rename_error
						try:
							conflicting = client.containers.get(container_name)
						except docker.errors.NotFound:
							conflicting = None
						if conflicting is None or conflicting.id != new_container_id:
							error_func(f"[ROLLBACK_STEP2] ❌ {container_name} is taken by a container this update did not create; leaving it alone")
							raise rename_error
						debug_func(f"[ROLLBACK_STEP2] The name is held by the new container, removing it")
						conflicting.remove(force=True)
						original.rename(container_name)
					debug_func(f"[ROLLBACK_STEP2] ✅ Original renamed back to {container_name}")
				else:
					debug_func(f"[ROLLBACK_STEP2] Original was never renamed, it keeps its name")

				# Start it again if it was running before
				if config.get('was_paused') and original.status == 'paused':
					# Never stopped: the update failed before it got that far,
					# at the pull. It is as it was, and start() on a paused
					# container fails, which used to be logged as a failed
					# rollback.
					rollback_successful = True
					debug_func(f"[ROLLBACK_STEP2] ✅ Original container was never stopped, it stays paused")
				elif config['is_running']:
					debug_func(f"[ROLLBACK_STEP2] Container was running before, starting it...")
					original.start()
					time.sleep(1)
					original.reload()
					debug_func(f"[ROLLBACK_STEP2] Original container status: {original.status}")
					if original.status == 'running':
						debug_func(get_text_func("debug_rollback_successful", container_name))
						debug_func(f"[ROLLBACK_STEP2] ✅ Rollback successful - original container is running")
						rollback_successful = True
						if config.get('was_paused'):
							# Back as it was: a container is paused for a reason.
							# Running is still better than lost, so a pause that
							# fails does not fail the rollback.
							try:
								original.pause()
								debug_func(f"[ROLLBACK_STEP2] Paused again, as it was")
							except Exception as pause_error:
								error_func(f"[ROLLBACK_STEP2] ⚠️ {container_name} is back but running: could not pause it again: {pause_error}")
					else:
						error_func(f"[ROLLBACK_STEP2] ❌ Original container failed to start after rollback. Status: {original.status}")
				else:
					rollback_successful = True
					debug_func(f"[ROLLBACK_STEP2] ✅ Original container restored (was not running before)")
			except Exception as rollback_error:
				error_func(f"[ROLLBACK_STEP2] ❌ CRITICAL: Failed to restore original container: {rollback_error}")

		except Exception as rollback_exception:
			error_func(f"[ROLLBACK_EXCEPTION] ❌ Critical error during rollback: {rollback_exception}")

		# Prepare error message
		if rollback_successful:
			error_msg = f"Update failed but rollback successful: {str(e)}"
			debug_func(f"[ROLLBACK_RESULT] ✅ Rollback was successful")
		else:
			error_msg = f"Update failed and rollback may have failed: {str(e)}"
			debug_func(f"[ROLLBACK_RESULT] ❌ Rollback FAILED - Container may be lost!")

		error_func(get_text_func("error_updating_container_with_error", container_name, error_msg))
		return get_text_func("error_updating_container", container_name)

