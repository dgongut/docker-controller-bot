"""
Compose Generator
Turns the configuration extracted from a running container into a
docker-compose service definition.

The container configuration is NOT read here: it comes from
`docker_update.extract_container_config`, the same extractor used to recreate
containers on every update. That extractor already solves the hard parts
(dropping values inherited from the image, resolving network endpoints,
normalising devices, detecting hostname/network_mode conflicts), so this module
only has to translate its output into Compose Spec keys.
"""
import yaml

from docker_compose_manager import COMPOSE_PROJECT_LABEL


class _Quoted(str):
	"""
	A string that must always be emitted quoted.

	YAML 1.1 reads `22:22` as a base-60 number (1342), which silently corrupts
	port mappings. Quoting them keeps them strings for every parser.
	"""


class _ComposeDumper(yaml.SafeDumper):
	"""SafeDumper that knows how to emit _Quoted, without touching global state"""


_ComposeDumper.add_representer(
	_Quoted,
	lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'"),
)


# Engine defaults. A value equal to any of these was not chosen by the user, so
# writing it into the compose file would only add noise.
DEFAULT_SHM_SIZE = 67108864  # 64MB
DEFAULT_LOG_DRIVER = "json-file"
DEFAULT_IPC_MODES = ("", "private")
DEFAULT_RUNTIMES = ("", "runc")
DEFAULT_NETWORK_MODES = ("", "bridge", "default")
# Network modes that are written as `network_mode:` instead of a named network
LITERAL_NETWORK_MODES = ("host", "none")

# Labels managed by Docker Compose itself: they are re-created by `compose up`
# and pinning them by hand would be wrong.
COMPOSE_MANAGED_LABEL_PREFIX = "com.docker.compose."

# Reserved compose name for the network a project creates for itself
DEFAULT_NETWORK_KEY = "default"

# Restart policies that mean "no restart policy at all"
EMPTY_RESTART_POLICIES = ("", "no")


def _format_bytes(value):
	"""Bytes as the shortest exact compose suffix ('512m'), or the raw number"""
	try:
		value = int(value)
	except (TypeError, ValueError):
		return None
	if value <= 0:
		return None
	for suffix, unit in (("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024)):
		if value % unit == 0:
			return f"{value // unit}{suffix}"
	return str(value)


def _format_duration(nanoseconds):
	"""Nanoseconds (Docker API) as a compose duration string ('30s', '1m30s')"""
	try:
		nanoseconds = int(nanoseconds)
	except (TypeError, ValueError):
		return None
	if nanoseconds <= 0:
		return None
	if nanoseconds % 1000000000:
		milliseconds = nanoseconds // 1000000
		return f"{milliseconds}ms" if milliseconds else f"{nanoseconds}ns"
	seconds = nanoseconds // 1000000000
	minutes, seconds = divmod(seconds, 60)
	hours, minutes = divmod(minutes, 60)
	parts = []
	if hours:
		parts.append(f"{hours}h")
	if minutes:
		parts.append(f"{minutes}m")
	if seconds or not parts:
		parts.append(f"{seconds}s")
	return "".join(parts)


def _is_named_volume(source):
	"""Whether a bind source is a named volume instead of a host path"""
	return bool(source) and not source.startswith(("/", "./", "../", "~"))


class ComposeGenerator:
	"""
	Builds a docker-compose document for a single container.

	Usage:
		ComposeGenerator(service_name, config).to_yaml()

	where `config` is the dict returned by
	`docker_update.extract_container_config`.
	"""

	def __init__(self, service_name, config):
		self.service_name = service_name
		self.config = config or {}
		# Named volumes and networks referenced by the service, collected while
		# building it so they can be declared at the top level.
		self._named_volumes = set()
		self._named_networks = {}

	# ------------------------------------------------------------------
	# Public API
	# ------------------------------------------------------------------
	def build(self):
		"""Returns the compose document as a plain dict"""
		service = {"image": self.config.get("image", ""), "container_name": self.service_name}

		self._add_runtime(service)
		self._add_environment(service)
		self._add_labels(service)
		self._add_volumes(service)
		self._add_ports(service)
		self._add_networking(service)
		self._add_restart_policy(service)
		self._add_resources(service)
		self._add_security(service)
		self._add_devices(service)
		self._add_namespaces(service)
		self._add_logging(service)
		self._add_healthcheck(service)
		self._add_legacy(service)

		# No `version:` key: it is obsolete and current Compose warns about it.
		document = {"services": {self.service_name: service}}
		if self._named_volumes:
			document["volumes"] = {name: {"external": True} for name in sorted(self._named_volumes)}
		if self._named_networks:
			document["networks"] = dict(sorted(self._named_networks.items()))
		return document

	def to_yaml(self):
		"""Returns the compose document rendered as YAML"""
		return yaml.dump(
			self.build(),
			Dumper=_ComposeDumper,
			default_flow_style=False,
			sort_keys=False,
			allow_unicode=True,
		)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------
	def _set(self, service, key, value):
		"""Adds a key only when the value carries information"""
		if value in (None, "", [], {}, False):
			return
		service[key] = value

	def _get(self, key, default=None):
		value = self.config.get(key)
		return default if value in (None, "", [], {}) else value

	# ------------------------------------------------------------------
	# Sections
	# ------------------------------------------------------------------
	def _add_runtime(self, service):
		self._set(service, "entrypoint", self._get("entrypoint"))
		self._set(service, "command", self._get("command"))
		self._set(service, "user", self._get("user"))
		self._set(service, "working_dir", self._get("working_dir"))
		self._set(service, "hostname", self._get("hostname"))
		self._set(service, "domainname", self._get("domainname"))
		self._set(service, "stop_signal", self._get("stop_signal"))
		self._set(service, "stdin_open", self.config.get("stdin_open", False))
		self._set(service, "tty", self.config.get("tty", False))
		self._set(service, "init", self.config.get("init", False))
		self._set(service, "read_only", self.config.get("read_only", False))

	def _add_environment(self, service):
		"""
		Environment variables the user actually set. The extractor already
		removed everything inherited from the image (PATH, version pins...).
		"""
		environment = self._get("environment", [])
		if environment:
			service["environment"] = list(environment)

	def _add_labels(self, service):
		labels = self._get("labels", {})
		user_labels = {
			key: value for key, value in labels.items()
			if not key.startswith(COMPOSE_MANAGED_LABEL_PREFIX)
		}
		self._set(service, "labels", user_labels)

	def _add_volumes(self, service):
		"""
		Merges the two places Docker keeps volumes: `Binds` (docker run -v) and
		`Mounts` (compose and `--mount`). Deduplicated by target so a container
		listing the same volume in both does not get it twice.
		"""
		volumes = []
		seen_targets = set()

		for bind in self._get("volumes", []):
			parts = str(bind).split(":")
			if len(parts) < 2:
				continue
			source, target = parts[0], parts[1]
			if target in seen_targets:
				continue
			seen_targets.add(target)
			if _is_named_volume(source):
				self._named_volumes.add(source)
			volumes.append(str(bind))

		for mount in self._get("mounts_list", []):
			target = mount.get("Target")
			source = mount.get("Source")
			if not target or target in seen_targets:
				continue
			seen_targets.add(target)
			if _is_named_volume(source):
				self._named_volumes.add(source)
			entry = f"{source}:{target}" if source else target
			if mount.get("ReadOnly"):
				entry = f"{entry}:ro"
			volumes.append(entry)

		self._set(service, "volumes", volumes)

		tmpfs = []
		for target, options in self._get("tmpfs_mounts", {}).items():
			tmpfs.append(f"{target}:{options}" if options else target)
		self._set(service, "tmpfs", tmpfs)

	def _add_ports(self, service):
		"""
		Port bindings from the API format to compose short syntax.
		{'80/tcp': [{'HostIp': '', 'HostPort': '8080'}]}  ->  ['8080:80']
		"""
		ports = []
		for port_spec, bindings in self._get("ports", {}).items():
			container_port, _, protocol = str(port_spec).partition("/")
			for binding in bindings or []:
				host_port = binding.get("HostPort") or ""
				host_ip = binding.get("HostIp") or ""
				entry = f"{host_port}:{container_port}" if host_port else container_port
				# A wildcard host IP is the default, spelling it out adds nothing
				if host_ip and host_ip not in ("0.0.0.0", "::"):
					entry = f"{host_ip}:{entry}"
				if protocol and protocol != "tcp":
					entry = f"{entry}/{protocol}"
				if entry not in ports:
					ports.append(_Quoted(entry))
		self._set(service, "ports", ports)

	def _add_networking(self, service):
		"""
		`host`, `none` and `container:<id>` become `network_mode`. Anything else
		becomes named networks declared as external, each carrying its endpoint
		settings (static IP, aliases...) when it has any.
		"""
		network_mode = self._get("network_mode", "")
		mode = str(network_mode).lower()

		if mode in LITERAL_NETWORK_MODES or mode.startswith("container:"):
			service["network_mode"] = network_mode
		else:
			self._add_named_networks(service, mode)

		self._set(service, "mac_address", self._get("mac_address"))
		self._set(service, "dns", self._get("dns"))
		self._set(service, "dns_opt", self._get("dns_opt"))
		self._set(service, "dns_search", self._get("dns_search"))
		self._set(service, "extra_hosts", self._get("extra_hosts"))

	def _add_named_networks(self, service, mode):
		"""
		Declares every network the container is attached to, not just the
		primary one: a container on two networks would otherwise come back with
		a compose file that puts it on one.
		"""
		project_default = self._project_default_network()
		raw_networks = self._get("networks", {})

		networks = {}
		for name, endpoint in raw_networks.items():
			if str(name).lower() in LITERAL_NETWORK_MODES:
				continue
			endpoint = self._clean_endpoint(endpoint)
			# The `<project>_default` network is created by compose itself on
			# every `up`, so declaring it as external would be a lie. Its
			# endpoint settings still matter though: written under the reserved
			# `default` key they survive without pinning the network's name.
			# A static IP is the exception, it needs the network declared with
			# its subnet, so that one keeps the real name.
			if name == project_default and not endpoint.get("ipv4_address"):
				if endpoint:
					service.setdefault("networks", {})[DEFAULT_NETWORK_KEY] = endpoint
				continue
			networks[name] = endpoint

		# Fall back to the primary network only when the endpoint list is
		# missing altogether (older API versions). An empty list because
		# everything was filtered out means there is nothing to declare.
		if not raw_networks and mode not in DEFAULT_NETWORK_MODES and mode != project_default:
			endpoint = {}
			self._set(endpoint, "ipv4_address", self._get("ipv4_address"))
			self._set(endpoint, "ipv6_address", self._get("ipv6_address"))
			self._set(endpoint, "aliases", self._get("network_aliases"))
			self._set(endpoint, "link_local_ips", self._get("link_local_ips"))
			networks = {self._get("network_mode"): self._clean_endpoint(endpoint)}

		# A container sitting only on the default bridge needs no declaration
		if not networks or (len(networks) == 1 and not any(networks.values())
							and list(networks)[0].lower() in DEFAULT_NETWORK_MODES):
			if not service.get("networks"):
				service.pop("networks", None)
			return

		declared = service.get("networks") or {}
		if any(networks.values()) or declared:
			merged = dict(declared)
			merged.update({name: networks[name] for name in sorted(networks)})
			service["networks"] = merged
		else:
			service["networks"] = sorted(networks)
		for name in networks:
			self._named_networks[name] = {"external": True}

	def _project_default_network(self):
		"""Name of the default network compose creates for this container's project"""
		project = (self.config.get("labels") or {}).get(COMPOSE_PROJECT_LABEL)
		return f"{project}_default" if project else None

	def _clean_endpoint(self, endpoint):
		"""
		Drops duplicated aliases and the container's own name, which Docker adds
		back on its own.

		The compose service name is deliberately kept: in the generated file the
		service is named after the container, so dropping it would take away the
		DNS name the rest of the stack uses to reach this one.
		"""
		endpoint = dict(endpoint or {})
		aliases = endpoint.get("aliases")
		if not aliases:
			return endpoint
		implicit = {self.service_name}
		deduped = []
		for alias in aliases:
			if alias in implicit or alias in deduped:
				continue
			deduped.append(alias)
		if deduped:
			endpoint["aliases"] = deduped
		else:
			endpoint.pop("aliases")
		return endpoint

	def _add_restart_policy(self, service):
		policy = self._get("restart_policy", {})
		name = policy.get("Name") or ""
		if name in EMPTY_RESTART_POLICIES:
			return
		if name == "on-failure" and policy.get("MaximumRetryCount"):
			service["restart"] = f"on-failure:{policy['MaximumRetryCount']}"
		else:
			service["restart"] = name

	def _add_resources(self, service):
		self._set(service, "mem_limit", _format_bytes(self.config.get("mem_limit")))
		self._set(service, "mem_reservation", _format_bytes(self.config.get("mem_reservation")))
		self._set(service, "shm_size", self._shm_size())

		cpus = self._cpus()
		if cpus is not None:
			service["cpus"] = cpus

		self._set(service, "cpu_shares", self.config.get("cpu_shares") or None)
		self._set(service, "cpuset", self._get("cpuset_cpus"))
		self._set(service, "mem_swappiness", self._mem_swappiness())
		self._set(service, "oom_kill_disable", self.config.get("oom_kill_disable", False))
		self._set(service, "oom_score_adj", self.config.get("oom_score_adj") or None)
		pids_limit = self.config.get("pids_limit") or 0
		if pids_limit > 0:
			service["pids_limit"] = pids_limit

	def _cpus(self):
		"""
		Number of CPUs as compose expresses it. `--cpus` and compose's own
		`cpus:` are stored as NanoCpus; containers configured the old way carry
		a quota over a period instead.
		"""
		nano_cpus = self.config.get("nano_cpus") or 0
		if nano_cpus > 0:
			cpus = nano_cpus / 1000000000
		else:
			quota = self.config.get("cpu_quota") or 0
			period = self.config.get("cpu_period") or 0
			if quota <= 0 or period <= 0:
				return None
			cpus = quota / period
		return int(cpus) if cpus == int(cpus) else round(cpus, 3)

	def _shm_size(self):
		size = self.config.get("shm_size")
		if not size or size == DEFAULT_SHM_SIZE:
			return None
		return _format_bytes(size)

	def _mem_swappiness(self):
		# -1 means "not set"
		value = self.config.get("mem_swappiness")
		if value is None or value < 0:
			return None
		return value

	def _add_security(self, service):
		self._set(service, "privileged", self.config.get("privileged", False))
		self._set(service, "cap_add", self._get("cap_add"))
		self._set(service, "cap_drop", self._get("cap_drop"))
		self._set(service, "security_opt", self._get("security_opt"))
		self._set(service, "group_add", self._get("group_add"))
		self._set(service, "sysctls", self._get("sysctls"))
		self._set(service, "storage_opt", self._get("storage_opt"))

		# API: [{'Name': 'nofile', 'Soft': 1024, 'Hard': 2048}]
		# compose: {nofile: {soft: 1024, hard: 2048}}
		ulimits = {}
		for ulimit in self._get("ulimits", []):
			name = ulimit.get("Name")
			if not name:
				continue
			soft, hard = ulimit.get("Soft"), ulimit.get("Hard")
			if soft == hard and soft is not None:
				ulimits[name] = soft
			else:
				entry = {}
				if soft is not None:
					entry["soft"] = soft
				if hard is not None:
					entry["hard"] = hard
				ulimits[name] = entry
		self._set(service, "ulimits", ulimits)

	def _add_devices(self, service):
		self._set(service, "devices", self._get("devices"))
		self._set(service, "device_cgroup_rules", self._get("device_cgroup_rules"))
		self._set(service, "blkio_config", self._blkio_config())

	def _blkio_config(self):
		"""Groups the blkio weights and per-device limits under blkio_config"""
		blkio = {}
		weight = self.config.get("blkio_weight") or 0
		if weight:
			blkio["weight"] = weight
		mapping = (
			("weight_device", "blkio_weight_device"),
			("device_read_bps", "device_read_bps"),
			("device_read_iops", "device_read_iops"),
			("device_write_bps", "device_write_bps"),
			("device_write_iops", "device_write_iops"),
		)
		for compose_key, config_key in mapping:
			entries = []
			for entry in self._get(config_key, []):
				converted = {}
				if entry.get("Path"):
					converted["path"] = entry["Path"]
				if entry.get("Rate") is not None:
					converted["rate"] = entry["Rate"]
				if entry.get("Weight") is not None:
					converted["weight"] = entry["Weight"]
				if converted:
					entries.append(converted)
			if entries:
				blkio[compose_key] = entries
		return blkio

	def _add_namespaces(self, service):
		ipc_mode = self._get("ipc_mode", "")
		if str(ipc_mode).lower() not in DEFAULT_IPC_MODES:
			service["ipc"] = ipc_mode
		self._set(service, "pid", self._get("pid_mode"))
		self._set(service, "uts", self._get("uts_mode"))
		self._set(service, "userns_mode", self._get("userns_mode"))
		self._set(service, "cgroup_parent", self._get("cgroup_parent"))
		# Only `host` is worth writing: `private` is the daemon default
		if str(self._get("cgroupns", "")).lower() == "host":
			service["cgroup"] = "host"
		runtime = self._get("runtime", "")
		if str(runtime).lower() not in DEFAULT_RUNTIMES:
			service["runtime"] = runtime

	def _add_logging(self, service):
		"""Only when the driver or its options differ from the daemon default"""
		log_config = self._get("log_config", {})
		driver = log_config.get("Type") or ""
		options = log_config.get("Config") or {}
		if not driver or (driver == DEFAULT_LOG_DRIVER and not options):
			return
		logging = {"driver": driver}
		if options:
			logging["options"] = options
		service["logging"] = logging

	def _add_healthcheck(self, service):
		"""API durations are nanoseconds, compose wants '30s'-style strings"""
		healthcheck = self._get("healthcheck", {})
		if not healthcheck:
			return
		test = healthcheck.get("Test") or []
		if test == ["NONE"]:
			service["healthcheck"] = {"disable": True}
			return
		result = {}
		if test:
			result["test"] = list(test)
		for compose_key, api_key in (
			("interval", "Interval"),
			("timeout", "Timeout"),
			("start_period", "StartPeriod"),
			("start_interval", "StartInterval"),
		):
			duration = _format_duration(healthcheck.get(api_key))
			if duration:
				result[compose_key] = duration
		if healthcheck.get("Retries"):
			result["retries"] = healthcheck["Retries"]
		self._set(service, "healthcheck", result)

	def _add_legacy(self, service):
		"""Legacy options that would otherwise be silently dropped"""
		self._set(service, "links", self._get("links"))
		self._set(service, "volumes_from", self._get("volumes_from"))
