"""
Docker Container Update Logic
Handles the complex logic of updating a Docker container with a new image
while preserving all configuration, mounts, networks, and resources.
"""

import docker
import docker.types
import docker.errors
import time
import threading

# Global lock dictionary to prevent concurrent updates of the same container
_container_locks = {}
_locks_lock = threading.Lock()

def get_container_lock(container_id):
	"""Get or create a lock for a specific container to prevent concurrent updates."""
	with _locks_lock:
		if container_id not in _container_locks:
			_container_locks[container_id] = threading.Lock()
		return _container_locks[container_id]


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
		'stdin_open': _get_val(container_attrs, 'AttachStdin', False),
		'tty': _get_val(container_attrs, 'Tty', False),
		'stop_signal': _get_val(container_attrs, 'StopSignal'),
		'labels': _get_dict(container_attrs, 'Labels'),
		'healthcheck': _get_val(container_attrs, 'Healthcheck'),
	}

	# Drop values inherited from the old image so the new image's defaults apply
	_strip_old_image_defaults(config, container_attrs, container)

	# Volumes and mounts
	config['volumes'] = _get_list(host_config, 'Binds')
	config['ports'] = _get_dict(host_config, 'PortBindings')
	config['tmpfs_mounts'] = {}
	config['mounts_list'] = []

	mounts_list_raw = _get_list(host_config, 'Mounts')
	for mount in mounts_list_raw:
		mount_type = _get_val(mount, 'Type', '')
		target = _get_val(mount, 'Target', '')

		if mount_type == 'tmpfs':
			tmpfs_options = _get_dict(mount, 'TmpfsOptions')
			size_bytes = _get_val(tmpfs_options, 'SizeBytes', 0)
			config['tmpfs_mounts'][target] = f"size={size_bytes}" if size_bytes else ''
		else:
			try:
				mount_obj = docker.types.Mount(
					target=target,
					source=_get_val(mount, 'Source', ''),
					type=mount_type,
					read_only=_get_val(mount, 'RW', True) == False,
					propagation=_get_val(mount, 'Propagation'),
					labels=_get_val(mount, 'Labels')
				)
				config['mounts_list'].append(mount_obj)
			except Exception:
				pass

	# Network configuration
	config['network_mode'] = _get_val(host_config, 'NetworkMode')
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
	config['domainname'] = None if _nm_conflicts_with_hostname else _get_val(container_attrs, 'Domainname')
	config['dns'] = _get_list(host_config, 'Dns')
	config['dns_opt'] = _get_list(host_config, 'DnsOptions')
	config['dns_search'] = _get_list(host_config, 'DnsSearch')
	config['extra_hosts'] = _get_list(host_config, 'ExtraHosts')
	# mac_address also conflicts with host/container:/none network modes
	config['mac_address'] = None if _nm_conflicts_with_hostname else _get_val(host_config, 'MacAddress')
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
				aliases = [a for a in aliases if a != _old_short_id]
			network_aliases = aliases if aliases else None
			links = _get_val(network_config, 'Links')
			network_links = links if links else None
			driver_opts = _get_val(network_config, 'DriverOpts')
			network_driver_opts = driver_opts if driver_opts else None
			mac = _get_val(network_config, 'MacAddress')
			network_mac_address = mac if mac else None
			link_local = _get_val(network_config, 'LinkLocalIPs')
			link_local_ips = link_local if link_local else None

	config['ipv4_address'] = ipv4_address
	config['ipv6_address'] = ipv6_address
	config['network_aliases'] = network_aliases
	config['network_links'] = network_links
	config['network_driver_opts'] = network_driver_opts
	config['network_mac_address'] = network_mac_address
	config['link_local_ips'] = link_local_ips

	# Resource limits
	config['restart_policy'] = _get_dict(host_config, 'RestartPolicy')
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
	config['links'] = _get_list(host_config, 'Links')
	config['volumes_from'] = _get_list(host_config, 'VolumesFrom')
	config['runtime'] = _get_val(host_config, 'Runtime')

	# Image
	image_with_tag = _get_val(container_attrs, 'Image', '')
	if tag:
		image_with_tag = f'{image_with_tag.split(":")[0]}:{tag}'
	config['image'] = image_with_tag

	# Status
	STATES_TO_STOP = ['running', 'restarting', 'paused', 'created']
	config['is_running'] = container.status in STATES_TO_STOP

	return config


def perform_update(client, container, config, container_name, message, edit_message_func,
				   debug_func, error_func, get_text_func, save_status_func,
				   container_id_length, telegram_group, skip_pull=False):
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

	Returns:
		str: Success or error message
	"""
	# Acquire lock for this container to prevent concurrent updates
	container_lock = get_container_lock(container.id)

	if not container_lock.acquire(blocking=False):
		error_msg = f"Container {container_name} is already being updated. Please wait."
		debug_func(f"[UPDATE_START] ❌ {error_msg}")
		error_func(error_msg)
		return error_msg

	try:
		return _perform_update_locked(client, container, config, container_name, message, edit_message_func,
									   debug_func, error_func, get_text_func, save_status_func,
									   container_id_length, telegram_group, skip_pull=skip_pull)
	finally:
		container_lock.release()


def _perform_update_locked(client, container, config, container_name, message, edit_message_func,
						   debug_func, error_func, get_text_func, save_status_func,
						   container_id_length, telegram_group, skip_pull=False):
	"""
	Internal function that performs the actual update (called with lock held).
	"""
	new_container = None
	old_container_name = f'{container_name}_old'
	old_container_id = container.id[:container_id_length]

	debug_func(f"[UPDATE_START] Container: {container_name} (ID: {old_container_id})")
	debug_func(f"[UPDATE_START] Old container will be named: {old_container_name}")

	try:
		# Pull new image with timeout validation
		if skip_pull:
			debug_func(f"[PULL_IMAGE] Skipping pull for {container_name} (in-place recreation)")
		else:
			if message:
				edit_message_func(get_text_func("updating_pulling_image", container_name), telegram_group, message.message_id)

			try:
				debug_func(f"[PULL_IMAGE] Starting pull of {config['image']}")
				pulled_image = client.images.pull(config['image'])
				if not pulled_image or not pulled_image.id:
					raise Exception("Image pull returned invalid image object")
				debug_func(f"[PULL_IMAGE] Image pulled successfully: {pulled_image.id[:container_id_length]}")
			except Exception as pull_error:
				error_func(get_text_func("error_pulling_image", config['image'], str(pull_error)))
				raise Exception(f"Failed to pull image {config['image']}: {pull_error}")

		# Stop container
		if message:
			edit_message_func(get_text_func("updating_stopping", container_name), telegram_group, message.message_id)
		debug_func(f"[STOP_CONTAINER] Stopping container {container_name} (ID: {old_container_id})")
		container.stop()
		debug_func(f"[STOP_CONTAINER] Container stopped successfully")

		# Rename to _old
		if message:
			edit_message_func(get_text_func("updating_renaming", container_name), telegram_group, message.message_id)
		debug_func(f"[RENAME_OLD] Renaming {container_name} (ID: {old_container_id}) to {old_container_name}")
		container.rename(old_container_name)
		debug_func(f"[RENAME_OLD] Successfully renamed to {old_container_name}")

		# Create new container
		if message:
			edit_message_func(get_text_func("updating_creating", container_name), telegram_group, message.message_id)

		try:
			# Build networking config with EndpointConfig for static IP and network settings
			networking_config = None
			has_network_config = (
				config['ipv4_address'] or config['ipv6_address'] or
				config['network_aliases'] or config['network_links'] or
				config['network_driver_opts'] or config['link_local_ips'] or
				config['network_mac_address']  # Include MAC in network config check
			)

			# For macvlan and similar networks, MAC should be in EndpointConfig, not in containers.create()
			# Otherwise the MAC gets lost
			effective_mac = None  # Will only be used for non-network-specific MAC

			if config['network_mode'] and has_network_config:
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
				if config['network_mac_address']:
					# MAC address goes in EndpointConfig for network-specific MAC (e.g., macvlan)
					endpoint_kwargs['mac_address'] = config['network_mac_address']

				endpoint_config = EndpointConfig(**endpoint_kwargs)
				networking_config = {config['network_mode']: endpoint_config}
				debug_func(f"[CREATE_CONTAINER] Network config: IPv4={config['ipv4_address']}, IPv6={config['ipv6_address']}, MAC={config['network_mac_address']}, aliases={config['network_aliases']}")
			else:
				# Only use container-level MAC if there's no network-specific config
				effective_mac = config['mac_address']
				if effective_mac:
					debug_func(f"[CREATE_CONTAINER] Container MAC address: {effective_mac}")

			debug_func(f"[CREATE_CONTAINER] Creating new container with name: {container_name}")
			new_container = client.containers.create(
				config['image'],
				name=container_name,
				command=config['command'] if config['command'] else None,
				entrypoint=config['entrypoint'],
				environment=config['environment'],
				working_dir=config['working_dir'],
				user=config['user'],
				volumes=config['volumes'],
				mounts=config['mounts_list'] if config['mounts_list'] else None,
				# docker-py only applies networking_config when `network` is also
				# passed (_create_container_args drops it silently otherwise, losing
				# aliases, static IPs, links... - including the compose service-name
				# DNS alias other containers rely on). When `network` is set it also
				# becomes HostConfig.NetworkMode, so both paths keep the same mode.
				network=config['network_mode'] if networking_config else None,
				network_mode=config['network_mode'],
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
				init=config['init'],
				read_only=config['read_only'],
				sysctls=config['sysctls'] if config['sysctls'] else None,
				ulimits=config['ulimits'] if config['ulimits'] else None,
				group_add=config['group_add'] if config['group_add'] else None,
				links=config['links'] if config['links'] else None,
				volumes_from=config['volumes_from'] if config['volumes_from'] else None,
				runtime=config['runtime'],
				tmpfs=config['tmpfs_mounts'] if config['tmpfs_mounts'] else None,
				ports=config['ports'] if config['ports'] else None,
			)
			debug_func(f"[CREATE_CONTAINER] New container created successfully (ID: {new_container.id[:container_id_length]})")
		except Exception as create_error:
			error_func(get_text_func("error_creating_container", container_name, str(create_error)))
			raise Exception(f"Failed to create new container: {create_error}")

		# Start new container only if original was running
		if config['is_running']:
			if message:
				edit_message_func(get_text_func("updating_starting", container_name), telegram_group, message.message_id)

			try:
				debug_func(f"[START_CONTAINER] Starting new container {container_name} (ID: {new_container.id[:container_id_length]})")
				new_container.start()
				debug_func(f"[START_CONTAINER] New container started successfully")
			except Exception as start_error:
				error_func(get_text_func("error_starting_container", container_name, str(start_error)))
				raise Exception(f"Failed to start new container: {start_error}")
		else:
			debug_func(f"[START_CONTAINER] Original container was not running, keeping new container stopped")

		# Verify container state - CRITICAL: Only delete old container after verification
		debug_func(f"[VERIFY_CONTAINER] Starting verification of new container {container_name} (ID: {new_container.id[:container_id_length]})")

		if config['is_running']:
			# Container should be running - verify it starts correctly
			max_retries = 5
			retry_count = 0
			while retry_count < max_retries:
				try:
					debug_func(f"[VERIFY_CONTAINER] Attempt {retry_count + 1}/{max_retries}: Reloading container state")
					try:
						new_container.reload()
					except docker.errors.NotFound:
						raise Exception("Container was removed by external process during verification")

					debug_func(f"[VERIFY_CONTAINER] Container status: {new_container.status}")
					if new_container.status == 'running':
						debug_func(f"[VERIFY_CONTAINER] ✅ Container {container_name} is running successfully")
						break
					elif new_container.status in ['exited', 'dead']:
						debug_func(f"[VERIFY_CONTAINER] ❌ Container exited with status: {new_container.status}")
						try:
							debug_func(f"[VERIFY_CONTAINER] Attempting to retrieve logs...")
							logs = new_container.logs(tail=50).decode('utf-8', errors='ignore')
							debug_func(f"[VERIFY_CONTAINER] Logs retrieved successfully")
						except Exception as log_error:
							debug_func(f"[VERIFY_CONTAINER] Could not retrieve logs: {log_error}")
							logs = f"[Could not retrieve logs: {log_error}]"
						raise Exception(f"Container exited immediately. Last logs: {logs}")
					retry_count += 1
					if retry_count < max_retries:
						debug_func(f"[VERIFY_CONTAINER] Container not ready yet, waiting 1 second...")
						time.sleep(1)
				except Exception as e:
					if retry_count >= max_retries - 1:
						debug_func(f"[VERIFY_CONTAINER] ❌ Max retries reached. Raising exception: {e}")
						raise Exception(f"Container failed to reach running state: {e}")
					debug_func(f"[VERIFY_CONTAINER] Exception on attempt {retry_count + 1}: {e}. Retrying...")
					retry_count += 1
					time.sleep(1)
			debug_func(f"[DELETE_OLD] New container verified and running. Now safe to delete old container {old_container_name}")
		else:
			# Container was stopped - just verify it exists
			try:
				new_container.reload()
				debug_func(f"[VERIFY_CONTAINER] ✅ Container {container_name} created successfully (kept stopped as original)")
			except docker.errors.NotFound:
				raise Exception("Container was removed by external process during verification")
			debug_func(f"[DELETE_OLD] New container verified. Now safe to delete old container {old_container_name}")

		# Save old image ID BEFORE deleting container (container object becomes invalid after delete)
		old_image_id = None
		try:
			old_image_id = container.image.id
		except Exception as e:
			debug_func(f"[DELETE_OLD] Warning: Could not get old image ID: {e}")

		# Delete old container
		try:
			if message:
				edit_message_func(get_text_func("updating_deleting_old", container_name), telegram_group, message.message_id)
			debug_func(f"[DELETE_OLD] Removing old container {old_container_name} (ID: {old_container_id})")
			container.remove()
			debug_func(f"[DELETE_OLD] ✅ Old container {old_container_name} deleted successfully")
		except docker.errors.APIError as e:
			error_func(get_text_func("error_deleting_container_with_error", container_name, e))
			raise Exception(f"Failed to delete old container: {e}")

		# Delete old image (using saved ID, not container.image.id which is now invalid).
		# In skip_pull mode the "old" image is the same image the new container
		# was just created from, so attempting to remove it would fail (or worse,
		# leave dangling references); skip the delete entirely.
		if old_image_id and not skip_pull:
			try:
				debug_func(f"[DELETE_IMAGE] Removing old image {old_image_id[:container_id_length]}")
				client.images.remove(old_image_id)
				debug_func(f"[DELETE_IMAGE] ✅ Old image deleted successfully")
			except Exception as e:
				debug_func(f"[DELETE_IMAGE] Warning: Could not delete old image: {e}")

		# Save status
		debug_func(f"[UPDATE_SUCCESS] ✅ Update completed successfully for {container_name}")
		save_status_func(config['image'], container_name, get_text_func("UPDATED_CONTAINER_TEXT"))
		return get_text_func("updated_container", container_name)

	except Exception as e:
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

			# STEP 2: Restore old container
			debug_func(f"[ROLLBACK_STEP2] Attempting to restore old container {old_container_name}")
			try:
				debug_func(f"[ROLLBACK_STEP2] Getting old container by name: {old_container_name}")
				try:
					old_container = client.containers.get(old_container_name)
				except docker.errors.NotFound:
					error_func(f"[ROLLBACK_STEP2] ❌ CRITICAL: Old container {old_container_name} not found - CONTAINER LOST!")
					raise Exception(f"Old container {old_container_name} not found - cannot rollback. Container may be permanently lost!")

				debug_func(f"[ROLLBACK_STEP2] ✅ Found old container {old_container_name} (ID: {old_container.id[:container_id_length]})")

				# Rename back to original name
				try:
					debug_func(f"[ROLLBACK_STEP2] Renaming {old_container_name} back to {container_name}")
					old_container.rename(container_name)
					debug_func(f"[ROLLBACK_STEP2] ✅ Old container renamed back to {container_name}")
				except docker.errors.APIError as rename_error:
					# If rename fails due to conflict, try to remove the conflicting container first
					if "already in use" in str(rename_error):
						debug_func(f"[ROLLBACK_STEP2] ⚠️ Name conflict detected: {rename_error}")
						debug_func(f"[ROLLBACK_STEP2] Attempting to resolve conflict...")
						try:
							debug_func(f"[ROLLBACK_STEP2] Getting conflicting container with name {container_name}")
							conflicting = client.containers.get(container_name)
							debug_func(f"[ROLLBACK_STEP2] Found conflicting container (ID: {conflicting.id[:container_id_length]})")
							debug_func(f"[ROLLBACK_STEP2] Removing conflicting container with force=True...")
							conflicting.remove(force=True)
							debug_func(f"[ROLLBACK_STEP2] ✅ Removed conflicting container")
							debug_func(f"[ROLLBACK_STEP2] Retrying rename of {old_container_name} to {container_name}")
							old_container.rename(container_name)
							debug_func(f"[ROLLBACK_STEP2] ✅ Old container renamed back to {container_name} after conflict resolution")
						except Exception as conflict_error:
							error_func(f"[ROLLBACK_STEP2] ❌ Failed to resolve name conflict: {conflict_error}")
							raise rename_error
					else:
						raise rename_error

				# Start old container if it was running before
				if config['is_running']:
					debug_func(f"[ROLLBACK_STEP2] Container was running before, starting it...")
					old_container.start()
					debug_func(f"[ROLLBACK_STEP2] Old container start command sent, waiting 1 second...")
					# Verify old container started
					time.sleep(1)
					debug_func(f"[ROLLBACK_STEP2] Reloading old container state...")
					old_container.reload()
					debug_func(f"[ROLLBACK_STEP2] Old container status: {old_container.status}")
					if old_container.status == 'running':
						debug_func(get_text_func("debug_rollback_successful", container_name))
						debug_func(f"[ROLLBACK_STEP2] ✅ Rollback successful - old container is running")
						rollback_successful = True
					else:
						error_func(f"[ROLLBACK_STEP2] ❌ Old container failed to start after rollback. Status: {old_container.status}")
				else:
					rollback_successful = True
					debug_func(f"[ROLLBACK_STEP2] ✅ Old container restored (was not running before)")
			except Exception as rollback_error:
				error_func(f"[ROLLBACK_STEP2] ❌ CRITICAL: Failed to restore old container: {rollback_error}")
				# Try one more time with force
				try:
					debug_func(f"[ROLLBACK_STEP2_FORCE] Attempting force restore of {old_container_name}")
					old_container = client.containers.get(old_container_name)
					debug_func(f"[ROLLBACK_STEP2_FORCE] Found old container, attempting force cleanup...")
					# Try to remove any conflicting container
					try:
						debug_func(f"[ROLLBACK_STEP2_FORCE] Checking for conflicting container {container_name}")
						conflicting = client.containers.get(container_name)
						debug_func(f"[ROLLBACK_STEP2_FORCE] Found conflicting container, removing...")
						conflicting.remove(force=True)
						debug_func(f"[ROLLBACK_STEP2_FORCE] Conflicting container removed")
					except Exception:
						debug_func(f"[ROLLBACK_STEP2_FORCE] No conflicting container found or already removed")
					debug_func(f"[ROLLBACK_STEP2_FORCE] Renaming old container...")
					old_container.rename(container_name)
					if config['is_running']:
						debug_func(f"[ROLLBACK_STEP2_FORCE] Starting old container...")
						old_container.start()
					rollback_successful = True
					debug_func(f"[ROLLBACK_STEP2_FORCE] ✅ Force restore successful")
				except Exception as force_error:
					error_func(f"[ROLLBACK_STEP2_FORCE] ❌ CRITICAL: Force restore also failed: {force_error}")

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

