"""
Docker Container Update Logic
Handles the complex logic of updating a Docker container with a new image
while preserving all configuration, mounts, networks, and resources.
"""

import docker
import docker.types


def extract_container_config(container, tag=None):
	"""
	Extract all configuration from a container for recreation.
	Returns a dictionary with all container settings.
	"""
	container_attrs = container.attrs.get('Config', {})
	host_config = container.attrs.get('HostConfig', {})
	network_settings = container.attrs.get('NetworkSettings', {})

	# Basic configuration
	config = {
		'command': container_attrs.get('Cmd', []),
		'environment': container_attrs.get('Env', []),
		'working_dir': container_attrs.get('WorkingDir', None),
		'entrypoint': container_attrs.get('Entrypoint', None),
		'user': container_attrs.get('User', 'root'),
		'stdin_open': container_attrs.get('AttachStdin', False),
		'tty': container_attrs.get('Tty', False),
		'stop_signal': container_attrs.get('StopSignal', None),
		'labels': container_attrs.get('Labels', {}),
		'healthcheck': container_attrs.get('Healthcheck', None),
	}

	# Volumes and mounts
	config['volumes'] = host_config.get('Binds', []) if host_config else []
	config['ports'] = host_config.get('PortBindings', {}) if host_config else {}
	config['tmpfs_mounts'] = {}
	config['mounts_list'] = []

	if host_config and 'Mounts' in host_config:
		for mount in host_config.get('Mounts', []):
			mount_type = mount.get('Type', '')
			target = mount.get('Target', '')

			if mount_type == 'tmpfs':
				tmpfs_options = mount.get('TmpfsOptions', {})
				size_bytes = tmpfs_options.get('SizeBytes', 0)
				config['tmpfs_mounts'][target] = f"size={size_bytes}" if size_bytes else ''
			else:
				try:
					mount_obj = docker.types.Mount(
						target=target,
						source=mount.get('Source', ''),
						type=mount_type,
						read_only=mount.get('RW', True) == False,
						propagation=mount.get('Propagation', None),
						labels=mount.get('Labels', None)
					)
					config['mounts_list'].append(mount_obj)
				except Exception as e:
					pass

	# Network configuration
	config['network_mode'] = host_config.get('NetworkMode', None) if host_config else None
	config['hostname'] = container_attrs.get('Hostname', None)
	config['domainname'] = container_attrs.get('Domainname', None)
	config['dns'] = host_config.get('Dns', []) if host_config else []
	config['dns_opt'] = host_config.get('DnsOptions', []) if host_config else []
	config['dns_search'] = host_config.get('DnsSearch', []) if host_config else []
	config['extra_hosts'] = host_config.get('ExtraHosts', []) if host_config else []
	config['mac_address'] = host_config.get('MacAddress', None) if host_config else None
	config['network_disabled'] = host_config.get('NetworkDisabled', False) if host_config else False

	# IPAM configuration
	ipv4_address = None
	ipv6_address = None
	if network_settings and config['network_mode']:
		ipam_config = network_settings.get('Networks', {}).get(config['network_mode'], {}).get('IPAMConfig', {})
		if ipam_config:
			ipv4_address = ipam_config.get('IPv4Address', None)
			ipv6_address = ipam_config.get('IPv6Address', None)
	config['ipv4_address'] = ipv4_address
	config['ipv6_address'] = ipv6_address

	# Resource limits
	config['restart_policy'] = host_config.get('RestartPolicy', {}) if host_config else {}
	config['cpu_quota'] = host_config.get('CpuQuota', None) if host_config else None
	config['cpu_period'] = host_config.get('CpuPeriod', None) if host_config else None
	config['cpu_shares'] = host_config.get('CpuShares', None) if host_config else None
	config['cpu_rt_period'] = host_config.get('CpuRealtimePeriod', None) if host_config else None
	config['cpu_rt_runtime'] = host_config.get('CpuRealtimeRuntime', None) if host_config else None
	config['cpuset_cpus'] = host_config.get('CpusetCpus', None) if host_config else None
	config['cpuset_mems'] = host_config.get('CpusetMems', None) if host_config else None
	config['mem_limit'] = host_config.get('Memory', None) if host_config else None
	config['mem_reservation'] = host_config.get('MemoryReservation', None) if host_config else None
	config['mem_swappiness'] = host_config.get('MemorySwappiness', None) if host_config else None
	config['memswap_limit'] = host_config.get('MemorySwap', None) if host_config else None
	config['kernel_memory'] = host_config.get('KernelMemory', None) if host_config else None
	config['oom_kill_disable'] = host_config.get('OomKillDisable', False) if host_config else False
	config['oom_score_adj'] = host_config.get('OomScoreAdj', None) if host_config else None
	config['pids_limit'] = host_config.get('PidsLimit', None) if host_config else None

	# Security
	config['privileged'] = host_config.get('Privileged', False) if host_config else False
	config['cap_add'] = host_config.get('CapAdd', []) if host_config else []
	config['cap_drop'] = host_config.get('CapDrop', []) if host_config else []
	config['security_opt'] = host_config.get('SecurityOpt', []) if host_config else []
	config['devices'] = host_config.get('Devices', []) if host_config else []
	config['device_cgroup_rules'] = host_config.get('DeviceCgroupRules', []) if host_config else []

	# I/O and storage
	config['blkio_weight'] = host_config.get('BlkioWeight', None) if host_config else None
	config['blkio_weight_device'] = host_config.get('BlkioWeightDevice', []) if host_config else []
	config['device_read_bps'] = host_config.get('BlkioDeviceReadBps', []) if host_config else []
	config['device_read_iops'] = host_config.get('BlkioDeviceReadIOps', []) if host_config else []
	config['device_write_bps'] = host_config.get('BlkioDeviceWriteBps', []) if host_config else []
	config['device_write_iops'] = host_config.get('BlkioDeviceWriteIOps', []) if host_config else []
	config['storage_opt'] = host_config.get('StorageOpt', {}) if host_config else {}
	config['log_config'] = host_config.get('LogConfig', {}) if host_config else {}
	config['shm_size'] = host_config.get('ShmSize', None) if host_config else None

	# Namespaces and cgroups
	config['ipc_mode'] = host_config.get('IpcMode', None) if host_config else None
	config['pid_mode'] = host_config.get('PidMode', None) if host_config else None
	config['uts_mode'] = host_config.get('UTSMode', None) if host_config else None
	config['userns_mode'] = host_config.get('UsernsMode', None) if host_config else None
	config['cgroup_parent'] = host_config.get('CgroupParent', None) if host_config else None
	config['cgroupns'] = host_config.get('CgroupnsMode', None) if host_config else None

	# Other
	config['init'] = host_config.get('Init', False) if host_config else False
	config['read_only'] = host_config.get('ReadonlyRootfs', False) if host_config else False
	config['sysctls'] = host_config.get('Sysctls', {}) if host_config else {}
	config['ulimits'] = host_config.get('Ulimits', []) if host_config else []
	config['group_add'] = host_config.get('GroupAdd', []) if host_config else []
	config['links'] = host_config.get('Links', []) if host_config else []
	config['volumes_from'] = host_config.get('VolumesFrom', []) if host_config else []
	config['runtime'] = host_config.get('Runtime', None) if host_config else None

	# Image
	image_with_tag = container_attrs.get('Image', '')
	if tag:
		image_with_tag = f'{image_with_tag.split(":")[0]}:{tag}'
	config['image'] = image_with_tag

	# Status
	STATES_TO_STOP = ['running', 'restarting', 'paused', 'created']
	config['is_running'] = container.status in STATES_TO_STOP

	return config


def perform_update(client, container, config, container_name, message, edit_message_func,
				   debug_func, error_func, get_text_func, save_status_func,
				   container_id_length, telegram_group):
	"""
	Perform the actual container update with the extracted configuration.

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

	Returns:
		str: Success or error message
	"""
	try:
		# Pull new image
		debug_func(get_text_func("debug_pulling_image", config['image']))
		if message:
			edit_message_func(get_text_func("updating_pulling_image", container_name), telegram_group, message.message_id)
		client.images.pull(config['image'])

		# Stop container
		debug_func(get_text_func("debug_stopping_container", container_name))
		if message:
			edit_message_func(get_text_func("updating_stopping", container_name), telegram_group, message.message_id)
		container.stop()

		# Rename to _old
		old_container_name = f'{container_name}_old'
		debug_func(get_text_func("debug_renaming_container", container_name, old_container_name))
		if message:
			edit_message_func(get_text_func("updating_renaming", container_name), telegram_group, message.message_id)
		container.rename(old_container_name)

		# Create new container
		debug_func(get_text_func("debug_creating_container", container_name))
		if message:
			edit_message_func(get_text_func("updating_creating", container_name), telegram_group, message.message_id)

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
			network_mode=config['network_mode'],
			hostname=config['hostname'],
			domainname=config['domainname'],
			dns=config['dns'] if config['dns'] else None,
			dns_opt=config['dns_opt'] if config['dns_opt'] else None,
			dns_search=config['dns_search'] if config['dns_search'] else None,
			extra_hosts=config['extra_hosts'] if config['extra_hosts'] else None,
			mac_address=config['mac_address'],
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

		# Start new container
		debug_func(get_text_func("debug_starting_container", container_name))
		if message:
			edit_message_func(get_text_func("updating_starting", container_name), telegram_group, message.message_id)
		new_container.start()

		# Delete old container
		try:
			debug_func(get_text_func("debug_container_deleting_old_container", container_name))
			if message:
				edit_message_func(get_text_func("updating_deleting_old", container_name), telegram_group, message.message_id)
			container.remove()
		except docker.errors.APIError as e:
			error_func(get_text_func("error_deleting_container_with_error", container_name, e))
			raise Exception(f"Failed to delete old container: {e}")

		# Delete old image
		debug_func(get_text_func("debug_deleting_image", config['image'].split(':')[0][:container_id_length]))
		try:
			client.images.remove(container.image.id)
		except Exception as e:
			debug_func(get_text_func("debug_image_can_not_be_deleted", container_name, e))

		# Save status
		save_status_func(config['image'], container_name, get_text_func("UPDATED_CONTAINER_TEXT"))
		return get_text_func("updated_container", container_name)

	except Exception as e:
		# Rollback
		debug_func(get_text_func("debug_rollback_update", container_name))
		try:
			old_container_name = f'{container_name}_old'
			try:
				old_container = client.containers.get(old_container_name)
				old_container.rename(container_name)
				if config['is_running']:
					old_container.start()
				debug_func(get_text_func("debug_rollback_successful", container_name))
			except:
				pass

			try:
				new_container.stop()
				new_container.remove()
			except:
				pass
		except:
			pass

		error_func(get_text_func("error_updating_container_with_error", container_name, e))
		return get_text_func("error_updating_container", container_name)

