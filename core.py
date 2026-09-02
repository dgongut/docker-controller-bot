import docker
import functools
import hashlib
import html
import io
import json
import os
import re
import requests
import shlex
import sys
import telebot
import threading
import time
import uuid
import yaml
import migration
import store
from config import *
from croniter import croniter
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from compose_generator import ComposeGenerator
from docker_update import extract_container_config, perform_update
from docker_compose_manager import (
    ComposeDetector,
    ComposeProjectManager
)
from schedule_manager import ScheduleManager
from schedule_flow import (
    save_schedule_state, load_schedule_state, clear_schedule_state,
    init_add_schedule_state
)
from port_manager import PortManager
import callback_registry
import host_registry
from i18n import get_text, language
from logger import debug, error, warning
from message_queue import MessageQueue

VERSION = "5.0.0_fase2"

_unmute_timer = None
_mute_lock = threading.Lock()  # Lock for thread-safe mute timer operations
_cache_lock = threading.Lock()  # Lock for thread-safe cache operations
_menu_refresh_lock = threading.Lock()  # Serialises multi-action menu repaints

# How often the background daemons look at their settings again. Long waits are
# slept in steps of this size so a change made from /settings is picked up
# during the wait instead of after it.
UPDATE_CHECK_POLL_SECONDS = 60

def sizeof_fmt(num, suffix="B"):
	for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
		if abs(num) < 1024.0:
			return f"{num:3.1f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f}Yi{suffix}"



# Initial variable validation
if TELEGRAM_TOKEN is None or TELEGRAM_TOKEN == '':
	error("You need to configure the bot token with the TELEGRAM_TOKEN variable")
	sys.exit(1)
if TELEGRAM_ADMIN is None or TELEGRAM_ADMIN == '':
	error("You need to configure the chatId of the user who will interact with the bot with the TELEGRAM_ADMIN variable")
	sys.exit(1)
if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
	error("You cannot be anonymous to control the bot. In the variable TELEGRAM_ADMIN you have to put your user id.")
	sys.exit(1)
if CONTAINER_NAME is None or CONTAINER_NAME == '':
	error("Container name needs to be set in the CONTAINER_NAME variable")
	sys.exit(1)
if TELEGRAM_GROUP is None or TELEGRAM_GROUP == '':
	if len(str(TELEGRAM_ADMIN).split(',')) > 1:
		error("Multiple administrators can only be specified if used in a group (using the TELEGRAM_GROUP variable)")
		sys.exit(1)
	TELEGRAM_GROUP = TELEGRAM_ADMIN

try:
	TELEGRAM_THREAD = int(TELEGRAM_THREAD)
except:
	error(f"The variable TELEGRAM_THREAD is the thread within a supergroup, it is a numeric value. It has been set to {TELEGRAM_THREAD}.")
	sys.exit(1)

# Resolve storage and run the one-time migrations before anything reads a
# setting. On the first start after updating from 4.x this seeds the settings
# file from the environment, so every value the user had in their compose is
# the value they keep.
_migration = migration.run()
LOCAL_HOST_ID = _migration.host_id

def button_columns():
	"""
	How many buttons per row the container lists use.

	Clamped to what Telegram accepts, so a hand-edited settings file cannot
	produce a keyboard the API rejects.
	"""
	try:
		columns = int(store.get("bot.button_columns"))
	except (TypeError, ValueError):
		columns = 2
	return max(1, min(columns, 8))

def notification_channel():
	"""Chat id container status notifications go to, or None when unset."""
	configured = str(store.get("bot.notification_channel") or "").strip()
	return configured or None

# Instantiate the bot
bot = telebot.TeleBot(TELEGRAM_TOKEN, num_threads=8)

# Instantiate the ScheduleManager
schedule_manager = ScheduleManager(store.root(), store.SCHEDULES_FILE)

# Instantiate the global message queue
message_queue = MessageQueue(delay_between_messages=0.1, max_retries=5)

# ============================================================================
# REPLY CONTEXT
# ============================================================================
# The bot answers wherever the interaction happened: a private chat with the bot
# or TELEGRAM_GROUP (optionally a specific topic of it). Every handler stores
# that origin here so the send helpers can default to it instead of always
# writing to TELEGRAM_GROUP. Background daemons (docker events, update checks,
# schedules) run without a context and fall back to TELEGRAM_GROUP, while status
# change notifications keep going to the configured notification channel.
_reply_context = threading.local()

def set_reply_context(chat_id, message_thread_id=None):
	"""Records the chat/topic the update being handled comes from"""
	_reply_context.chat_id = chat_id
	_reply_context.thread_id = message_thread_id

def clear_reply_context():
	"""Clears the context so a reused worker thread does not inherit it"""
	_reply_context.chat_id = None
	_reply_context.thread_id = None

def get_reply_chat_id():
	"""Chat the bot must answer in, or TELEGRAM_GROUP outside of a handler"""
	chat_id = getattr(_reply_context, "chat_id", None)
	if chat_id is None:
		return TELEGRAM_GROUP
	return chat_id

def get_reply_thread_id(chat_id):
	"""
	Topic to post into for a given chat, or None when it has none.

	The chat currently being answered uses the topic of the incoming message,
	TELEGRAM_GROUP falls back to TELEGRAM_THREAD (notifications coming from
	background daemons, or a topic that could not be detected) and any other chat
	(a private chat, the notification channel) has no topics at all.
	"""
	context_chat_id = getattr(_reply_context, "chat_id", None)
	thread_id = None
	if context_chat_id is not None and str(chat_id) == str(context_chat_id):
		thread_id = getattr(_reply_context, "thread_id", None)
	if not thread_id and str(chat_id) == str(TELEGRAM_GROUP):
		thread_id = TELEGRAM_THREAD
	if not thread_id or int(thread_id) == 1:
		return None
	return int(thread_id)

def is_allowed_origin(chat, user_id):
	"""
	Whether the bot accepts commands coming from a given chat.

	Being an administrator is not enough on its own: the bot answers wherever it
	is talked to, so it only listens to the private chat of the administrator
	itself and to TELEGRAM_GROUP. Any other group the bot may have been added to
	is ignored, otherwise its members would get to read the answers.
	"""
	if chat.type == "private":
		return str(chat.id) == str(user_id)
	return str(chat.id) == str(TELEGRAM_GROUP)

def with_reply_context(handler):
	"""Binds the reply context for the whole lifetime of a Telegram handler"""
	@functools.wraps(handler)
	def wrapper(update):
		origin = update.message if isinstance(update, telebot.types.CallbackQuery) else update
		if origin is not None and getattr(origin, "chat", None) is not None:
			# Only forums have real topics; in any other chat message_thread_id
			# may just point at the message being replied to
			is_forum = bool(getattr(origin.chat, "is_forum", False))
			set_reply_context(origin.chat.id, origin.message_thread_id if is_forum else None)
		else:
			clear_reply_context()
		try:
			return handler(update)
		finally:
			clear_reply_context()
	return wrapper

class DockerManager:
	"""
	Every Docker operation, against one host.

	One instance per host. The client is resolved through the registry rather
	than from the environment, so which machine an operation lands on is a
	property of the manager and never something a caller has to remember to
	pass along.
	"""

	def __init__(self, host_id=None):
		self.host_id = host_id or host_registry.local_host_id()
		self.client = host_registry.client(self.host_id)
		self.compose_manager = ComposeProjectManager(self.client)

	@property
	def alias(self):
		"""The host's display name, for messages that have to say where."""
		return host_registry.alias(self.host_id)

	def list_containers(self, comando=""):
		comando = comando.split('@', 1)[0]
		if comando == "/run":
			status = ['paused', 'exited', 'created', 'dead']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		elif comando == "/stop" or comando == "/restart":
			status = ['running', 'restarting']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		elif comando == "/exec":
			status = ['running']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		else:
			containers = self.client.containers.list(all=True)
		status_order = {'running': 0, 'restarting': 1, 'paused': 2, 'exited': 3, 'created': 4, 'dead': 5}
		sorted_containers = sorted(containers, key=lambda x: (0 if x.name == CONTAINER_NAME else 1, status_order.get(x.status, 6), x.name.lower()))
		return sorted_containers

	def container_named(self, name):
		"""
		The container with exactly this name on this host, or None.

		The daemon does the filtering rather than list_containers: this runs on
		every button press, and over ssh pulling the whole list to discard all
		but one is a round-trip the size of the machine instead of the size of
		the answer. Docker's name filter matches by substring, hence the exact
		comparison afterwards.
		"""
		for container in self.client.containers.list(all=True, filters={"name": name}):
			if container.name == name:
				return container
		return None

	# ========== COMPOSE PROJECT METHODS ==========

	def get_compose_projects(self):
		"""
		Returns all detected Docker Compose projects.

		Returns:
			dict: Dictionary {project_name: ComposeProjectInfo}
		"""
		return self.compose_manager.get_all_projects()

	def is_compose_container(self, container):
		"""
		Checks whether a container is part of a Compose project.

		Args:
			container: Docker SDK container object

		Returns:
			bool: True if it is part of a Compose project
		"""
		is_compose = ComposeDetector.is_compose_container(container)
		if is_compose:
			project_name = ComposeDetector.get_project_name(container)
			service_name = ComposeDetector.get_service_name(container)
			debug(f"Container '{container.name}' is part of compose project '{project_name}' (service: {service_name})")
		return is_compose

	def get_container_project_info(self, container):
		"""
		Returns information about the Compose project a container belongs to.

		Args:
			container: Docker SDK container object

		Returns:
			tuple: (project_name, service_name) or (None, None) if not Compose
		"""
		if not ComposeDetector.is_compose_container(container):
			return None, None

		project_name = ComposeDetector.get_project_name(container)
		service_name = ComposeDetector.get_service_name(container)
		return project_name, service_name

	def get_project_info(self, project_name):
		"""
		Returns full information about a Compose project.

		Args:
			project_name: Project name

		Returns:
			ComposeProjectInfo: Project information or None if it doesn't exist
		"""
		return self.compose_manager.get_project_info(project_name)

	# ========== END COMPOSE PROJECT METHODS ==========

	def stop_container(self, container_id, container_name, from_schedule=False):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.stop()
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=get_text("stopped_container", container_name))
			return None
		except Exception as e:
			error(f"Could not stop container {container_name}. Error: [{e}]")
			return get_text("error_stopping_container", container_name)

	def restart_container(self, container_id, container_name, from_schedule=False):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.restart()
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=get_text("restarted_container", container_name))
			return None
		except Exception as e:
			error(f"Could not restart container {container_name}. Error: [{e}]")
			return get_text("error_restarting_container", container_name)

	def start_container(self, container_id, container_name, from_schedule=False):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.start()
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=get_text("started_container", container_name))
			return None
		except Exception as e:
			error(f"Could not start container {container_name}. Error: [{e}]")
			return get_text("error_starting_container", container_name)

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			logs = container.logs().decode("utf-8")
			# Escaped because the message is parsed as HTML and this is whatever
			# the process inside the container decided to print: a single "<"
			# in its output would have Telegram reject the whole message, and
			# the user would see "the logs could not be shown" for a container
			# that is working fine.
			return get_text("showing_logs", container_name, html.escape(logs[-3500:]))
		except Exception as e:
			error(f"The logs for container {container_name} could not be shown. Error: [{e}]")
			return get_text("error_showing_logs_container", container_name)

	def show_logs_raw(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return container.logs().decode("utf-8")
		except Exception as e:
			error(f"The logs for container {container_name} could not be shown. Error: [{e}]")
			return get_text("error_showing_logs_container", container_name)

	def get_docker_compose(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return generate_docker_compose(container)
		except Exception as e:
			error(f"Could not show docker compose for container {container_name}. Error: [{e}]")
			return get_text("error_showing_compose_container", container_name)

	def get_project_info_formatted(self, project_name):
		"""
		Returns formatted information about a Compose project for display to the user.

		Args:
			project_name: Project name

		Returns:
			str: Formatted text with the project information
		"""
		try:
			project_info = self.get_project_info(project_name)
			if not project_info:
				return get_text("error_project_not_found", project_name)

			# Basic information. Everything below comes from Compose labels on
			# the containers, so it is escaped: the message is parsed as HTML
			# and a path or a service name carrying a "<" would have Telegram
			# reject it whole.
			text = f'📦 <b>{get_text("project_info_title", html.escape(project_name))}</b>\n\n'
			text += '<pre><code>'

			# Project name
			text += f'🏷️  {get_text("project_name")}: {html.escape(project_name)}\n\n'

			# Working directory
			working_dir = project_info.get_working_dir()
			if working_dir:
				text += f'📁 {get_text("project_working_dir")}: {html.escape(str(working_dir))}\n\n'

			# Configuration file
			config_files = project_info.get_config_files()
			if config_files:
				# Extract only the file name
				import os
				config_file_name = os.path.basename(config_files)
				text += f'📄 {get_text("project_config_file")}: {html.escape(config_file_name)}\n\n'

			# Number of services
			service_count = project_info.get_container_count()
			text += f'🐳 {get_text("project_services_count")}: {service_count}\n\n'

			# Service list with status and image
			text += f'📋 {get_text("project_services_list")}:\n'
			for service_name in sort_project_services(project_info):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)

				# Get image (short name)
				image_with_tag = container.attrs.get('Config', {}).get('Image', 'N/A')
				# Shorten the image name if it is too long
				if len(image_with_tag) > 30:
					image_with_tag = image_with_tag[:27] + "..."

				text += f'  {status_emoji} {html.escape(service_name)} ({html.escape(image_with_tag)})\n'

			# Dependencies between services
			text += f'\n🔗 {get_text("project_dependencies")}:\n'
			has_dependencies = False
			for service_name in sort_project_services(project_info):
				container = project_info.services[service_name]
				depends_on = container.labels.get('com.docker.compose.depends_on', '')
				if depends_on:
					has_dependencies = True
					# Format dependencies (they may be comma-separated)
					deps = depends_on.replace(',', ', ')
					text += f'  {html.escape(service_name)} → {html.escape(deps)}\n'

			if not has_dependencies:
				text += f'  {get_text("project_no_dependencies")}\n'

			text += '</code></pre>'

			return text

		except Exception as e:
			error(f"Could not display information for project {project_name}. Error: [{e}]")
			return get_text("error_showing_info_project", project_name)

	def get_info(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			if container.status == "running":
				used_cpu = 0.0
				ram = "N/A"
				try:
					stats = container.stats(stream=False)

					if "cpu_stats" in stats and "precpu_stats" in stats:
						cpu_delta = stats["cpu_stats"]["cpu_usage"].get("total_usage", 0) - stats["precpu_stats"]["cpu_usage"].get("total_usage", 0)
						system_cpu_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
						online_cpus = stats["cpu_stats"]["online_cpus"]
						if system_cpu_delta > 0 and cpu_delta > 0:
							cpu_usage_percentage = (cpu_delta / system_cpu_delta) * online_cpus * 100
							used_cpu = round(cpu_usage_percentage, 2)

					if "memory_stats" in stats:
						memory_stats = stats["memory_stats"]
						stats = memory_stats.get("stats", {})
						active_anon = stats.get("active_anon", 0)
						active_file = stats.get("active_file", 0)
						inactive_anon = stats.get("inactive_anon", 0)
						inactive_file = stats.get("inactive_file", 0)
						memory_used = active_anon + active_file + inactive_anon + inactive_file
						used_ram_mb = memory_used / (1024 * 1024)
						if "limit" in memory_stats and memory_stats["limit"] > 0:
							limit_mb = memory_stats["limit"] / (1024 * 1024)
							memory_usage_percentage = round((used_ram_mb / limit_mb) * 100, 2)
							if used_ram_mb > 1024:
								used_ram_gb = used_ram_mb / 1024
								limit_mb_gb = limit_mb / 1024
								ram = f"{used_ram_gb:.2f}/{limit_mb_gb:.2f} GB ({memory_usage_percentage}%)"
							else:
								ram = f"{used_ram_mb:.2f}/{limit_mb:.2f} MB ({memory_usage_percentage}%)"
						else:
							ram = f"{used_ram_mb:.2f} MB"
				except Exception as e:
					error(f"Container {container_name} statistics not available. Error: [{e}]")

			has_update = None
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', 'N/A')

			# Always read cache, regardless of the check_updates setting, which
			# only controls automatic detection and not manual /checkupdate
			try:
				has_update = read_container_update_status(image_with_tag, container_name)
			except Exception as e:
				debug(f"Queried for update {container_name} and it is not available: [{e}]")

			possible_update = has_update is True

			text = '<pre><code>\n'
			text += f'{get_text("status")}: {get_status_emoji(container.status, container_name, container)} ({container.status})\n\n'
			if container.status == "running":
				health_text = get_health_status_text(container)
				if health_text:
					text += f"- {get_text('health')}: {health_text}\n\n"
				if 0.0 != used_cpu:
					text += f"- CPU: {used_cpu}%\n\n"
				if ("0.00 MB") not in ram:
					text += f"- RAM: {ram}\n\n"

			# Port mappings
			port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
			if port_bindings:
				text += f"- {get_text('ports')}:\n"
				for container_port, host_bindings in port_bindings.items():
					if host_bindings:
						for host_binding in host_bindings:
							host_ip = host_binding.get('HostIp', '0.0.0.0')
							host_port = host_binding.get('HostPort', '')
							# Format: 0.0.0.0:8080 -> 80/tcp
							if host_ip == '0.0.0.0' or host_ip == '':
								text += f"  {host_port} → {container_port}\n"
							else:
								text += f"  {host_ip}:{host_port} → {container_port}\n"
				text += "\n"

			text += f'- {get_text("container_id")}: {container_id}\n\n'
			text += f'- {get_text("used_image")}:\n{image_with_tag}\n\n'

			# Try to get image ID (may fail if image was deleted)
			try:
				image_id = container.image.id.replace("sha256:", "")[:CONTAINER_ID_LENGTH]
				text += f'- {get_text("image_id")}: {image_id}'
			except Exception as e:
				debug(f"Could not get image ID for container {container_name}: [{e}]")
				text += f'- {get_text("image_id")}: N/A'

			if store.get("bot.check_updates"):
				text += f"\n\n{update_status_text(has_update)}"
			text += "</code></pre>"
			return f'📜 {get_text("information")} <b>{container_name}</b>:\n{text}', possible_update
		except Exception as e:
			error(f"Could not display information for container {container_name}. Error: [{e}]")
			return get_text("error_showing_info_container", container_name), False

	def update(self, container_id, container_name, message, bot, tag=None):
		"""
		Update a container with a new image while preserving all configuration.
		Uses docker_update module for the actual update logic.
		"""
		try:
			if CONTAINER_NAME == container_name:
				# Self-update: hand the job to the updater container.
				#
				# Always on the local socket, never on self.client: the bot's
				# own container exists on exactly one host, so running the
				# updater anywhere else would look for a container that is not
				# there. Reaching this from another host's menu is possible,
				# because the bot appears in its own listing.
				local = host_registry.client(host_registry.local_host_id())
				if not tag:
					container_environment = {'CONTAINER_NAME': container_name}
				else:
					container_environment = {'CONTAINER_NAME': container_name, 'TAG': tag}
				container_volumes = {'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}}
				new_container = local.containers.run(
					UPDATER_IMAGE,
					name=UPDATER_CONTAINER_NAME,
					environment=container_environment,
					volumes=container_volumes,
					network_mode="bridge",
					detach=True
				)
				return get_text("self_update_message")
			else:
				# Regular container update
				client = self.client
				container = client.containers.get(container_id)

				# Extract all configuration from current container
				config = extract_container_config(container, tag)

				# Perform the update using the extracted configuration
				result = perform_update(
					client=client,
					container=container,
					config=config,
					container_name=container_name,
					message=message,
					edit_message_func=edit_message_text,
					debug_func=debug,
					error_func=error,
					get_text_func=get_text,
					save_status_func=save_container_update_status,
					container_id_length=CONTAINER_ID_LENGTH,
					telegram_group=message.chat.id if message else get_reply_chat_id()
				)
				return result
		except Exception as e:
			error(f"Could not update container {container_name}. Error: [{e}]")
			return get_text("error_updating_container", container_name)

	def recreate_with_overrides(self, container_id, container_name, config_overrides):
		"""
		Recreates a container in-place using its current image, applying the
		given overrides to the extracted configuration before recreation.
		Skips the image pull (the image does not change) and the
		`save_container_update_status` write (nothing was actually updated).

		Used to rewrite HostConfig namespace fields (NetworkMode, IpcMode,
		PidMode, UTSMode) that point at a container id which has just been
		replaced (e.g. dependents of a recreated parent that share its
		network/ipc/pid/uts namespace).
		"""
		try:
			container = self.client.containers.get(container_id)
			config = extract_container_config(container, tag=None)
			if config_overrides:
				config.update(config_overrides)
			result = perform_update(
				client=self.client,
				container=container,
				config=config,
				container_name=container_name,
				message=None,
				edit_message_func=lambda *a, **kw: None,
				debug_func=debug,
				error_func=error,
				get_text_func=get_text,
				save_status_func=lambda *a, **kw: None,
				container_id_length=CONTAINER_ID_LENGTH,
				telegram_group=TELEGRAM_GROUP,
				skip_pull=True,
			)
			return result
		except Exception as e:
			error(f"Could not recreate container {container_name}. Error: [{e}]")
			return get_text("error_updating_container", container_name)

	def force_check_update(self, container_id):
		loading_msg = None
		container = None
		image_with_tag = ''
		has_update = None
		try:
			container = self.client.containers.get(container_id)
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', '')
			local_image = container.image.id

			# Show loading message while pulling the image (can be slow for big images)
			loading_msg = send_message(message=get_text("fetching_image_data"))

			try:
				remote_image = self.client.images.pull(image_with_tag)
				if not remote_image or not remote_image.id:
					error(f"Failed to pull image {image_with_tag}. Verify that the image exists in the registry.")
					has_update = None
					save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
					if loading_msg:
						delete_message(loading_msg.message_id)
						loading_msg = None
					send_message(message=get_text("error_pulling_image", image_with_tag))
					return
			except docker.errors.ImageNotFound:
				error(f"Image {image_with_tag} not found in registry. Check the image name.")
				has_update = None
				save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
				if loading_msg:
					delete_message(loading_msg.message_id)
					loading_msg = None
				send_message(message=get_text("error_pulling_image", image_with_tag))
				return
			except docker.errors.APIError as e:
				error(f"Error pulling image {image_with_tag}. Error: [{e}]")
				has_update = None
				save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
				if loading_msg:
					delete_message(loading_msg.message_id)
					loading_msg = None
				send_message(message=get_text("error_pulling_image", image_with_tag))
				return

			local_image_normalized = local_image.replace('sha256:', '')
			remote_image_normalized = remote_image.id.replace('sha256:', '')

			debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image_normalized[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image_normalized[:CONTAINER_ID_LENGTH]}]")

			if loading_msg:
				delete_message(loading_msg.message_id)
				loading_msg = None

			if local_image_normalized != remote_image_normalized:
				# Keep the pulled image cached locally so the subsequent update
				# operation does not need to re-download it.
				debug(f"{container.name} update detected! Keeping downloaded image [{remote_image_normalized[:CONTAINER_ID_LENGTH]}] for upcoming update")
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container_ref(self.host_id, container)}"))
				has_update = True
				sent_message = send_message(
					message=f'{host_label(self.host_id)}{get_text("available_update", container.name)}',
					reply_markup=markup)
				# Save container cache for this notification
				if sent_message:
					save_container_cache(sent_message.chat.id, sent_message.message_id, [container], self.host_id)
			else:
				has_update = False
				send_message(message=f'{host_label(self.host_id)}{get_text("already_updated", container.name)}')
		except Exception as e:
			error(f"Could not check update: [{e}]")
			has_update = None
			if loading_msg:
				try:
					delete_message(loading_msg.message_id)
				except:
					pass

		if image_with_tag and container is not None and getattr(container, 'name', None):
			save_container_update_status(image_with_tag, container.name, has_update, self.host_id)

	def delete(self, container_id, container_name):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container_is_running = container.status in ['running', 'restarting', 'paused', 'created']
			if container_is_running:
				debug(f"Container {container_name} is running. It will be stopped.")
				container.stop()
			container.remove()
			return get_text("deleted_container", container_name)
		except Exception as e:
			error(f"Could not delete container {container_name}. Error: [{e}]")
			return get_text("error_deleting_container", container_name)

	def prune_containers(self):
		try:
			pruned_containers = self.client.containers.prune()
			if pruned_containers:
				file_size_bytes = sizeof_fmt(pruned_containers['SpaceReclaimed'])
			debug(f"Deleted: [{str(pruned_containers)}] - Space reclaimed: {str(file_size_bytes)}")
			return get_text("prune_containers", str(file_size_bytes)), str(pruned_containers)
		except Exception as e:
			error(f"An error has occurred deleting unused containers. Error: [{e}]")
			return get_text("error_prune_containers")

	def prune_images(self):
		try:
			pruned_images = self.client.images.prune(filters={'dangling': False})
			if pruned_images:
				file_size_bytes = sizeof_fmt(pruned_images['SpaceReclaimed'])
			debug(f"Deleted: [{str(pruned_images)}] - Space reclaimed: {str(file_size_bytes)}")
			return get_text("prune_images", str(file_size_bytes)), str(pruned_images)
		except Exception as e:
			error(f"An error occurred deleting unused images. Error: [{e}]")
			return get_text("error_prune_images")

	def prune_networks(self):
		try:
			pruned_networks = self.client.networks.prune()
			debug(f"Deleted: [{str(pruned_networks)}]")
			return get_text("prune_networks"), str(pruned_networks)
		except Exception as e:
			error(f"An error occurred while deleting unused networks. Error: [{e}]")
			return get_text("error_prune_networks")


	def prune_volumes(self):
		try:
			pruned_volumes = self.client.volumes.prune()
			if pruned_volumes:
				file_size_bytes = sizeof_fmt(pruned_volumes['SpaceReclaimed'])
			debug(f"Deleted: [{str(pruned_volumes)}] - Space reclaimed: {str(file_size_bytes)}")
			return get_text("prune_volumes", str(file_size_bytes)), str(pruned_volumes)
		except Exception as e:
			error(f"An error occurred deleting unused volumes. Error: [{e}]")
			return get_text("error_prune_volumes")

	def execute_command(self, container_id, container_name, command):
		try:
			container = self.client.containers.get(container_id)
			# Some minimal/distroless images do not ship a shell.
			# Try common shells first and, if none is available, run the command directly.
			for exec_command in (['sh', '-c', command], ['bash', '-c', command]):
				try:
					result = container.exec_run(exec_command)
				except docker.errors.APIError:
					continue
				if not self._is_executable_missing(result):
					return self._decode_exec_output(result)
			# No shell available: execute the command tokens directly.
			try:
				direct_command = shlex.split(command)
			except ValueError:
				direct_command = command
			result = container.exec_run(direct_command)
			return self._decode_exec_output(result)
		except Exception as e:
			error(f"Error executing command [{command}] in container {container_name}. Error: [{e}]")
			return get_text("error_executing_command_container", command, container_name)

	def _decode_exec_output(self, result):
		output = result.output.decode('utf-8') if result.output else ''
		if not output:
			output = get_text("command_executed_without_output")
		return output

	def _is_executable_missing(self, result):
		output = (result.output or b'').decode('utf-8', errors='replace')
		if "OCI runtime exec failed" in output or "unable to start container process" in output:
			return True
		if result.exit_code in (126, 127) and ("executable file not found" in output or "no such file or directory" in output):
			return True
		return False

# Instantiate the DockerManager
# Managers are cached per host: each one holds a connection, so rebuilding it
# on every command would mean reconnecting constantly.
_managers = {}
_managers_lock = threading.Lock()


def manager(host_id=None):
	"""
	The manager for one host, building it on first use.

	Raises host_registry.HostUnavailable when the host cannot be reached, which
	callers sweeping several hosts catch to skip the ones that are down.
	"""
	host_id = host_id or host_registry.local_host_id()
	with _managers_lock:
		existing = _managers.get(host_id)
		if existing is not None and existing.client is host_registry.client(host_id):
			return existing
		built = DockerManager(host_id)
		_managers[host_id] = built
		return built


def managers():
	"""A manager per reachable host, skipping the ones that do not answer."""
	built = []
	for entry, _ in host_registry.reachable_hosts():
		try:
			built.append(manager(entry["id"]))
		except host_registry.HostUnavailable as e:
			warning(str(e))
	return built


def forget_managers():
	"""Drops the cached managers, so the next use reconnects."""
	with _managers_lock:
		_managers.clear()


# The local host's manager. Most of the bot still goes through this one, and
# with a single host configured it is the only one there is.
docker_manager = DockerManager()

# Instantiate the PortManager
port_manager = PortManager(docker_manager)

def host_alias(host_id):
	"""
	A host's display name, ready to go into a message parsed as HTML.

	Aliases are typed by hand, so one containing `<` would otherwise break the
	parse and the message would never arrive.
	"""
	return html.escape(host_registry.alias(host_id))


def host_label(host_id):
	"""
	How a message says which host it is about, or nothing when there is only
	one.

	With a single host configured the bot must read exactly as it did before
	hosts existed, so this is empty rather than saying "local" everywhere.
	"""
	if host_registry.is_single_host():
		return ""
	return f"<b>{host_alias(host_id)}</b> · "


class DockerEventMonitor:
	"""
	Watches one host's event stream and reports containers starting and
	stopping.

	One instance per host: client.events() blocks, so it needs a thread of its
	own, and a host going away must not take the others' streams with it.
	"""

	# Reconnection backs off up to this, and never gives up. 4.x stopped after
	# five failures, which on a remote host that is briefly unreachable would
	# mean its events stay silent until the bot is restarted.
	MAX_BACKOFF_SECONDS = 300

	def __init__(self, host_id):
		self.host_id = host_id
		self._stop = threading.Event()

	@property
	def alias(self):
		return host_registry.alias(self.host_id)

	def stop(self):
		"""
		Asks the monitor to finish.

		The blocking events() call cannot be interrupted, so the client is
		dropped as well: the stream then fails and the loop sees the flag.
		"""
		self._stop.set()
		host_registry.drop(self.host_id)

	def detectar_eventos_contenedores(self):
		client = host_registry.client(self.host_id)
		for event in client.events(decode=True):
			if self._stop.is_set():
				return

			# Only process container events
			event_type = event.get('Type', '')
			if event_type != 'container':
				continue

			# Support both 'Action' (Docker Desktop/newer) and 'status' (Docker Engine/older) formats
			action = event.get('Action', '') or event.get('status', '')
			actor = event.get('Actor', {})
			attributes = actor.get('Attributes', {})
			container_name = attributes.get('name', '')

			message = None
			if action == "start":
				message = get_text("started_container", container_name)
			elif action == "die":
				message = get_text("stopped_container", container_name)
			elif action == "create" and store.get("bot.extended_messages"):
				message = get_text("created_container", container_name)

			if message:
				message = f"{host_label(self.host_id)}{message}"
				try:
					if is_muted():
						debug(f"Message [{message}] omitted because muted")
						continue

					send_message_to_notification_channel(message=message)
				except Exception as e:
					error(f"Could not send notification [{message}]. Error: [{e}]")
					time.sleep(20) # Possible Telegram saturation causing send_message to raise an exception

	def _event_loop_with_retry(self):
		"""
		Keeps the stream alive, backing off after each failure.

		Failures are logged in full the first few times and then only
		occasionally: a host that stays down would otherwise fill the log with
		the same line every five minutes.
		"""
		failures = 0
		while not self._stop.is_set():
			try:
				debug(f"Event monitor ({self.alias}): starting event listener...")
				self.detectar_eventos_contenedores()
				if self._stop.is_set():
					break
				debug(f"Event monitor ({self.alias}): event stream ended unexpectedly, restarting...")
			except Exception as e:
				if self._stop.is_set():
					break
				failures += 1
				if failures <= 3 or failures % 10 == 0:
					error(f"Event monitor ({self.alias}) error #{failures}: [{e}]")
			else:
				failures = 0

			# Force a fresh connection on the next attempt.
			host_registry.drop(self.host_id)
			backoff = min(5 * (2 ** min(failures, 6)), self.MAX_BACKOFF_SECONDS) if failures else 5
			if self._stop.wait(backoff):
				break
		debug(f"Event monitor ({self.alias}) stopped")

	def demonio_event(self):
		"""Start event daemon in a background thread."""
		thread = threading.Thread(target=self._event_loop_with_retry, daemon=True)
		thread.start()
		debug(f"Event monitor daemon started for {self.alias}")
		return thread


class EventMonitorSupervisor:
	"""
	Keeps one event monitor running per configured host.

	Hosts can be added and removed from /settings while the bot is running, so
	something has to notice and start or stop the matching stream. Reconciling
	on a timer rather than on a signal keeps it simple and self-healing: a
	monitor that died for good is picked up on the next pass.
	"""

	RECONCILE_SECONDS = 30

	def __init__(self):
		self._monitors = {}
		self._lock = threading.Lock()

	def reconcile(self):
		"""Starts monitors for new hosts and stops them for removed ones."""
		configured = {entry["id"] for entry in host_registry.hosts()}
		with self._lock:
			for host_id in configured - set(self._monitors):
				monitor = DockerEventMonitor(host_id)
				self._monitors[host_id] = monitor
				monitor.demonio_event()
			for host_id in set(self._monitors) - configured:
				debug(f"Host {host_id} is gone: stopping its event monitor")
				self._monitors.pop(host_id).stop()

	def _supervise(self):
		while True:
			try:
				self.reconcile()
			except Exception as e:
				error(f"Event monitor supervisor error: [{e}]")
			time.sleep(self.RECONCILE_SECONDS)

	def start(self):
		"""Brings up a monitor per host and keeps watching for changes."""
		self.reconcile()
		thread = threading.Thread(target=self._supervise, daemon=True)
		thread.start()
		return thread


def wait_for_next_update_check():
	"""
	Waits out the configured interval between update checks, in short steps.

	The interval is hours long and is read again on every step, so shortening it
	from /settings applies during the current wait rather than after it. The wait
	also ends early when update checks are switched off.
	"""
	waited = 0.0
	logged_interval = None
	while True:
		if not store.get("bot.check_updates"):
			return
		try:
			interval_hours = float(store.get("bot.check_update_every_hours"))
		except (TypeError, ValueError):
			interval_hours = 4.0
		target = max(interval_hours * 3600, UPDATE_CHECK_POLL_SECONDS)
		if interval_hours != logged_interval:
			debug(f"Waiting {interval_hours} hours for the next update check...")
			logged_interval = interval_hours
		if waited >= target:
			return
		step = min(UPDATE_CHECK_POLL_SECONDS, target - waited)
		time.sleep(step)
		waited += step


class DockerUpdateMonitor:
	def __init__(self):
		self.client = docker.from_env()

	def detectar_actualizaciones(self):
		while True:
			if not store.get("bot.check_updates"):
				# The daemon runs even with checks disabled so that turning them
				# back on from /settings takes effect without a restart.
				time.sleep(UPDATE_CHECK_POLL_SECONDS)
				continue

			# An empty cache means this is the first pass on this install, so
			# anything found is pre-existing rather than new. Filling it quietly
			# avoids announcing every pending update at once, which is what used
			# to happen after every container recreation.
			cold_cache = not store.has_update_cache()
			if cold_cache:
				debug("Update cache is empty: this pass will fill it without notifying")

			# Every reachable host, in the order they are configured. One being
			# down degrades its own check and nothing else.
			for entry, owner, _ in hosts_with_containers():
				try:
					self._check_host(entry, owner, cold_cache)
				except Exception as e:
					error(f"Update check failed on {entry.get('alias', entry['id'])}: [{e}]")
			wait_for_next_update_check()

	def _check_host(self, entry, owner, cold_cache):
		"""
		Checks one host for image updates and reports what it finds.

		One host at a time, in sequence, rather than a thread each: the interval
		is hours long so there is nothing to gain in parallel, and every host
		pulling images at once would saturate the same network and disk.
		"""
		host_id = entry["id"]
		containers = owner.client.containers.list(all=True)
		# Sort containers: bot first, then running, then stopped (all alphabetically)
		sorted_containers = sort_containers_by_priority(containers)
		grouped_updates_containers = []  # list of [id, name] pairs
		should_notify = False
		for container in sorted_containers:
			if (container.status == "exited" or container.status == "dead") and not store.get("bot.check_update_stopped_containers"):
				debug(f"Ignoring update check for container {container.name} (stopped)")
				continue

			labels = container.labels
			if LABEL_IGNORE_CHECK_UPDATES in labels:
				debug(f"Ignoring update check for container {container.name} (label)")
				continue

			container_attrs = container.attrs['Config']
			image_with_tag = container_attrs['Image']
			try:
				local_image = container.image.id
				remote_image = owner.client.images.pull(image_with_tag)
				debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}]")
				if local_image != remote_image.id:
					if LABEL_AUTO_UPDATE in labels:
						if store.get("bot.extended_messages") and not is_muted():
							send_message_to_notification_channel(message=f'{host_label(host_id)}{get_text("auto_update", container.name)}')
						debug(f"Auto-updating container {container.name}")
						# Build a send_fn that routes to the notification channel,
						# or silently swallows messages (with a debug trace) when muted.
						if is_muted():
							def _auto_update_send_fn(msg):
								debug(f"Message [{msg}] omitted because muted")
								return None
						else:
							def _auto_update_send_fn(msg):
								return send_message_to_notification_channel(message=msg)
						perform_container_update(container_ref(host_id, container), container.name, send_fn=_auto_update_send_fn)
						continue
					old_has_update = read_container_update_status(image_with_tag, container.name, host_id)
					has_update = True
					# Keep the pulled image cached locally so the subsequent update
					# operation does not need to re-download it.
					debug(f"{container.name} update detected! Keeping downloaded image [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}] for upcoming update")

					if container.name != CONTAINER_NAME:
						grouped_updates_containers.append([container_ref(host_id, container), container.name])

					if old_has_update is True:
						debug("Update already notified")
						continue

					if container.name == CONTAINER_NAME:
						markup = InlineKeyboardMarkup(row_width = 1)
						markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container_ref(host_id, container)}"))
						if not is_muted() and not cold_cache:
							sent_message = send_message(message=f'{host_label(host_id)}{get_text("available_update", container.name)}', reply_markup=markup)
							# Save container cache for this notification
							if sent_message:
								save_container_cache(sent_message.chat.id, sent_message.message_id, [container], host_id)
						else:
							debug(f"Message [{get_text('available_update', container.name)}] omitted because muted")
						# Persist the "already notified" status so the bot is not spammed
						# every cycle. Other containers reach the equivalent save below
						# via the grouped-updates flow; the bot's self-update has its
						# own dedicated message and would otherwise skip it.
						save_container_update_status(image_with_tag, container.name, has_update, host_id)
						continue

					should_notify = not cold_cache
				else: # Contenedor actualizado
					has_update = False
			except Exception as e:
				error(f"Could not check update: [{e}]")
				has_update = None
			save_container_update_status(image_with_tag, container.name, has_update, host_id)

		if grouped_updates_containers and should_notify:
			markup = InlineKeyboardMarkup(row_width=button_columns())
			markup.add(*[
				InlineKeyboardButton(f'{ICON_CONTAINER_MARK_FOR_UPDATE} {cname}', callback_data=f'toggleUpdate|{cid}')
				for cid, cname in grouped_updates_containers
			])
			markup.add(
				InlineKeyboardButton(get_text("button_update_all"), callback_data="toggleUpdateAll"),
				InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar")
			)
			if not is_muted():
				message = send_message(message=f'{host_label(host_id)}{get_text("available_updates", len(grouped_updates_containers))}', reply_markup=markup)
				if message:
					save_update_data(message.chat.id, message.message_id, grouped_updates_containers)
					# Also populate the container name cache so the callback parser
					# can resolve names from IDs without an extra Docker lookup.
					_objs = []
					for cid, _ in grouped_updates_containers:
						try:
							_objs.append(owner.client.containers.get(ref_id(cid)))
						except Exception as e:
							debug(f"Could not fetch container {cid} for cache: {e}")
					if _objs:
						try:
							save_container_cache(message.chat.id, message.message_id, _objs, host_id)
						except Exception as e:
							debug(f"Could not pre-populate container name cache: {e}")
			else:
				debug(f"Message [{get_text('available_updates', len(grouped_updates_containers))}] omitted because muted")


	def demonio_update(self):
		"""Start update daemon with limited retries to prevent infinite restart loops."""
		max_retries = 5
		retry_count = 0

		while retry_count < max_retries:
			try:
				thread = threading.Thread(target=self.detectar_actualizaciones, daemon=True)
				thread.start()
				return  # Successfully started
			except Exception as e:
				retry_count += 1
				if retry_count >= max_retries:
					error(f"Update daemon failed {max_retries} times. Stopping. Last error: [{e}]")
					return
				error(f"Update daemon error (attempt {retry_count}/{max_retries}). Retrying in 5 seconds... Error: [{e}]")
				time.sleep(5)

def schedule_container_ref(host_id, container_name):
	"""
	The reference for a scheduled task's container, on its own host.

	Resolved on that host and nowhere else: a task names a container, and
	acting on a same-named container elsewhere would be both silent and, for
	stop or exec, destructive.
	"""
	short_id = find_container_id_on_host(host_id, container_name)
	return make_ref(host_id, short_id) if short_id else None


class DockerScheduleMonitor:
	def __init__(self):
		super().__init__()
		self.schedule_manager = schedule_manager  # Use the global instance
		self.last_run = {}  # Track last execution time for each schedule
		self._reboot_tasks_executed = set()  # Track which @reboot tasks have been executed
		self._execute_reboot_tasks()  # Execute @reboot tasks on startup

	def _execute_reboot_tasks(self):
		"""Execute all @reboot tasks immediately on bot startup"""
		try:
			schedules = self.schedule_manager.get_all_schedules()

			for schedule in schedules:
				# Only execute @reboot tasks
				if schedule.get("cron") == "@reboot":
					success = self._execute_schedule_action(schedule)
					if success:
						# Mark this task as executed
						self._reboot_tasks_executed.add(schedule.get("name"))
		except Exception as e:
			error(f"Error reading schedule file: [{e}]")

	def _execute_schedule_action(self, schedule: dict):
		"""
		Execute a schedule action from JSON format.

		Args:
			schedule: Schedule dict from JSON

		Returns:
			True if successful, False if failed
		"""
		try:
			action = schedule.get("action", "").lower()
			container = schedule.get("container", "")
			# Tasks created before hosts existed carry none, and mean the local
			# machine: that is where they were set up to run.
			schedule_host = schedule.get("host") or host_registry.local_host_id()
			minutes = schedule.get("minutes")
			command = schedule.get("command", "")
			show_output = bool(schedule.get("show_output", False))
			prune_type = schedule.get("prune_type", "")
			schedule_name = schedule.get("name", "")

			# Helper function to handle errors consistently
			def handle_error(error_msg):
				error(error_msg)
				# Disable the schedule instead of deleting it
				if schedule_name:
					self.schedule_manager.update_schedule(schedule_name, enabled=False)
					send_message(message=get_text("error_schedule_disabled", schedule_name))
				return False

			# Execute action based on type
			if action == "run":
				containerId = schedule_container_ref(schedule_host, container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				run(containerId, container, from_schedule=True)

			elif action == "stop":
				containerId = schedule_container_ref(schedule_host, container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				stop(containerId, container, from_schedule=True)

			elif action == "restart":
				containerId = schedule_container_ref(schedule_host, container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				restart(containerId, container, from_schedule=True)

			elif action == "mute":
				try:
					minutes = int(minutes)
					if minutes <= 0:
						return handle_error(f"Invalid minutes value: {minutes}")
					mute(minutes)
				except (ValueError, TypeError):
					return handle_error(f"Invalid minutes value: {minutes}")

			elif action == "exec":
				containerId = schedule_container_ref(schedule_host, container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				execute_command(containerId, container, command, show_output)

			elif action == "prune":
				# Execute prune based on type
				result_message = None
				data = None
				prune_label = ""

				if prune_type == "containers":
					result_message, data = manager(schedule_host).prune_containers()
					prune_label = get_text("button_containers")
				elif prune_type == "images":
					result_message, data = manager(schedule_host).prune_images()
					prune_label = get_text("button_images")
				elif prune_type == "networks":
					result_message, data = manager(schedule_host).prune_networks()
					prune_label = get_text("button_networks")
				elif prune_type == "volumes":
					result_message, data = manager(schedule_host).prune_volumes()
					prune_label = get_text("button_volumes")
				else:
					return handle_error(f"Unknown prune type: {prune_type}")

				# Show output if requested, otherwise just log
				if show_output and result_message:
					# Send the same format as manual /prune command
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
					fichero_temporal = get_temporal_file(data, prune_label)
					x = send_message(message=get_text("loading_file"))
					send_document(document=fichero_temporal, reply_markup=markup, caption=result_message)
					delete_message(x.message_id)
				else:
					debug(f"Scheduled prune executed: {result_message}")

			return True

		except Exception as e:
			error(f"Error executing schedule action [{action}]: [{str(e)}]")
			return False

	def run(self):
		"""Main loop: check and execute scheduled tasks every minute"""
		while True:
			try:
				schedules = self.schedule_manager.get_all_schedules()
				now = datetime.now()

				for schedule in schedules:
					# Skip disabled schedules
					if not schedule.get("enabled", True):
						continue

					cron_expr = schedule.get("cron")
					schedule_name = schedule.get("name")

					# Skip @reboot tasks in the main loop (they're executed at startup)
					if cron_expr == "@reboot":
						continue

					# Check if this task should run now
					if self.should_run(schedule_name, cron_expr, now):
						self._execute_schedule_action(schedule)
			except Exception as e:
				error(f"Error reading schedule file: [{e}]")
			time.sleep(60)

	def should_run(self, schedule_name, cron_expr, now):
		"""
		Check if a cron expression should run at the given time.
		Uses a tracking system to ensure tasks only run once per scheduled time.

		Note: @reboot tasks are handled separately in _execute_reboot_tasks()
		and should not reach this method.
		"""
		try:
			# Create a croniter object starting from one minute ago
			# This helps us detect if we should run in the current minute
			one_minute_ago = now - timedelta(minutes=1)
			cron = croniter(cron_expr, one_minute_ago)

			# Get the next execution time after one minute ago
			next_execution = cron.get_next(datetime)

			# Check if the next execution is within the current minute
			# (i.e., it should run now)
			should_run = (next_execution.year == now.year and
						 next_execution.month == now.month and
						 next_execution.day == now.day and
						 next_execution.hour == now.hour and
						 next_execution.minute == now.minute)

			# Track execution to avoid running multiple times in the same minute
			task_key = f"{schedule_name}_{now.strftime('%Y-%m-%d %H:%M')}"
			if should_run and task_key not in self.last_run:
				self.last_run[task_key] = True
				return True

			return False
		except Exception as e:
			debug(f"Error checking cron schedule '{schedule_name}' with expression '{cron_expr}': {e}")
			return False

	def demonio_schedule(self):
		"""Start schedule daemon with limited retries to prevent infinite restart loops."""
		max_retries = 5
		retry_count = 0

		while retry_count < max_retries:
			try:
				thread = threading.Thread(target=self.run, daemon=True)
				thread.start()
				return  # Successfully started
			except Exception as e:
				retry_count += 1
				if retry_count >= max_retries:
					error(f"Schedule daemon failed {max_retries} times. Stopping. Last error: [{e}]")
					return
				error(f"Schedule daemon error (attempt {retry_count}/{max_retries}). Retrying in 5 seconds... Error: [{e}]")
				time.sleep(5)

# ============================================================================
# SCHEDULE INTERACTIVE FLOW FUNCTIONS
# ============================================================================

def _validate_schedule_index(index_str: str, schedules: list) -> int:
	"""Validate and return schedule index, or -1 if invalid"""
	try:
		idx = int(index_str)
		if 1 <= idx <= len(schedules):
			return idx - 1  # Convert to 0-based index
		return -1
	except (ValueError, TypeError):
		return -1

def _build_schedule_summary(state: dict) -> str:
	"""Build a consistent schedule summary message from state dict"""
	lines = []

	# Add schedule details
	if state.get("name"):
		lines.append(f"<b>{get_text('schedule_label_name')}:</b> {state.get('name')}")
	if state.get("cron"):
		lines.append(f"<b>{get_text('schedule_label_cron')}:</b> {state.get('cron')}")
	if state.get("action"):
		lines.append(f"<b>{get_text('schedule_label_action')}:</b> {state.get('action')}")
	if state.get("container"):
		lines.append(f"<b>{get_text('schedule_label_container')}:</b> {state.get('container')}")
	if state.get("minutes") is not None:  # Use is not None to handle 0
		lines.append(f"<b>{get_text('schedule_label_minutes')}:</b> {state.get('minutes')}")
	if state.get("prune_type"):
		lines.append(f"<b>{get_text('schedule_label_prune_type')}:</b> {state.get('prune_type')}")
	# Which machine the task will run on, once one has been chosen. Never
	# defaulted here: the step that asks for it renders this summary above the
	# question, and a fallback would have it claiming a host while still
	# asking which one.
	if (state.get("host") and not host_registry.is_single_host()
			and state.get("action") in HOST_SCOPED_SCHEDULE_ACTIONS):
		lines.append(f"<b>{get_text('schedule_label_host')}:</b> {host_alias(state['host'])}")
	# Only show show_output if action is exec or prune and show_output is not None
	if state.get("action") in ("exec", "prune") and state.get("show_output") is not None:
		lines.append(f"<b>{get_text('schedule_label_show_output')}:</b> {get_text('schedule_yes') if state.get('show_output') else get_text('schedule_no')}")
	if state.get("command"):
		lines.append(f"<b>{get_text('schedule_label_command')}:</b> {state.get('command')}")

	return "\n".join(lines)

def _validate_containers_available() -> bool:
	"""Check if there are containers available (excluding bot container)"""
	return len(_get_available_containers()) > 0

def _get_available_containers() -> list:
	"""
	The containers a scheduled task can act on, as (host_entry, container).

	Across every reachable host: a task pinned to one machine would otherwise
	only ever be offered the local one's containers.
	"""
	available = []
	for entry, _, containers in hosts_with_containers():
		for container in containers:
			if container.name != CONTAINER_NAME:
				available.append((entry, container))
	return available

def show_schedule_menu(user_id: int, chat_id: int):
	"""Show the main schedule menu - Optimized with caching and efficient string building"""
	schedules = schedule_manager.get_all_schedules()

	# Pre-cache all needed translations
	title = get_text("schedule_menu_title")
	current_schedules_label = get_text("schedule_current_schedules")
	no_schedules_msg = get_text("schedule_no_schedules")
	status_enabled = get_text('schedule_status_enabled')
	status_disabled = get_text('schedule_status_disabled')
	label_status = get_text('schedule_label_status')
	label_cron = get_text('schedule_label_cron')
	label_action = get_text('schedule_label_action')
	label_minutes = get_text('schedule_label_minutes')
	label_container = get_text('schedule_label_container')
	label_command = get_text('schedule_label_command')
	label_show_output = get_text('schedule_label_show_output')
	label_prune_type = get_text('schedule_label_prune_type')
	yes_text = get_text('schedule_yes')
	no_text = get_text('schedule_no')

	# Build message efficiently with list
	lines = [title]

	if schedules:
		lines.append(f"\n\n<b>{current_schedules_label}</b>")

		for idx, sched in enumerate(schedules, 1):
			# Unpack all values at once
			name = sched['name']
			action = sched.get('action', '')
			cron = sched.get('cron', '* * * * *')
			container = sched.get('container', '')
			minutes = sched.get('minutes', '')
			command = sched.get('command', '')
			show_output = sched.get('show_output', False)
			prune_type = sched.get('prune_type', '')
			enabled = sched.get('enabled', True)

			# Build schedule entry
			status_icon = "🟢" if enabled else "🔴"
			status_text = status_enabled if enabled else status_disabled

			lines.append(f"\n<b>{idx}. {name}</b>")
			lines.append(f"  {label_status}: <b>{status_icon} {status_text}</b>")
			lines.append(f"  {label_cron}: <code>{cron}</code>")
			lines.append(f"  {label_action}: <b>{action}</b>")

			# Which machine the task runs on. A task names a container, and a
			# name is only unique within one daemon, so with several hosts the
			# listing is ambiguous without this. Placed where the creation
			# summary puts it — after what it acts on, before show_output — so
			# both read the same way round.
			host_line = None
			if not host_registry.is_single_host() and action in HOST_SCOPED_SCHEDULE_ACTIONS:
				task_host = sched.get("host") or host_registry.local_host_id()
				host_line = f"  {get_text('schedule_label_host')}: <b>{host_alias(task_host)}</b>"

			# Add action-specific details
			if action == 'mute':
				lines.append(f"  {label_minutes}: <b>{minutes}</b>")
			elif action == 'exec':
				lines.append(f"  {label_container}: <b>{container}</b>")
				lines.append(f"  {label_command}: <code>{command}</code>")
				if host_line:
					lines.append(host_line)
				output_text = yes_text if show_output else no_text
				lines.append(f"  {label_show_output}: <b>{output_text}</b>")
			elif action == 'prune':
				lines.append(f"  {label_prune_type}: <b>{prune_type}</b>")
				if host_line:
					lines.append(host_line)
				output_text = yes_text if show_output else no_text
				lines.append(f"  {label_show_output}: <b>{output_text}</b>")
			elif action in ('run', 'stop', 'restart'):
				lines.append(f"  {label_container}: <b>{container}</b>")
				if host_line:
					lines.append(host_line)

			lines.append("")
	else:
		lines.append(f"\n\n{no_schedules_msg}")

	message_text = "\n".join(lines)

	# Build keyboard
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(
		InlineKeyboardButton(get_text("schedule_button_add"), callback_data="scheduleAdd"),
		InlineKeyboardButton(get_text("schedule_button_edit"), callback_data="scheduleEdit"),
		InlineKeyboardButton(get_text("schedule_button_delete"), callback_data="scheduleDelete"),
		InlineKeyboardButton(get_text("button_close"), callback_data="cerrar")
	)

	send_message(message=message_text, reply_markup=markup)

def show_schedule_delete_list(user_id: int, chat_id: int):
	"""Show list of schedules to delete - Optimized"""
	schedules = schedule_manager.get_all_schedules()

	if not schedules:
		send_message(message=get_text("schedule_no_schedules"))
		return

	# Build message efficiently
	header = get_text("schedule_select_to_delete")
	schedule_lines = [f"{idx}. <code>{sched['name']}</code>" for idx, sched in enumerate(schedules, 1)]
	message_text = f"{header}\n\n" + "\n".join(schedule_lines)

	markup = InlineKeyboardMarkup(row_width=5)
	buttons = [InlineKeyboardButton(str(idx), callback_data=f"scheduleSelectDelete|{idx}")
	           for idx in range(1, len(schedules) + 1)]
	markup.add(*buttons)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))

	send_message(message=message_text, reply_markup=markup)

def show_schedule_edit_list(user_id: int, chat_id: int):
	"""Show list of schedules to edit - Optimized"""
	schedules = schedule_manager.get_all_schedules()

	if not schedules:
		send_message(message=get_text("schedule_no_schedules"))
		return

	# Build message efficiently
	header = get_text("schedule_select_to_edit")
	schedule_lines = [f"{idx}. <code>{sched['name']}</code>" for idx, sched in enumerate(schedules, 1)]
	message_text = f"{header}\n\n" + "\n".join(schedule_lines)

	markup = InlineKeyboardMarkup(row_width=5)
	buttons = [InlineKeyboardButton(str(idx), callback_data=f"scheduleSelectEdit|{idx}")
	           for idx in range(1, len(schedules) + 1)]
	markup.add(*buttons)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))

	send_message(message=message_text, reply_markup=markup)

def show_schedule_edit_options(user_id: int, schedule_name: str):
	"""Show options to edit a schedule"""
	schedule = schedule_manager.get_schedule(schedule_name)
	if not schedule:
		send_message(message=get_text("error_invalid_selection"))
		return

	action = schedule.get('action')
	enabled = schedule.get('enabled', True)
	cron = schedule.get('cron', '* * * * *')
	container = schedule.get('container', '')
	minutes = schedule.get('minutes', '')
	command = schedule.get('command', '')
	show_output = schedule.get('show_output', False)
	prune_type = schedule.get('prune_type', '')
	status_text = get_text('schedule_status_enabled') if enabled else get_text('schedule_status_disabled')
	status_icon = "🟢" if enabled else "🔴"

	# Build message with schedule details
	message_text = f"<b>{schedule_name}</b>\n\n"
	message_text += f"<b>{get_text('schedule_label_status')}:</b> {status_icon} {status_text}\n"
	message_text += f"<b>{get_text('schedule_label_cron')}:</b> <code>{cron}</code>\n"
	message_text += f"<b>{get_text('schedule_label_action')}:</b> <b>{action}</b>\n"
	# Which machine the task runs on, same rule and same position as the other
	# two renderers: only with more than one host, only for actions that act on
	# Docker, and after what the task acts on.
	host_line = ""
	if not host_registry.is_single_host() and action in HOST_SCOPED_SCHEDULE_ACTIONS:
		task_host = schedule.get("host") or host_registry.local_host_id()
		host_line = f"<b>{get_text('schedule_label_host')}:</b> <b>{host_alias(task_host)}</b>\n"

	if action == 'mute':
		message_text += f"<b>{get_text('schedule_label_minutes')}:</b> <b>{minutes}</b>\n"
	elif action == 'exec':
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{container}</b>\n"
		message_text += f"<b>{get_text('schedule_label_command')}:</b> <code>{command}</code>\n"
		message_text += host_line
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> <b>{get_text('schedule_yes') if show_output else get_text('schedule_no')}</b>\n"
	elif action == 'prune':
		message_text += f"<b>{get_text('schedule_label_prune_type')}:</b> <b>{prune_type}</b>\n"
		message_text += host_line
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> <b>{get_text('schedule_yes') if show_output else get_text('schedule_no')}</b>\n"
	elif action in ('run', 'stop', 'restart'):
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{container}</b>\n"
		message_text += host_line

	message_text += "\n" + get_text("schedule_edit_what") + "\n\n"

	schedule_id = schedule.get('id', 0)

	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("schedule_edit_name"), callback_data=f"scheduleEditField|name|{schedule_id}"))
	markup.add(InlineKeyboardButton(get_text("schedule_edit_cron"), callback_data=f"scheduleEditField|cron|{schedule_id}"))

	if action in ('run', 'stop', 'restart', 'exec'):
		markup.add(InlineKeyboardButton(get_text("schedule_edit_container"), callback_data=f"scheduleEditField|container|{schedule_id}"))

	if action == 'mute':
		markup.add(InlineKeyboardButton(get_text("schedule_edit_minutes"), callback_data=f"scheduleEditField|minutes|{schedule_id}"))

	if action == 'exec':
		markup.add(InlineKeyboardButton(get_text("schedule_edit_command"), callback_data=f"scheduleEditField|command|{schedule_id}"))

	if action == 'prune':
		markup.add(InlineKeyboardButton(get_text("schedule_edit_prune_type"), callback_data=f"scheduleEditField|prune_type|{schedule_id}"))

	if action in ('exec', 'prune'):
		markup.add(InlineKeyboardButton(get_text("schedule_edit_show_output"), callback_data=f"scheduleEditField|show_output|{schedule_id}"))

	# Add status toggle button
	status_button_text = get_text("schedule_button_disable") if enabled else get_text("schedule_button_enable")
	markup.add(InlineKeyboardButton(status_button_text, callback_data=f"scheduleEditStatus|{schedule_id}"))

	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	send_message(message=message_text, reply_markup=markup)

def ask_schedule_name(user_id: int):
	"""Ask user for schedule name"""
	state = init_add_schedule_state()
	message_text = get_text("schedule_ask_name")
	msg = send_message(message=message_text, reply_markup=create_simple_keyboard("button_cancel"))
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)

def show_schedule_container_selection(user_id: int, action: str):
	"""Show container selection for schedule"""
	schedule_state = load_schedule_state(user_id)
	if schedule_state:
		# Check if there are available containers
		available_containers = _get_available_containers()
		if not available_containers:
			send_message(message=get_text("error_no_containers_available"))
			clear_schedule_state(user_id)
			return

		schedule_state["step"] = "ask_container"

		# Delete previous message if exists
		if schedule_state.get("last_message_id"):
			try:
				delete_message(schedule_state.get("last_message_id"))
			except:
				pass

		# Build message with summary
		message_text = _build_schedule_summary(schedule_state)
		message_text += f"\n\n{get_text('schedule_ask_container')}"

		markup = InlineKeyboardMarkup(row_width=2)
		# The choice travels as an index, with the name and the host kept in the
		# state: a reference plus a name would not fit in 64 bytes of
		# callback_data for every container someone might have.
		for idx, (entry, container) in enumerate(available_containers):
			label = container.name
			if not host_registry.is_single_host():
				label = f'{container.name} · {entry.get("alias", entry["id"])}'
			markup.add(InlineKeyboardButton(label, callback_data=f"scheduleSelectContainer|{idx}"))
			schedule_state[f"container_{idx}"] = container.name
			schedule_state[f"container_host_{idx}"] = entry["id"]
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		msg = send_message(message=message_text, reply_markup=markup)
		schedule_state["last_message_id"] = msg.message_id if msg else None
		save_schedule_state(user_id, schedule_state)

def ask_schedule_prune_show_output(user_id: int, state: dict):
	"""Asks whether a scheduled prune should report what it removed."""
	state["step"] = "ask_show_output_prune"
	if state.get("last_message_id"):
		try:
			delete_message(state.get("last_message_id"))
		except:
			pass

	message_text = _build_schedule_summary(state)
	message_text += f"\n\n{get_text('schedule_ask_show_output')}"

	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(
		InlineKeyboardButton(get_text("button_yes"), callback_data="scheduleSelectPruneShowOutput|yes"),
		InlineKeyboardButton(get_text("button_no"), callback_data="scheduleSelectPruneShowOutput|no")
	)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	msg = send_message(message=message_text, reply_markup=markup)
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)


def ask_schedule_prune_host(user_id: int, state: dict):
	"""
	Asks which host a scheduled prune will clean.

	A prune task picks no container, so nothing else in the flow would say
	where it runs.

	Asked after the object type, which is where the summary lists it. Matching
	the order means the summary only ever grows downwards as the answers come
	in, so the last thing answered is the last line. Interactive /prune asks
	the machine first instead, because that one deletes on the spot; defining a
	task deletes nothing until it runs, and the confirmation shows everything.
	"""
	state["step"] = "ask_prune_host"
	if state.get("last_message_id"):
		try:
			delete_message(state.get("last_message_id"))
		except:
			pass

	message_text = _build_schedule_summary(state)
	message_text += f'\n\n{get_text("pick_a_host")}'

	markup = InlineKeyboardMarkup(row_width=1)
	for entry in host_registry.hosts():
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}',
			callback_data=f'scheduleSelectHost|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	msg = send_message(message=message_text, reply_markup=markup)
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)


def ask_schedule_prune_type(user_id: int, state: dict):
	"""Asks which kind of unused object a scheduled prune will remove."""
	state["step"] = "ask_prune_type"
	if state.get("last_message_id"):
		try:
			delete_message(state.get("last_message_id"))
		except:
			pass

	message_text = _build_schedule_summary(state)
	message_text += f"\n\n{get_text('schedule_ask_prune_type')}"

	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(*[
		InlineKeyboardButton(get_text(f"schedule_prune_{kind}"),
							callback_data=f"scheduleSelectPruneType|{kind}")
		for kind in ("containers", "images", "networks", "volumes")
	])
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	msg = send_message(message=message_text, reply_markup=markup)
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)


def is_valid_cron(cron_expr: str) -> bool:
	"""Validate cron expression"""
	try:
		croniter(cron_expr)
		return True
	except:
		return False

def confirm_schedule_creation(user_id: int, state: dict):
	# A task that acts on Docker always ends up with a host, so the executor
	# never has to guess: a prune on a single-host setup never got asked. A
	# mute is the bot's own notifications and gets none.
	if state.get("action") in HOST_SCOPED_SCHEDULE_ACTIONS and not state.get("host"):
		state["host"] = host_registry.local_host_id()
		save_schedule_state(user_id, state)
	elif state.get("action") not in HOST_SCOPED_SCHEDULE_ACTIONS:
		state["host"] = None
		save_schedule_state(user_id, state)
	"""Show confirmation of schedule creation"""
	# Delete previous message if exists
	if state.get("last_message_id"):
		try:
			delete_message(state.get("last_message_id"))
		except:
			pass

	# Use the centralized summary builder
	message_text = get_text("schedule_confirm_title") + "\n\n"
	message_text += _build_schedule_summary(state)

	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(
		InlineKeyboardButton(get_text("button_confirm"), callback_data="scheduleConfirm"),
		InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar")
	)

	send_message(message=message_text, reply_markup=markup)

def handle_schedule_flow(user_id: int, user_input: str, state: dict, chat_id: int = None, user_message_id: int = None):
	"""Handle the schedule creation flow and editing"""
	step = state.get("step")
	chatId = chat_id  # Make chatId available in the function

	# Delete user message after processing
	if user_message_id and chat_id:
		try:
			delete_message(user_message_id, chat_id)
		except:
			pass

	# Check if this is an edit operation
	if state.get("field"):
		field = state.get("field")
		schedule_name = state.get("schedule_name")
		schedule = schedule_manager.get_schedule(schedule_name)

		if not schedule:
			send_message(message=get_text("error_invalid_selection"))
			clear_schedule_state(user_id)
			return

		# Delete previous message if exists
		if state.get("last_message_id"):
			try:
				delete_message(state.get("last_message_id"))
			except:
				pass

		# Handle field editing
		if field == "name":
			# Validate new name doesn't already exist
			if user_input != schedule_name and schedule_manager.get_schedule(user_input):
				send_message(message=get_text("schedule_name_exists"))
				# Re-ask for name
				message_text = f"<b>{get_text('schedule_edit_name')}</b>\n\n"
				message_text += f"{get_text('schedule_ask_name')}\n"
				message_text += f"<i>{get_text('current_value')}: {schedule_name}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, name=user_input)
			send_message(message=get_text("schedule_updated_success", user_input))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return
		elif field == "cron":
			# Validate cron expression
			if not is_valid_cron(user_input):
				# Delete previous message if exists
				if state.get("last_message_id"):
					try:
						delete_message(state.get("last_message_id"))
					except:
						pass

				# Re-ask for cron with error message
				schedule = schedule_manager.get_schedule(schedule_name)
				current_cron = schedule.get('cron', '* * * * *')
				message_text = f"❌ <b>{get_text('schedule_invalid_cron')}</b>\n\n"
				message_text += f"<b>{get_text('schedule_edit_cron')}</b>\n\n"
				message_text += f"{get_text('schedule_ask_cron')}\n"
				message_text += f"<i>{get_text('current_value')}: {current_cron}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, cron=user_input)
			send_message(message=get_text("schedule_updated_success", schedule_name))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return
		elif field == "container":
			schedule_manager.update_schedule(schedule_name, container=user_input)
			send_message(message=get_text("schedule_updated_success", schedule_name))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return
		elif field == "command":
			schedule_manager.update_schedule(schedule_name, command=user_input)
			send_message(message=get_text("schedule_updated_success", schedule_name))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return
		elif field == "minutes":
			# Validate minutes is a number
			try:
				minutes = int(user_input)
				if minutes <= 0:
					# Delete previous message if exists
					if state.get("last_message_id"):
						try:
							delete_message(state.get("last_message_id"))
						except:
							pass

					# Re-ask for minutes with error message
					schedule = schedule_manager.get_schedule(schedule_name)
					current_minutes = schedule.get('minutes', '')
					message_text = f"❌ <b>{get_text('schedule_invalid_minutes')}</b>\n\n"
					message_text += f"<b>{get_text('schedule_edit_minutes')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_minutes')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(user_id, state)
					return
			except:
				# Delete previous message if exists
				if state.get("last_message_id"):
					try:
						delete_message(state.get("last_message_id"))
					except:
						pass

				# Re-ask for minutes with error message
				schedule = schedule_manager.get_schedule(schedule_name)
				current_minutes = schedule.get('minutes', '')
				message_text = f"❌ <b>{get_text('schedule_invalid_minutes')}</b>\n\n"
				message_text += f"<b>{get_text('schedule_edit_minutes')}</b>\n\n"
				message_text += f"{get_text('schedule_ask_minutes')}\n"
				message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, minutes=minutes)
			send_message(message=get_text("schedule_updated_success", schedule_name))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return

	if step == "ask_name":
		# Validate name doesn't already exist
		if schedule_manager.get_schedule(user_input):
			# Delete previous message if exists
			if state.get("last_message_id"):
				try:
					delete_message(state.get("last_message_id"))
				except:
					pass

			# Show error message with re-ask for name in the same message
			message_text = f"❌ <b>{get_text('schedule_name_exists')}</b>\n\n"
			message_text += get_text("schedule_ask_name")

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = send_message(message=message_text, reply_markup=markup)
			state["last_message_id"] = msg.message_id if msg else None
			save_schedule_state(user_id, state)
			return

		state["name"] = user_input
		state["step"] = "ask_cron"

		# Delete previous message if exists
		if state.get("last_message_id"):
			try:
				delete_message(state.get("last_message_id"))
			except:
				pass

		# Build message with summary
		message_text = f"<b>{get_text('schedule_label_name')}:</b> {user_input}\n\n"
		message_text += get_text("schedule_ask_cron")

		markup = InlineKeyboardMarkup(row_width=1)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		msg = send_message(message=message_text, reply_markup=markup)
		state["last_message_id"] = msg.message_id if msg else None
		save_schedule_state(user_id, state)

	elif step == "ask_cron":
		# Validate cron expression
		if not is_valid_cron(user_input):
			# Delete previous message if exists
			if state.get("last_message_id"):
				try:
					delete_message(state.get("last_message_id"))
				except:
					pass

			# Show error message with re-ask for cron in the same message
			message_text = f"❌ <b>{get_text('schedule_invalid_cron')}</b>\n\n"
			# Add current progress
			if state.get("name"):
				message_text += f"<b>{get_text('schedule_label_name')}:</b> {state.get('name')}\n\n"
			message_text += get_text("schedule_ask_cron")

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = send_message(message=message_text, reply_markup=markup)
			state["last_message_id"] = msg.message_id if msg else None
			save_schedule_state(user_id, state)
			return

		state["cron"] = user_input
		state["step"] = "ask_action"

		# Delete previous message if exists
		if state.get("last_message_id"):
			try:
				delete_message(state.get("last_message_id"))
			except:
				pass

		# Build message with summary
		message_text = _build_schedule_summary(state)
		message_text += f"\n\n{get_text('schedule_ask_action')}"

		markup = InlineKeyboardMarkup(row_width=2)
		markup.add(
			InlineKeyboardButton("run", callback_data="scheduleSelectAction|run"),
			InlineKeyboardButton("stop", callback_data="scheduleSelectAction|stop"),
			InlineKeyboardButton("restart", callback_data="scheduleSelectAction|restart"),
			InlineKeyboardButton("mute", callback_data="scheduleSelectAction|mute"),
			InlineKeyboardButton("exec", callback_data="scheduleSelectAction|exec"),
			InlineKeyboardButton("prune", callback_data="scheduleSelectAction|prune")
		)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		msg = send_message(message=message_text, reply_markup=markup)
		state["last_message_id"] = msg.message_id if msg else None
		save_schedule_state(user_id, state)

	elif step == "ask_minutes":
		# Validate minutes is a number
		try:
			minutes = int(user_input)
			if minutes <= 0:
				# Delete previous message if exists
				if state.get("last_message_id"):
					try:
						delete_message(state.get("last_message_id"))
					except:
						pass

				# Show error message with re-ask for minutes in the same message
				message_text = f"❌ <b>{get_text('schedule_invalid_minutes')}</b>\n\n"
				# Add current progress
				message_text += _build_schedule_summary(state)
				message_text += f"\n\n{get_text('schedule_ask_minutes')}"

				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
		except:
			# Delete previous message if exists
			if state.get("last_message_id"):
				try:
					delete_message(state.get("last_message_id"))
				except:
					pass

			# Show error message with re-ask for minutes in the same message
			message_text = f"❌ <b>{get_text('schedule_invalid_minutes')}</b>\n\n"
			# Add current progress
			message_text += _build_schedule_summary(state)
			message_text += f"\n\n{get_text('schedule_ask_minutes')}"

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = send_message(message=message_text, reply_markup=markup)
			state["last_message_id"] = msg.message_id if msg else None
			save_schedule_state(user_id, state)
			return

		state["minutes"] = minutes
		state["step"] = "confirm"

		# Delete previous message if exists
		if state.get("last_message_id"):
			try:
				delete_message(state.get("last_message_id"))
			except:
				pass

		save_schedule_state(user_id, state)
		# For mute action, go directly to confirmation (step 4/5)
		confirm_schedule_creation(user_id, state)

	elif step == "ask_command":
		state["command"] = user_input
		state["step"] = "confirm"

		# Delete previous message if exists
		if state.get("last_message_id"):
			try:
				delete_message(state.get("last_message_id"))
			except:
				pass

		save_schedule_state(user_id, state)
		# For exec action, go to confirmation
		confirm_schedule_creation(user_id, state)

# --- AJUSTES (/settings) -----------------------------------------------------
#
# Booleans the menu can flip directly. Whitelisted rather than taken from the
# callback data as-is, so a crafted callback cannot write arbitrary keys into
# the settings file.
SETTINGS_TOGGLES = {
	"check_updates": "button_settings_check_updates",
	"check_update_stopped_containers": "button_settings_check_stopped",
	"extended_messages": "button_settings_extended_messages",
	"multi_selection": "button_settings_multi_selection",
}

# Which screen each toggle belongs to, so pressing one repaints the screen it
# was pressed on instead of bouncing back to the top level.
SETTINGS_TOGGLE_SCREEN = {
	"extended_messages": "main",
	"multi_selection": "main",
	"check_updates": "updates",
	"check_update_stopped_containers": "updates",
}

def _on_off(value):
	return "✅" if value else "❌"

def _selected_prefix(is_selected):
	"""Marks the option currently in force inside a picker."""
	return "✅ " if is_selected else ""

def _format_interval(hours):
	"""Shows 4.0 as 4 and leaves 0.5 alone, so the menu reads like a number."""
	try:
		hours = float(hours)
	except (TypeError, ValueError):
		return "4"
	return str(int(hours)) if hours == int(hours) else str(hours)

def _toggle_button(field):
	"""A toggle row, showing its state in the label rather than above it."""
	return InlineKeyboardButton(
		f'{_on_off(store.get(f"bot.{field}"))} - {get_text(SETTINGS_TOGGLES[field])}',
		callback_data=f"settingsToggle|{field}"
	)

def _add_navigation(markup, back_callback):
	"""
	The last row of a menu screen: one step back, and out.

	Both, always, and through the same function: a screen with only a way back
	leaves the menu open with no way to dismiss it, and one with only a close
	button makes going up a level mean starting over from /start. Sharing a row
	keeps the two apart from the options above them.
	"""
	markup.row(
		InlineKeyboardButton(get_text("button_back"), callback_data=back_callback),
		InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	return markup

def build_settings():
	"""
	Renders the settings menu.

	Every value lives on its own button rather than in a block of text above
	them: repeating each setting in both places meant reading the same thing
	twice and left the buttons unable to say what they were currently set to.
	The rows are ordered so the three update settings sit together, which is
	what gives "check stopped ones too" something to be about.

	One row per setting, because a label carrying its value is too long to
	share a line without Telegram truncating it.
	"""
	locale_code = language().upper()
	channel = notification_channel()
	interval = _format_interval(store.get("bot.check_update_every_hours"))

	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(
		get_text("settings_row_language", LANGUAGE_NAMES.get(locale_code, locale_code)),
		callback_data="settingsLanguage"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_columns", button_columns()),
		callback_data="settingsColumns"))
	markup.add(_toggle_button("extended_messages"))
	markup.add(_toggle_button("multi_selection"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_updates_on", interval) if store.get("bot.check_updates")
		else get_text("settings_row_updates_off"),
		callback_data="settingsUpdates"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_channel", channel or get_text("settings_not_set")),
		callback_data="settingsAskChannel"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_hosts", len(host_registry.hosts())),
		callback_data="settingsHosts"))
	if channel:
		markup.add(InlineKeyboardButton(get_text("button_settings_clear_channel"), callback_data="settingsClearChannel"))
	_add_navigation(markup, "startMenu")

	# Nothing but the title: every value is on a button, and the file location
	# is answered by the README rather than by a line nobody needs twice a year.
	# The persistence warning stays, because that one is a problem to act on.
	lines = [get_text("settings_title")]
	if not store.is_persistent():
		lines.append("")
		lines.append(get_text("settings_not_persistent"))

	return "\n".join(lines), markup

def build_settings_updates():
	"""
	The update-checking settings, on a screen of their own.

	They get their own screen because "include the stopped containers" cannot
	explain itself in a label: it needs both "in the update check" and "which
	containers", and no name short enough to be a button carries all of that.
	Here the sentence at the top says it once, and the three rows below are
	unambiguously about that.
	"""
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(_toggle_button("check_updates"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_interval", _format_interval(store.get("bot.check_update_every_hours"))),
		callback_data="settingsAskInterval"))
	markup.add(_toggle_button("check_update_stopped_containers"))
	_add_navigation(markup, "settings")

	text = f'{get_text("settings_updates_title")}\n\n{get_text("settings_updates_help")}'
	return text, markup

def build_settings_hosts():
	"""
	The list of Docker hosts, each with whether it answers right now.

	Every host is checked in parallel with a deadline, so opening this with a
	machine unplugged costs a few seconds once instead of hanging for the sum
	of every timeout.
	"""
	statuses = host_registry.status_snapshot()
	markup = InlineKeyboardMarkup(row_width=1)
	for entry in host_registry.hosts():
		ok, _ = statuses.get(entry["id"], (False, ""))
		markup.add(InlineKeyboardButton(
			f'{"🟢" if ok else "🔴"} - {entry.get("alias", entry["id"])}',
			callback_data=f'settingsHost|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_host_add"), callback_data="settingsHostAdd"))
	_add_navigation(markup, "settings")

	text = f'{get_text("settings_hosts_title")}\n\n{get_text("settings_hosts_help")}'
	return text, markup


def build_settings_host(host_id):
	"""
	One host: where it is, whether it answers, and what can be done to it.

	A screen of its own rather than making a press act: removing a host from a
	list is one mistap away from removing the wrong one.
	"""
	entry = host_registry.host(host_id)
	if entry is None:
		return None

	# Only this host: sweeping the fleet to draw one screen means waiting on
	# every other machine as well.
	ok, reason = host_registry.host_status(host_id)
	lines = [get_text("settings_host_title", host_alias(host_id))]
	lines.append(f'<code>{html.escape(entry.get("url", ""))}</code>')
	lines.append("")
	lines.append(get_text("settings_host_ok") if ok else get_text("settings_host_failed"))
	if not ok and reason:
		lines.append(f'<i>{html.escape(str(reason))}</i>')
	if entry.get("local"):
		lines.append("")
		lines.append(get_text("settings_host_is_local"))

	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_host_test"), callback_data=f"settingsHost|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_host_rename"), callback_data=f"settingsHostRename|{host_id}"))
	# The local host has no remove button: the bot itself runs on it.
	if not entry.get("local"):
		markup.add(InlineKeyboardButton(get_text("button_host_remove"), callback_data=f"settingsHostRemove|{host_id}"))
	_add_navigation(markup, "settingsHosts")
	return "\n".join(lines), markup


def build_settings_host_remove(host_id):
	"""Confirmation before dropping a host, since it takes its state with it."""
	entry = host_registry.host(host_id)
	if entry is None:
		return None
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_host_remove_confirm"),
									callback_data=f"settingsHostRemoveConfirm|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data=f"settingsHost|{host_id}"))
	return get_text("settings_host_remove_confirm", host_alias(host_id)), markup


def parse_host_definition(raw):
	"""
	Reads what someone typed when adding a host.

	Accepts "nas ssh://dgongut@nas" and a bare URL, deriving a name from it in
	that case. One prompt instead of two: asking for a name and then a URL is
	twice the taps for something people paste in one go.

	Returns (alias, url), or (None, None) when there is no usable URL.
	"""
	parts = (raw or "").split()
	if not parts:
		return None, None

	url = next((part for part in parts if "://" in part), None)
	if url is None:
		return None, None

	named = [part for part in parts if part != url]
	if named:
		return " ".join(named), url

	# No name given: take the host out of the URL, which is what someone would
	# have called it anyway.
	remainder = url.split("://", 1)[1]
	if remainder.startswith("/"):
		return "local", url
	authority = remainder.split("/", 1)[0]
	return authority.split("@")[-1].split(":")[0] or url, url


def build_settings_screen(screen):
	"""Builds one of the settings screens by name."""
	if screen == "updates":
		return build_settings_updates()
	if screen == "hosts":
		return build_settings_hosts()
	return build_settings()

def send_settings_menu(prefix=None, screen="main"):
	"""Sends a settings screen as a new message, optionally led by a confirmation."""
	text, markup = build_settings_screen(screen)
	if prefix:
		text = f"{prefix}\n\n{text}"
	send_message(message=text, reply_markup=markup)

def render_settings(chat_id, message_id, screen="main"):
	"""Repaints an open settings screen in place."""
	text, markup = build_settings_screen(screen)
	edit_message_text(text, chat_id, message_id, reply_markup=markup)

def build_language_keyboard(with_back=True, mark_current=True):
	"""
	Language keyboard, with each language named in itself.

	Deliberately not flags: Català and Galego have no flag in Unicode, so two of
	the eight would be left without one, and English would force a choice
	between two countries that both speak it. Someone looking for their language
	scans for the word in their own language anyway.
	"""
	current = language().upper() if mark_current else None
	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(*[
		InlineKeyboardButton(
			f'{_selected_prefix(code == current)}{LANGUAGE_NAMES.get(code, code)}',
			callback_data=f"settingsSetLanguage|{code}"
		)
		for code in SUPPORTED_LANGUAGES
	])
	# Nothing on the first run: there is no previous screen to go back to, and
	# closing would leave the bot with no language chosen.
	if with_back:
		_add_navigation(markup, "settings")
	return markup

def show_settings_language(chat_id, message_id):
	"""Language picker reached from the settings menu."""
	edit_message_text(get_text("button_settings_language"), chat_id, message_id,
					reply_markup=build_language_keyboard())

def ask_initial_language():
	"""
	Asks which language to use, on a brand new install only.

	Nothing is marked as current and there is no way back, because at this point
	there is no previous choice to go back to: the default is a fallback the user
	never picked. Choosing repaints the message as the settings menu, so the
	first thing they see is where the rest of the settings live.
	"""
	debug("New install with no language configured: asking for one")
	send_message(message=get_text("settings_choose_language"),
				reply_markup=build_language_keyboard(with_back=False, mark_current=False))

def show_settings_columns(chat_id, message_id):
	"""Column picker, capped at what a Telegram keyboard row holds comfortably."""
	current = button_columns()
	markup = InlineKeyboardMarkup(row_width=4)
	markup.add(*[
		InlineKeyboardButton(
			f'{_selected_prefix(columns == current)}{columns}',
			callback_data=f"settingsSetColumns|{columns}"
		)
		for columns in range(1, 5)
	])
	_add_navigation(markup, "settings")
	edit_message_text(get_text("button_settings_columns"), chat_id, message_id, reply_markup=markup)

def ask_text_input(user_id, field, prompt_key, back_to="main"):
	"""
	Asks for a value that has to be typed rather than picked.

	`back_to` names the settings screen to return to once answered, or is None
	for /mute, which is reached from the main menu and has nothing to go back
	to.
	"""
	debug(f"Running command: ask_text_input({field}) for user {user_id}")
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelTextInput"))
	sent = send_message(message=get_text(prompt_key), reply_markup=markup)
	if sent:
		save_text_input_state(user_id, field, sent.message_id, back_to)

def save_text_input_state(user_id, field, delete_message_id, back_to="main"):
	_save_cache("pending_text_input", user_id, {
		"field": field,
		"deleteMessage": delete_message_id,
		"back_to": back_to,
	})

def load_text_input_state(user_id):
	return _load_cache("pending_text_input", user_id)

def clear_text_input_state(user_id):
	_clear_cache("pending_text_input", user_id)

def apply_settings_text_value(field, raw):
	"""
	Validates and stores a typed setting.

	Returns the confirmation to show, or None when the value was rejected and
	the error has already been sent.
	"""
	if field == "check_update_every_hours":
		try:
			hours = float(raw.replace(",", "."))
		except ValueError:
			hours = 0
		if hours <= 0:
			send_message(message=get_text("settings_invalid_interval"))
			return None
		store.set("bot.check_update_every_hours", hours)
		return get_text("settings_updated")

	if field == "host_add":
		alias_name, url = parse_host_definition(raw)
		if not url:
			send_message(message=get_text("settings_host_invalid"))
			return None
		try:
			entry = host_registry.add_host(alias_name, url)
		except host_registry.HostRejected as e:
			# Nothing was tried: it is what was typed that cannot be registered.
			key = ("settings_host_duplicate" if e.reason == "duplicate"
				   else "settings_host_bad_scheme")
			send_message(message=get_text(key, html.escape(url)))
			return None
		except host_registry.HostUnavailable as e:
			# Rejected at the point of adding, while the connection details are
			# still in front of the person typing them.
			send_message(message=get_text("settings_host_unreachable", html.escape(url), html.escape(str(e.reason))))
			return None
		return get_text("settings_host_added", html.escape(entry["alias"]))

	if field.startswith("host_rename:"):
		host_id = field.split(":", 1)[1]
		new_alias = raw.strip()
		if not new_alias:
			send_message(message=get_text("settings_host_invalid"))
			return None
		if not host_registry.rename_host(host_id, new_alias):
			send_message(message=get_text("error_invalid_selection"))
			return None
		return get_text("settings_updated")

	if field == "notification_channel":
		# Verify the bot can actually reach the channel before saving it.
		# Storing an id it cannot post to would send every notification into
		# the void, with nothing in the interface to explain why.
		try:
			bot.get_chat(raw)
		except Exception as e:
			debug(f"Rejected notification channel {raw}: {e}")
			send_message(message=get_text("settings_channel_unreachable", raw))
			return None
		store.set("bot.notification_channel", raw)
		return get_text("settings_updated")

	warning(f"Unknown setting field: {field}")
	return None

# --- MENÚ PRINCIPAL (/start) ---------------------------------------------
#
# Commands grouped by what someone is trying to do. A button per command would
# be 21 buttons: the wall of text the menu used to be, only with bigger targets
# and more scrolling. Two levels keep the first screen to seven buttons with
# nothing tucked away.
#
# Labels come from the locale files, so this structure needs no translating.
START_CATEGORY_COMMANDS = {
	"containers": ("list", "run", "stop", "restart", "delete"),
	"diagnose": ("logs", "logfile", "info", "exec"),
	"updates": ("checkupdate", "updateall", "changetag"),
	"system": ("prune", "ports", "compose"),
	"automation": ("schedule", "mute"),
	"about": ("version", "donate", "donors"),
}

# The first screen, in order. A category opens a submenu; a command runs
# straight away, which is what /settings wants since it opens a menu of its own
# and a category holding one item would be a level for nothing.
START_ROOT = (
	("category", "containers"),
	("category", "diagnose"),
	("category", "updates"),
	("category", "system"),
	("category", "automation"),
	("command", "settings"),
	("category", "about"),
)

def _start_summary():
	"""
	Container counts for the header, across every reachable host.

	The host count is only shown when there is more than one: with a single
	host saying "servers: 1" is noise about a concept that machine's owner
	never had to think about.

	When some host is not answering the header says so as a fraction rather
	than counting only what replied — the container totals are missing that
	machine's containers, and a bare number would present a partial count as
	the whole picture.

	None when nothing answers at all: a header claiming zero containers would
	read as "everything is gone" rather than "I cannot see anything".
	"""
	try:
		sections = hosts_with_containers()
		containers = [container
					for _, _, host_containers in sections
					for container in host_containers]
		configured = host_registry.hosts()
		if not containers and configured:
			return None
	except Exception as e:
		debug(f"Could not count containers for the start menu: {e}")
		return None
	running = sum(1 for c in containers if c.status in ("running", "restarting"))
	stopped = len(containers) - running
	if len(configured) <= 1:
		return get_text("start_summary", len(containers), running, stopped)
	if len(sections) == len(configured):
		return get_text("start_summary_hosts", len(containers), running, stopped, len(configured))
	return get_text("start_summary_hosts_partial", len(containers), running, stopped,
					len(sections), len(configured))

def _start_button(kind, key):
	if kind == "category":
		return InlineKeyboardButton(get_text(f"start_cat_{key}"), callback_data=f"startCategory|{key}")
	return InlineKeyboardButton(get_text(f"start_cmd_{key}"), callback_data=f"startCommand|{key}")

def build_start_menu():
	"""The first screen: what the bot is watching, and the ways in."""
	lines = [get_text("start_title")]
	summary = _start_summary()
	if summary:
		lines.append(summary)

	markup = InlineKeyboardMarkup(row_width=button_columns())
	markup.add(*[_start_button(kind, key) for kind, key in START_ROOT])
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	return "\n".join(lines), markup

def build_start_category(key):
	"""One category's commands, plus the way back. None if the key is unknown."""
	commands = START_CATEGORY_COMMANDS.get(key)
	if not commands:
		return None
	markup = InlineKeyboardMarkup(row_width=button_columns())
	markup.add(*[
		InlineKeyboardButton(get_text(f"start_cmd_{name}"), callback_data=f"startCommand|{name}")
		for name in commands
	])
	_add_navigation(markup, "startMenu")
	return f'{get_text("start_title")}\n{get_text(f"start_cat_{key}")}', markup

def send_start_menu():
	"""Sends the main menu as a new message."""
	text, markup = build_start_menu()
	send_message(message=text, reply_markup=markup)

def render_start_menu(chat_id, message_id):
	"""Repaints an open menu back to its first screen."""
	text, markup = build_start_menu()
	edit_message_text(text, chat_id, message_id, reply_markup=markup)

def render_start_category(chat_id, message_id, key):
	"""Repaints an open menu as one category."""
	built = build_start_category(key)
	if not built:
		warning(f"Unknown start menu category: {key}")
		return
	text, markup = built
	edit_message_text(text, chat_id, message_id, reply_markup=markup)


# Commands, filled in by importing the module that defines them. The core does
# not import it: the dependency has to go one way, or the module that needs
# everything here could not be imported from here.
COMMAND_ACTIONS = {}


def register_command(name, action):
	"""Registers the function that runs when `name` is typed or pressed."""
	if name in COMMAND_ACTIONS:
		raise ValueError(f"Command {name} is already registered")
	COMMAND_ACTIONS[name] = action


@bot.message_handler(commands=["start", "list", "run", "stop", "restart", "delete", "exec", "checkupdate", "updateall", "changetag", "logs", "logfile", "compose", "mute", "schedule", "settings", "info", "version", "donate", "donors", "prune", "ports"])
@with_reply_context
def command_controller(message):
	userId = message.from_user.id
	comando = message.text.split(' ', 1)[0]
	if not is_allowed_origin(message.chat, userId):
		debug(f"Command {comando} ignored: chat {message.chat.id} is neither an administrator private chat nor TELEGRAM_GROUP")
		return

	messageId = message.id
	container_id = None
	container_name = None
	if not comando in ('/mute', f'/mute@{bot.get_me().username}'
					,'/schedule', f'/schedule@{bot.get_me().username}'
					,'/settings', f'/settings@{bot.get_me().username}'):
		argument = " ".join(message.text.split()[1:])
		if argument:
			# Searched across every host. Names rarely repeat between machines,
			# and when they do the user is asked rather than guessed at.
			container_id, container_name, candidates = resolve_container_argument(argument)
			if candidates:
				action_type = COMMAND_PICKERS.get(comando.split("@", 1)[0])
				if action_type:
					send_container_disambiguation(action_type, container_name, candidates)
					return
				# No picker for this command: fall back to the first match
				# rather than refusing, which is what it did before hosts.
				entry, container = candidates[0]
				container_id = container_ref(entry["id"], container)
			if container_id:
				debug(f"Argument {argument!r} resolved to {container_id}")

	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1
	debug(f"COMMAND: {comando} | USER: {userId} | CHAT: {message.chat.id} | THREAD: {message_thread_id}")

	# The topic filter only applies to groups: a private chat with the bot has no
	# topics, so commands sent there must always be accepted
	if message.chat.type != "private" and message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if not is_admin(userId):
		warning(f"User {userId} ({message.from_user.username}) tried to use admin command without permission")
		send_message(chat_id=userId, message=get_text("user_not_admin"))
		return

	if comando not in ('/start', f'/start@{bot.get_me().username}'):
		delete_message(messageId)

	# List containers
	# /start is the menu itself. Everything else goes through the table, so a
	# typed command and its button in that menu run the same function.
	if comando in ('/start', f'/start@{bot.get_me().username}'):
		send_start_menu()
		return

	action = COMMAND_ACTIONS.get(comando.split('@', 1)[0])
	if action is None:
		debug(f"No action registered for {comando}")
		return

	argument = None
	parts = message.text.split(maxsplit=1)
	if len(parts) > 1:
		argument = parts[1].strip() or None

	action(user_id=userId, chat_id=message.chat.id, container_id=container_id,
			container_name=container_name, argument=argument)
def answer_callback_quietly(callback_id, text=None, show_alert=False):
	"""
	Stops Telegram's spinner on a button, tolerating a failure to do so.

	This is presentation: it acknowledges the press. Letting it abort the
	handler meant a dropped connection to Telegram turned an action into
	nothing at all, which is far worse than a spinner that keeps turning for a
	few seconds.
	"""
	try:
		bot.answer_callback_query(callback_id, text=text, show_alert=show_alert)
		return True
	except Exception as e:
		debug(f"Could not answer callback {callback_id}: {e}")
		return False


@bot.callback_query_handler(func=lambda mensaje: True)
@with_reply_context
def button_controller(call):
	"""
	Dispatches a button press to its registered handler.

	Everything specific to a callback now lives with the callback, so this does
	only what is common to all of them: check the caller, resolve the
	arguments, and decide whether the message survives the press.
	"""
	try:
		messageId = call.message.id
		chatId = call.message.chat.id
		userId = call.from_user.id

		if not is_admin(userId):
			warning(f"User {userId} ({call.from_user.username}) tried to use admin command without permission")
			send_message(chat_id=userId, message=get_text("user_not_admin"))
			answer_callback_quietly(call.id, text="❌")
			return

		spec, args = callback_registry.parse(call.data)
		ctx = callback_registry.Context(
			call=call, comando=spec.name, messageId=messageId, chatId=chatId, userId=userId, **args)

		# Answered before running so Telegram stops showing its spinner. The
		# update toggles answer themselves instead, with their own feedback.
		#
		# Failing to answer must not abort the press: the answer is cosmetic,
		# the action is not. A momentary blip talking to Telegram used to turn
		# a button into "error processing request" with nothing having run.
		if spec.answer_immediately:
			answer_callback_quietly(call.id)

		# Which host this press is about. A container reference says so
		# directly; a project hash resolves to one; everything else means the
		# host the bot runs on.
		ctx.hostId = host_registry.local_host_id()

		# A callback that names a container carries a reference to it.
		if ctx.containerId:
			ctx.hostId = ref_host(ctx.containerId)
			if not ctx.containerName:
				ctx.containerName = get_container_name(chatId, messageId, ctx.containerId)
				if not ctx.containerName:
					close_multi_action_menu(chatId, messageId)
					send_message(message=get_text("container_does_not_exist", ref_id(ctx.containerId)))
					debug(f"Container {ctx.containerId} not found in cache or Docker")
					return

		# Notifications for containers that were later recreated (e.g. via compose)
		# keep a stale short id in their callback_data. If we know the name, resolve
		# it back to the current id so the operation targets the live container
		# instead of failing with "container does not exist".
		#
		# Looked up on the reference's own host and nowhere else. Searching
		# every host could land the action on a container of the same name on
		# a different machine, which is the one mistake here that would be both
		# silent and destructive.
		if ctx.containerId and ctx.containerName and not spec.project_arg:
			current_id = find_container_id_on_host(ctx.hostId, ctx.containerName)
			if current_id:
				if current_id != ref_id(ctx.containerId):
					debug(f"Resolved stale id {ctx.containerId} to {current_id} via name {ctx.containerName} on {ctx.hostId}")
				ctx.containerId = make_ref(ctx.hostId, current_id)
			else:
				close_multi_action_menu(chatId, messageId)
				send_message(message=get_text("container_does_not_exist", ctx.containerName))
				debug(f"Container {ctx.containerName} (stale {ctx.containerId}) not found on {ctx.hostId}")
				return

		# For project-scoped callbacks the argument carries a short hash of the
		# project name, to stay under Telegram's 64-byte callback_data limit.
		# The hash resolves to the host as well, because two machines can run a
		# project of the same name.
		if spec.project_arg and ctx.containerName:
			resolved_host, resolved_name = resolve_project_hash(ctx.containerName)
			if not resolved_name:
				send_message(message=get_text("error_project_not_found", ctx.containerName))
				debug(f"Unknown project hash: {ctx.containerName}")
				return
			ctx.hostId = resolved_host
			ctx.containerName = resolved_name

		# A /run, /stop or /restart menu left open for multi-selection: the message
		# survives this press and its keyboard is rebuilt once the action is done
		ctx.multiAction = load_multi_action(chatId, messageId) if spec.multi_action else None

		debug(f"BUTTON: {spec.name} | USER: {userId} | CHAT: {chatId}")
	except Exception as e:
		error(f"Error initializing callback: [{str(e)}]")
		answer_callback_quietly(call.id, text=get_text("error_callback_processing"), show_alert=True)
		return

	try:
		# Unless the handler repaints it, the message the press came from is
		# replaced by whatever the handler opens, so it goes away first.
		if not spec.keeps_message and not ctx.multiAction:
			delete_message(messageId)

		spec.handler(ctx)
	except Exception as e:
		error(f"Error executing callback [{spec.name}]: [{str(e)}]")
		try:
			send_message(message=get_text("error_callback_processing"))
		except:
			pass

@bot.message_handler(func=lambda message: True)
@with_reply_context
def handle_text(message):
	userId = message.from_user.id
	username = message.from_user.username
	pending = load_command_request_state(userId)
	pending_port_check = load_port_check_request_state(userId)
	pending_input = load_text_input_state(userId)
	schedule_state = load_schedule_state(userId)
	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1

	if not is_allowed_origin(message.chat, userId):
		debug(f"Message ignored: chat {message.chat.id} is neither an administrator private chat nor TELEGRAM_GROUP")
		return

	# The topic filter only applies to groups: a private chat with the bot has no
	# topics, so messages sent there must always be accepted
	if message.chat.type != "private" and message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if not is_admin(userId):
		warning(f"User {userId} ({username}) tried to use admin command without permission")
		send_message(chat_id=userId, message=get_text("user_not_admin"))
		return

	if pending:
		command_text = message.text.strip()
		containerId = pending.get("containerId")
		containerName = pending.get("containerName")
		deleteMessage = pending.get("deleteMessage")
		delete_message(deleteMessage)
		delete_message(message.message_id, message.chat.id)
		clear_command_request_state(userId)
		confirm_execute_command(containerId, containerName, command_text)
	elif pending_port_check:
		port_text = message.text.strip()
		deleteMessage = pending_port_check.get("deleteMessage")
		delete_message(deleteMessage)
		delete_message(message.message_id, message.chat.id)
		clear_port_check_request_state(userId)

		# Validate port number
		try:
			port_number = int(port_text)
			if port_number < 1 or port_number > 65535:
				send_message(message=get_text("ports_invalid_range"))
				return

			# Check the port
			is_available, result_message = check_specific_port(port_number)
			send_message(message=result_message)
		except ValueError:
			send_message(message=get_text("ports_invalid_number"))
	elif pending_input:
		raw_value = message.text.strip()
		field = pending_input.get("field")
		delete_message(pending_input.get("deleteMessage"))
		delete_message(message.message_id, message.chat.id)
		clear_text_input_state(userId)
		if field == "mute_minutes":
			COMMAND_ACTIONS["/mute"](user_id=userId, chat_id=message.chat.id, argument=raw_value)
		else:
			confirmation = apply_settings_text_value(field, raw_value)
			send_settings_menu(prefix=confirmation, screen=pending_input.get("back_to") or "main")
	elif schedule_state:
		handle_schedule_flow(userId, message.text.strip(), schedule_state, message.chat.id, message.message_id)
	else:
		pass

def _execute_container_action(action, containerId, containerName, from_schedule=False):
	"""
	Generic function to execute container actions (run, stop, restart).

	Args:
		action: Action name ('run', 'stop', 'restart')
		containerId: Container ID
		containerName: Container name
		from_schedule: Whether called from schedule

	Returns:
		None on success, or the error message that was already sent to the user
	"""
	# Method names rather than bound methods: a bound method carries the host
	# it was taken from, so the map would always act on the local one.
	action_map = {
		'run': {'debug': 'run', 'message_key': 'starting', 'method': 'start_container'},
		'stop': {'debug': 'stop', 'message_key': 'stopping', 'method': 'stop_container'},
		'restart': {'debug': 'restart', 'message_key': 'restarting', 'method': 'restart_container'},
	}

	config = action_map.get(action)
	if not config:
		error(f"Unknown action: {action}")
		return f"Unknown action: {action}"

	debug(f"Running command: {config['debug']} for container {containerName}")
	x = send_message(message=get_text(config['message_key'], containerName))
	try:
		owner = manager_for(containerId)
	except host_registry.HostUnavailable as e:
		message = get_text("host_unreachable", host_alias(ref_host(containerId)), html.escape(str(e.reason)))
		send_message(message=message)
		return message
	result = getattr(owner, config['method'])(
		container_id=ref_id(containerId), container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)
	return result

def run(containerId, containerName, from_schedule=False):
	return _execute_container_action('run', containerId, containerName, from_schedule)

def stop(containerId, containerName, from_schedule=False):
	return _execute_container_action('stop', containerId, containerName, from_schedule)

def restart(containerId, containerName, from_schedule=False):
	return _execute_container_action('restart', containerId, containerName, from_schedule)

def _execute_compose_project_action(action, project_name, show_extended=True, host_id=None):
	"""
	Generic function to execute compose project actions (run, stop, restart).

	Args:
		action: Action name ('run', 'stop', 'restart')
		project_name: Project name
		show_extended: Whether to show extended messages
	"""
	debug(f"Running command: {action}_compose_project for project {project_name}")

	# Get project information, on the host the project lives on. A broad except
	# because a host can build its client and fail on the next call.
	try:
		owner = manager(host_id or host_registry.local_host_id())
		project_info = owner.get_project_info(project_name)
	except Exception as e:
		debug(f"Could not read project {project_name}: {e}")
		project_info = None
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Get containers sorted by dependencies
	containers = project_info.containers
	sorted_containers = owner.compose_manager.sort_containers_by_dependencies(containers)

	# Per-action configuration
	if action == 'restart':
		send_message(message=get_text("restarting_project", project_name))
		# Stop containers in reverse order
		for container in reversed(sorted_containers):
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=get_text("stopping_service", service_name))
			try:
				container.stop(timeout=10)
			except Exception as e:
				debug(f"Error stopping {service_name}: {e}")
				if show_extended:
					send_message(message=get_text("error_stopping_service", service_name))
		# Start containers in the correct order
		for container in sorted_containers:
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=get_text("starting_service", service_name))
			try:
				container.start()
			except Exception as e:
				debug(f"Error starting {service_name}: {e}")
				if show_extended:
					send_message(message=get_text("error_starting_service", service_name))
		send_message(message=get_text("project_restarted_success", project_name))

	elif action == 'run':
		send_message(message=get_text("starting_project", project_name))
		# Start containers in the correct order
		for container in sorted_containers:
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=get_text("starting_service", service_name))
			try:
				container.start()
			except Exception as e:
				debug(f"Error starting {service_name}: {e}")
				if show_extended:
					send_message(message=get_text("error_starting_service", service_name))
		send_message(message=get_text("project_started_success", project_name))

	elif action == 'stop':
		send_message(message=get_text("stopping_project", project_name))
		# Stop containers in reverse order
		for container in reversed(sorted_containers):
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=get_text("stopping_service", service_name))
			try:
				container.stop(timeout=10)
			except Exception as e:
				debug(f"Error stopping {service_name}: {e}")
				if show_extended:
					send_message(message=get_text("error_stopping_service", service_name))
		send_message(message=get_text("project_stopped_success", project_name))

def restart_compose_project(project_name, host_id=None):
	"""Restarts a complete Docker Compose project respecting dependency order."""
	_execute_compose_project_action('restart', project_name, host_id=host_id)

def _container_has_healthcheck(container):
	"""True if `container` has a non-NONE healthcheck configured."""
	try:
		container.reload()
	except Exception:
		return False
	hc = (container.attrs.get('Config') or {}).get('Healthcheck') or {}
	test = hc.get('Test')
	return bool(test) and test != ['NONE']


def _wait_for_container_healthy(container, timeout_seconds=180):
	"""
	Polls the container until its healthcheck reports 'healthy'.
	Returns True if healthy before the deadline, False otherwise (timeout,
	container gone, or no healthcheck status reported).
	"""
	deadline = time.time() + timeout_seconds
	while time.time() < deadline:
		try:
			container.reload()
		except Exception:
			return False
		state = container.attrs.get('State') or {}
		health = state.get('Health') or {}
		status = health.get('Status')
		if status is None:
			return False
		if status == 'healthy':
			return True
		time.sleep(1)
	return False


def _wait_for_container_exit_success(container, timeout_seconds=180):
	"""
	Polls the container until it exits and returns True if ExitCode == 0,
	False on timeout, non-zero exit, or container gone.
	"""
	deadline = time.time() + timeout_seconds
	while time.time() < deadline:
		try:
			container.reload()
		except Exception:
			return False
		state = container.attrs.get('State') or {}
		if state.get('Status') == 'exited':
			return state.get('ExitCode') == 0
		time.sleep(1)
	return False


_NAMESPACE_HOSTCONFIG_FIELDS = (
	('NetworkMode', 'network_mode'),
	('IpcMode', 'ipc_mode'),
	('PidMode', 'pid_mode'),
	('UTSMode', 'uts_mode'),
)


def _compute_namespace_overrides(dep_container, old_parent_id, new_parent_id):
	"""
	Inspects a dependent container's HostConfig and returns a dict of config
	overrides (in extract_container_config keys) to rewrite any namespace
	field that points at `container:<old_parent_id>` to point at
	`container:<new_parent_id>` instead. Returns {} when no rewrite is needed.
	"""
	if not old_parent_id or not new_parent_id:
		return {}
	try:
		dep_container.reload()
	except Exception as e:
		debug(f"Could not reload {dep_container.name} to inspect HostConfig for namespace overrides: {e}")
	host_config = dep_container.attrs.get('HostConfig') or {}
	old_refs = {f"container:{old_parent_id}", f"container:{old_parent_id[:12]}"}
	new_ref = f"container:{new_parent_id}"
	overrides = {}
	for hc_field, cfg_key in _NAMESPACE_HOSTCONFIG_FIELDS:
		val = host_config.get(hc_field) or ''
		if val in old_refs:
			overrides[cfg_key] = new_ref
	# Dependents sharing a namespace with the (now-replaced) parent typically
	# exit when the parent is removed. Force the recreation to start the new
	# container regardless of the current (exited) status, mirroring the
	# stop+start behaviour applied to non-namespace dependents.
	if overrides:
		overrides['is_running'] = True
	return overrides


def restart_dependents_after_update(project_name, updated_service_name, new_parent_container=None, old_parent_id=None, send_fn=None, host_id=None):
	"""
	Restarts only the services that depend (directly or transitively) on the
	updated service. Services unrelated to the updated one are left untouched.
	The updated container itself is NOT restarted again (it was already
	recreated and started by perform_update).

	When at least one dependent declared `condition: service_healthy` (or
	`service_completed_successfully`) on the updated service in its compose
	file, we honor that condition by waiting on `new_parent_container` before
	starting the dependents back up. Falls back to immediate start when no
	such condition is declared, or when the parent has no healthcheck.

	When a dependent's `HostConfig.NetworkMode` / `IpcMode` / `PidMode` /
	`UTSMode` points at the old parent container id (e.g. compose's
	`network_mode: "service:<parent>"`), a simple stop+start cannot
	re-attach the dead namespace. Those dependents are instead recreated
	in-place via `recreate_with_overrides` so their HostConfig is rewritten
	to reference the new parent id.

	Args:
		project_name: Compose project name
		updated_service_name: Service name of the container that was just updated
		new_parent_container: The freshly recreated container for the updated
			service, used to wait on its healthcheck. Optional.
		old_parent_id: The container id of the parent BEFORE the update, used
			to detect dependents whose HostConfig still references the dead id.
			Optional; when None, namespace recreation is skipped.
		send_fn: Function used to send user-facing messages. Receives the message
			text as its only argument. If None, defaults to send_message (admin chat).
	"""
	if send_fn is None:
		send_fn = lambda msg: send_message(message=msg)

	debug(f"Restarting dependents of service {updated_service_name} in project {project_name}")

	# Get project information, on the host the updated container lives on
	owner = manager(host_id or host_registry.local_host_id())
	project_info = owner.get_project_info(project_name)
	if not project_info:
		send_fn(get_text("error_project_not_found", project_name))
		return

	# Get only the transitive dependents of the updated service
	dependents = owner.compose_manager.get_transitive_dependents(
		project_info.containers, updated_service_name
	)

	if not dependents:
		debug(f"No dependents found for service {updated_service_name}, nothing to restart")
		return

	dependent_count = len(dependents)

	# Prefer the parent's container name (what shows up in `docker ps`) over
	# the compose service name when addressing the user; keep the service
	# name in debug logs for compose-level traceability.
	parent_display_name = new_parent_container.name if new_parent_container is not None else updated_service_name

	# Initial message
	send_fn(get_text("restarting_dependent_services", parent_display_name, dependent_count))

	# Pre-compute which dependents need full recreation because they share a
	# namespace with the (now-replaced) parent container id. Those are NOT
	# stopped here; recreate_with_overrides will handle their lifecycle so
	# the extracted config keeps is_running=True for them.
	new_parent_id = new_parent_container.id if new_parent_container is not None else None
	namespace_overrides = {}
	for container in dependents:
		overrides = _compute_namespace_overrides(container, old_parent_id, new_parent_id)
		if overrides:
			namespace_overrides[container.id] = overrides

	# Stop dependents in reverse order (deepest dependents first), skipping
	# those that will be fully recreated. Per-service stop progress is logged
	# only in debug; the Telegram user gets a single summary at the end (and
	# explicit error messages if a stop fails).
	for container in reversed(dependents):
		if container.id in namespace_overrides:
			continue
		service_name = container.labels.get('com.docker.compose.service', container.name)
		try:
			container.stop(timeout=10)
		except Exception as e:
			debug(f"Error stopping {service_name}: {e}")
			if store.get("bot.extended_messages"):
				send_fn(get_text("error_stopping_service", container.name))

	# Honor depends_on conditions before restarting: if any dependent declared
	# `service_healthy` / `service_completed_successfully` on the updated
	# service, wait for the new parent to satisfy it.
	needs_healthy = False
	needs_completed = False
	for container in dependents:
		cond = owner.compose_manager.get_dependency_condition(container, updated_service_name)
		if cond == 'service_healthy':
			needs_healthy = True
		elif cond == 'service_completed_successfully':
			needs_completed = True

	if new_parent_container is not None and (needs_healthy or needs_completed):
		if needs_healthy:
			if _container_has_healthcheck(new_parent_container):
				debug(f"Waiting for {updated_service_name} to become healthy before starting dependents")
				if store.get("bot.extended_messages"):
					send_fn(get_text("waiting_for_healthy", parent_display_name))
				t0 = time.time()
				ok = _wait_for_container_healthy(new_parent_container, timeout_seconds=180)
				if ok:
					debug(f"{updated_service_name} became healthy after {time.time()-t0:.1f}s")
					if store.get("bot.extended_messages"):
						send_fn(get_text("healthy_ready", parent_display_name))
				else:
					debug(f"Timed out waiting for {updated_service_name} to be healthy; starting dependents anyway")
			else:
				debug(f"{updated_service_name} declared as service_healthy dependency but has no healthcheck; not waiting")
		if needs_completed:
			debug(f"Waiting for {updated_service_name} to exit successfully before starting dependents")
			_wait_for_container_exit_success(new_parent_container, timeout_seconds=180)

	# Start dependents in dependency order. Dependents whose namespace
	# references the old parent id are recreated in-place (rewriting the
	# reference) instead of merely started, since starting a container with
	# a stale `container:<id>` namespace fails. Per-service start progress is
	# logged only in debug; recreation IS announced (it's an unusual event)
	# and errors are reported in both paths.
	for container in dependents:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		overrides = namespace_overrides.get(container.id)
		if overrides:
			debug(f"Recreating {service_name} to rewrite namespace -> new parent {updated_service_name} (id={new_parent_id[:12]}); overrides={list(overrides.keys())}")
			if store.get("bot.extended_messages"):
				send_fn(get_text("recreating_namespace_dependent", container.name))
			try:
				owner.recreate_with_overrides(container.id, container.name, overrides)
			except Exception as e:
				debug(f"Error recreating {service_name}: {e}")
				if store.get("bot.extended_messages"):
					send_fn(get_text("error_recreating_namespace_dependent", container.name))
			continue
		try:
			container.start()
		except Exception as e:
			debug(f"Error starting {service_name}: {e}")
			if store.get("bot.extended_messages"):
				send_fn(get_text("error_starting_service", container.name))

	# Final message
	send_fn(get_text("dependent_services_restarted_success", parent_display_name, dependent_count))


def perform_container_update(container_id, container_name, tag=None, send_fn=None):
	"""
	Single entry point for container updates. Wraps the full flow:
	  1. Capture Compose project/service info BEFORE the update (container is recreated).
	  2. Send the "updating" progress message via send_fn.
	  3. Delegate the actual update to docker_manager.update().
	  4. Send the final result message via send_fn.
	  5. If the container belongs to a Compose project, restart only the services
	     that depend on it (directly or transitively).

	All update triggers (/update, /changetag, auto-update daemon) should go
	through this function so the behaviour is identical regardless of origin.

	Args:
		container_id: Container ID to update.
		container_name: Container name (used in user-facing messages).
		tag: Optional new image tag (used by the /changetag flow).
		send_fn: Function used to send user-facing messages. Receives the message
			text and returns the sent telegram Message (or None to suppress).
			If None, defaults to send_message (admin chat).
	"""
	if send_fn is None:
		send_fn = lambda msg: send_message(message=msg)

	# Capture Compose info BEFORE the update, since the container object is recreated.
	project_name = None
	updated_service_name = None
	old_parent_id = None
	# The host comes from the reference and is used for everything below: the
	# container, its update and its project's dependents all live on the same
	# machine, and resolving it once keeps them from drifting apart.
	host_id = ref_host(container_id)
	owner = manager(host_id)
	try:
		container_obj = owner.client.containers.get(ref_id(container_id))
		project_name = ComposeDetector.get_project_name(container_obj)
		updated_service_name = ComposeDetector.get_service_name(container_obj)
		old_parent_id = container_obj.id
	except Exception as e:
		debug(f"Could not pre-fetch Compose info for {container_name}: {e}")

	# Send the initial "updating" progress message
	# Both the progress line and the result say which machine, the same as the
	# start/stop notifications do. Without it an update report on a multi-host
	# setup does not say where it happened.
	label = host_label(host_id)
	x = send_fn(f'{label}{get_text("updating", container_name)}')

	# Perform the actual update
	result = owner.update(container_id=ref_id(container_id), container_name=container_name, message=x, bot=bot, tag=tag)

	# Remove the progress message and send the final result. The chat is taken
	# from the sent message itself: send_fn may target the notification channel
	# (auto-update) instead of the chat being used.
	if x is not None:
		delete_message(x.message_id, x.chat.id)
	send_fn(f"{label}{result}")

	# Restart dependents if applicable. Resolve the freshly recreated container
	# by name (Docker enforces unique container names, and perform_update keeps
	# the original name) so dependents can wait on its healthcheck when their
	# compose `depends_on` declared `condition: service_healthy`.
	if project_name and updated_service_name:
		new_parent_container = None
		try:
			new_parent_container = owner.client.containers.get(container_name)
		except Exception as e:
			debug(f"Could not fetch new container after update for {container_name}: {e}")
		restart_dependents_after_update(
			project_name,
			updated_service_name,
			new_parent_container=new_parent_container,
			old_parent_id=old_parent_id,
			send_fn=send_fn,
			host_id=host_id,
		)

def run_compose_project(project_name, host_id=None):
	"""Starts a complete Docker Compose project respecting dependency order."""
	_execute_compose_project_action('run', project_name, host_id=host_id)

def stop_compose_project(project_name, host_id=None):
	"""Stops a complete Docker Compose project respecting dependency order."""
	_execute_compose_project_action('stop', project_name, host_id=host_id)

def delete_compose_project(project_name, host_id=None):
	"""
	Deletes a complete Docker Compose project.

	Args:
		project_name: Compose project name to delete
	"""
	debug(f"Running command: delete_compose_project for project {project_name}")

	# Get project information, on the host the project lives on
	try:
		project_info = manager(host_id or host_registry.local_host_id()).get_project_info(project_name)
	except Exception as e:
		debug(f"Could not read project {project_name}: {e}")
		project_info = None
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Get the project's containers
	containers = project_info.containers
	container_count = len(containers)

	# Initial message
	send_message(message=get_text("deleting_project", project_name, container_count))

	# Delete each container
	for container in containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if store.get("bot.extended_messages"):
			send_message(message=get_text("deleting_service", service_name))
		try:
			container.remove(force=True)
		except Exception as e:
			debug(f"Error deleting {service_name}: {e}")
			send_message(message=get_text("error_deleting_service", service_name))

	# Final message
	send_message(message=get_text("project_deleted_success", project_name, container_count))

def logs(containerId, containerName):
	debug(f"Running command: logs for container {containerName}")
	result = manager_for(containerId).show_logs(container_id=ref_id(containerId), container_name=containerName)
	send_message(message=result, reply_markup=create_simple_keyboard("button_close"))

def log_file(containerId, containerName):
	debug(f"Running command: log_file for container {containerName}")
	markup = create_simple_keyboard("button_delete")
	result = manager_for(containerId).show_logs_raw(container_id=ref_id(containerId), container_name=containerName)
	if isinstance(result, str):
		fichero_temporal = get_temporal_file(result, f'logs_{containerName}')
		x = send_message(message=get_text("loading_file"))
		send_document(document=fichero_temporal, reply_markup=markup, caption=get_text("logs", containerName))
		if x:
			delete_message(x.message_id)
	else:
		send_message(message=result, reply_markup=markup)

def get_temporal_file(data, fileName):
	fichero_temporal = io.BytesIO(data.encode('utf-8'))
	fecha_hora_actual = datetime.now()
	formato = "%Y.%m.%d_%H.%M.%S"
	fecha_hora_formateada = fecha_hora_actual.strftime(formato)
	fichero_temporal.name = f"{fileName}_{fecha_hora_formateada}.txt"
	return fichero_temporal

def mute(minutes):
	"""Mute the bot with thread-safe lock to prevent race conditions."""
	global _unmute_timer

	if minutes == 0:
		unmute()
		return

	# Use lock to prevent race conditions with unmute timer
	with _mute_lock:
		# Cancel any existing unmute timer
		if _unmute_timer is not None:
			_unmute_timer.cancel()
			_unmute_timer = None

		store.state_set("mute_until", time.time() + minutes * 60)
		debug(f"Bot muted for {minutes} minutes")
		if store.get("bot.extended_messages"):
			if minutes == 1:
				send_message(message=get_text("muted_singular"))
			else:
				send_message(message=get_text("muted", minutes))
		_unmute_timer = threading.Timer(minutes * 60, unmute)
		_unmute_timer.start()

def unmute():
	"""Unmute the bot with thread-safe lock to prevent race conditions."""
	global _unmute_timer

	# Use lock to prevent race conditions with mute timer
	with _mute_lock:
		# Cancel any existing unmute timer
		if _unmute_timer is not None:
			_unmute_timer.cancel()
			_unmute_timer = None

		store.state_set("mute_until", 0)
		debug("Bot unmuted")
		if store.get("bot.extended_messages"):
			send_message(message=get_text("unmuted"))

def _mute_until():
	"""Epoch second the mute expires at, or 0 when not muted."""
	try:
		return float(store.state_get("mute_until") or 0)
	except (TypeError, ValueError):
		return 0

def is_muted():
	return time.time() < _mute_until()

def check_mute():
	"""Restores a mute that was still running when the bot last stopped."""
	global _unmute_timer

	mute_until = _mute_until()
	if mute_until == 0:
		return

	if time.time() >= mute_until:
		# The mute expired while the bot was down.
		unmute()
	elif _unmute_timer is None:
		# Still muted: re-arm the timer for whatever is left of it.
		_unmute_timer = threading.Timer(mute_until - time.time(), unmute)
		_unmute_timer.start()

def compose(containerId, containerName):
	debug(f"Running command: compose for container {containerName}")
	markup = create_simple_keyboard("button_delete")
	result = manager_for(containerId).get_docker_compose(container_id=ref_id(containerId), container_name=containerName)
	if isinstance(result, str) and not result.startswith("Error"):
		fichero_temporal = io.BytesIO(result.encode('utf-8'))
		fichero_temporal.name = "docker-compose.txt"
		x = send_message(message=get_text("loading_file"))
		send_document(document=fichero_temporal, reply_markup=markup, caption=get_text("compose", containerName))
		if x:
			delete_message(x.message_id)
	else:
		send_message(message=result, reply_markup=markup)

def info(containerId, containerName):
	debug(f"Running command: info for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	x = send_message(message=get_text("obtaining_info", containerName))
	result, possible_update = manager_for(containerId).get_info(container_id=ref_id(containerId), container_name=containerName)
	delete_message(x.message_id)
	if possible_update:
		markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{containerId}"))
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	send_message(message=result, reply_markup=markup)

def _confirm_prune_action(prune_type, host_id=None):
	"""
	Asks for confirmation before pruning, naming the host when there is more
	than one: this deletes things and there is no undo.
	"""
	host_id = host_id or host_registry.local_host_id()
	# Callers pass either "images" or "Images"; the callback name is capitalised.
	kind = prune_type.capitalize()
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"),
									callback_data=f"prune|prune{kind}|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	question = get_text(f"confirm_prune_{kind.lower()}")
	if not host_registry.is_single_host():
		question = f'{get_text("list_host_header", host_alias(host_id))}\n{question}'
	send_message(message=question, reply_markup=markup)


def confirm_prune(prune_type, host_id=None):
	"""Asks for confirmation before pruning one kind of object on one host."""
	_confirm_prune_action(prune_type, host_id)

def confirm_delete(containerId, containerName):
	debug(f"Running command: confirm_delete for container {containerName}")
	markup = create_confirm_cancel_keyboard(f"delete|{containerId}", "button_confirm_delete")
	send_message(message=get_text("confirm_delete", containerName), reply_markup=markup)

def ask_command(userId, containerId, containerName):
	debug(f"Running command: ask_command for container {containerName}")
	markup = create_simple_keyboard("button_cancel", "cancelAskCommand")
	x = send_message(message=get_text("prompt_enter_command", containerName), reply_markup=markup)
	if x:
		save_command_request_state(userId, containerId, containerName, x.message_id)

def confirm_execute_command(containerId, containerName, command):
	debug(f"Running command: confirm_exec for container {containerName} with command [{command}]")
	commandId = save_command_cache(command)
	markup = create_confirm_cancel_keyboard(f"exec|{containerId}|{commandId}", "button_confirm", f"cancelExec|{commandId}")
	send_message(message=get_text("confirm_exec", containerName, html.escape(command)), reply_markup=markup)

def execute_command(containerId, containerName, command, sendMessage=True):
	debug(f"Running command: exec for container {containerName} with command [{command}]")
	result = manager_for(containerId).execute_command(container_id=ref_id(containerId), container_name=containerName, command=command)
	if sendMessage:
		max_length = 3500
		escaped_command = html.escape(command)
		if len(result) <= max_length:
			send_message(message=get_text("executed_command", containerName, escaped_command, html.escape(result)))
		else:
			first_part = result[:max_length]
			send_message(message=get_text("executed_command", containerName, escaped_command, html.escape(first_part)))
			for i in range(max_length, len(result), max_length):
				part = result[i:i + max_length]
				send_message(message=f"<pre><code>{html.escape(part)}</code></pre>")

def confirm_change_tag(containerId, containerName, tag):
	debug(f"Running command: confirm_change_tag for container {containerName} to tag {tag}")

	# Show loading message
	loading_msg = send_message(message=get_text("fetching_image_data"))

	# Get detailed comparison
	comparison = get_image_comparison(containerId, containerName, new_tag=tag)

	# Delete loading message
	if loading_msg:
		delete_message(loading_msg.message_id)

	if not comparison:
		# Fallback to simple confirmation if comparison fails
		markup = create_confirm_cancel_keyboard(f"changeTag|{containerId}|{tag}", f"button_confirm_change_tag", "cerrar", "button_cancel")
		send_message(message=get_text("confirm_change_tag", containerName, tag), reply_markup=markup)
		return

	# Check if images are identical
	if comparison['current_digest'] == comparison['new_digest']:
		# Same image, just show info
		message = f"""📦 <b>{containerName}</b>

ℹ️ <b>Información:</b>
   {get_text('update_tag')}: <code>{comparison['current_tag']}</code> → <code>{comparison['new_tag']}</code>
   {get_text('update_created')}: {comparison['current_date']}
   {get_text('update_size')}: {comparison['current_size']}
   {get_text('update_digest')}: <code>{comparison['current_digest']}</code>

⚠️ Ambos tags apuntan a la misma imagen (mismo digest)."""
	else:
		# Different images, show full comparison
		# Build changes list
		changes = []
		changes.append(f"{get_text('update_size_change')}: {comparison['size_diff']}")
		if comparison['days_diff'] > 0:
			changes.append(f"{comparison['days_diff']} {get_text('update_days_newer')}")
		elif comparison['days_diff'] < 0:
			changes.append(f"{abs(comparison['days_diff'])} {get_text('update_days_older')}")

		# Format changes (use bullet points only if multiple items)
		if len(changes) > 1:
			changes_text = "\n   • " + "\n   • ".join(changes)
		else:
			changes_text = "\n   " + changes[0]

		message = f"""📦 <b>{containerName}</b>

📌 <b>{get_text('update_current_image')}:</b>
   {get_text('update_tag')}: <code>{comparison['current_tag']}</code>
   {get_text('update_created')}: {comparison['current_date']}
   {get_text('update_size')}: {comparison['current_size']}
   {get_text('update_digest')}: <code>{comparison['current_digest']}</code>

🆕 <b>{get_text('update_new_image')}:</b>
   {get_text('update_tag')}: <code>{comparison['new_tag']}</code>
   {get_text('update_created')}: {comparison['new_date']}
   {get_text('update_size')}: {comparison['new_size']}
   {get_text('update_digest')}: <code>{comparison['new_digest']}</code>

📊 <b>{get_text('update_changes')}:</b>{changes_text}"""

	if comparison['description']:
		message += f"\n\n📝 <b>{get_text('update_description')}:</b>\n{comparison['description']}"

	if comparison['registry_url']:
		# Use dynamic text based on registry
		if comparison['registry_name']:
			link_text = f"{get_text('update_more_info_registry', comparison['registry_name'])}"
		else:
			link_text = get_text('update_more_info')
		message += f"\n\n🔗 <a href=\"{comparison['registry_url']}\">{link_text}</a>"

	# Create keyboard with tag parameter in button text
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_change_tag", tag), callback_data=f"changeTag|{containerId}|{tag}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=message, reply_markup=markup)

def change_tag_container(containerId, containerName):
	try:
		markup = InlineKeyboardMarkup(row_width=button_columns())
		container = manager_for(containerId).client.containers.get(ref_id(containerId))
		repo = container.attrs['Config']['Image'].split(":")[0]
		tags = get_docker_tags(repo)

		if not tags:
			error(f"Could not get tags for image {repo}")
			send_message(message=get_text("error_getting_tags", repo))
			return

		botones = []
		for tag in tags:
			callback_data = f"confirmChangeTag|{containerId}|{tag}"
			if len(callback_data) <= 64:
				botones.append(InlineKeyboardButton(tag, callback_data=callback_data))
			else:
				warning(f"Tag name too long for container {containerName}: {tag}")

		markup.add(*botones)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		send_message(message=get_text("change_tag", containerName), reply_markup=markup)
	except Exception as e:
		error(f"Error changing tag for container {containerName}. Error: [{e}]")
		send_message(message=get_text("error_changing_tag", containerName))

def get_image_comparison(containerId, containerName, new_tag=None):
	"""
	Get detailed comparison between current and new image.

	Args:
		containerId: Container ID
		containerName: Container name
		new_tag: Optional new tag to compare against. If None, pulls the same tag to check for updates.

	Returns:
		dict with comparison information or None if error
	"""
	try:
		owner = manager_for(containerId)
		container = owner.client.containers.get(ref_id(containerId))
		current_image = container.image
		current_tag = container.attrs['Config']['Image']

		# Current image info
		current_digest = current_image.id.replace('sha256:', '')[:12]
		current_size = current_image.attrs.get('Size', 0)
		current_created = current_image.attrs.get('Created', '')

		# Determine what image to pull for comparison
		if new_tag:
			# Changing tag: use the new tag
			repo = current_tag.split(':')[0]
			tag_to_pull = f"{repo}:{new_tag}"
		else:
			# Checking for update: pull the same tag
			tag_to_pull = current_tag

		# Pull new image (without applying to container)
		debug(f"Pulling image {tag_to_pull} for comparison")
		new_image = owner.client.images.pull(tag_to_pull)

		# New image info
		new_digest = new_image.id.replace('sha256:', '')[:12]
		new_size = new_image.attrs.get('Size', 0)
		new_created = new_image.attrs.get('Created', '')

		# Calculate differences
		has_update = current_digest != new_digest
		size_diff = new_size - current_size

		# Format dates
		from datetime import datetime
		try:
			current_date = datetime.fromisoformat(current_created.replace('Z', '+00:00'))
			new_date = datetime.fromisoformat(new_created.replace('Z', '+00:00'))
			current_date_str = current_date.strftime('%Y-%m-%d')
			new_date_str = new_date.strftime('%Y-%m-%d')
			days_diff = (new_date - current_date).days
		except:
			current_date_str = get_text('update_date_unknown')
			new_date_str = get_text('update_date_unknown')
			days_diff = 0

		# Format size difference
		if size_diff > 0:
			size_diff_str = f"+{sizeof_fmt(size_diff)}"
		elif size_diff < 0:
			size_diff_str = f"-{sizeof_fmt(abs(size_diff))}"
		else:
			size_diff_str = get_text('update_no_size_change')

		# Get Docker Hub description (optional, may fail for private images)
		description = get_dockerhub_description(tag_to_pull)

		# Clean up description: remove Markdown formatting and truncate
		if description:
			# Remove Markdown headers (# ## ### with or without space)
			description = re.sub(r'^#+\s*', '', description, flags=re.MULTILINE)
			# Remove Markdown links [text](url) -> text
			description = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', description)
			# Remove Markdown bold/italic (**text** or *text* or __text__)
			description = re.sub(r'[*_]{1,2}([^*_]+)[*_]{1,2}', r'\1', description)
			# Remove extra whitespace and newlines
			description = ' '.join(description.split())
			# Remove any remaining # symbols
			description = description.replace('#', '')
			# Truncate if still too long
			if len(description) > 200:
				description = description[:200].rsplit(' ', 1)[0] + '...'
			# Final cleanup: if description is just "..." or empty, set to None
			if not description or description.strip() in ['...', '.', '']:
				description = None

		# Build registry URL
		registry_url, registry_name = build_registry_url(tag_to_pull)

		# Keep the pulled image cached locally so that the subsequent update
		# (or change-tag) operation does not need to re-download it.

		return {
			'has_update': has_update,
			'current_tag': current_tag,
			'new_tag': tag_to_pull,
			'current_digest': current_digest,
			'current_size': sizeof_fmt(current_size),
			'new_digest': new_digest,
			'new_size': sizeof_fmt(new_size),
			'size_diff': size_diff_str,
			'current_date': current_date_str,
			'new_date': new_date_str,
			'days_diff': days_diff,
			'description': description,
			'registry_url': registry_url,
			'registry_name': registry_name
		}
	except Exception as e:
		error(f"Error getting update comparison for {containerName}: {e}")
		return None

def sanitize_dockerhub_description(text):
	"""
	Sanitize a Docker Hub description so it renders cleanly inside a
	Telegram HTML message:
	- Convert <br> and common block-level closing tags to line breaks.
	- Strip all remaining HTML tags.
	- Decode HTML entities (e.g. &amp; -> &).
	- Escape characters that are special in Telegram HTML (& < >).
	- Collapse runs of whitespace and blank lines.
	"""
	if not text:
		return text

	# Line breaks for <br> variants and common block-level closers
	text = re.sub(r'<\s*br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
	text = re.sub(r'</\s*(p|div|li|tr|h[1-6])\s*>', '\n', text, flags=re.IGNORECASE)

	# Strip any remaining HTML tags
	text = re.sub(r'<[^>]+>', '', text)

	# Decode HTML entities before re-escaping for Telegram
	text = html.unescape(text)

	# Escape Telegram HTML special chars (interpolated into an HTML message)
	text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

	# Collapse whitespace: tabs/spaces -> single space, max one blank line
	text = re.sub(r'[ \t]+', ' ', text)
	text = re.sub(r' *\n *', '\n', text)
	text = re.sub(r'\n{3,}', '\n\n', text)

	return text.strip()

def get_dockerhub_description(image_tag):
	"""
	Get description from Docker Hub API.
	Returns truncated description or None if not available.
	"""
	try:
		import requests

		# Parse image name
		# Format: [registry/]repository[:tag]
		# Examples: nginx:latest, library/nginx:latest, ghcr.io/user/image:tag

		parts = image_tag.split('/')

		# Check if it's a Docker Hub image (no registry or docker.io)
		if len(parts) == 1 or (len(parts) == 2 and '.' not in parts[0]):
			# Docker Hub image
			if len(parts) == 1:
				# Official image (e.g., nginx:latest)
				namespace = 'library'
				repo_with_tag = parts[0]
			else:
				# User image (e.g., user/image:latest)
				namespace = parts[0]
				repo_with_tag = parts[1]

			# Remove tag
			repo = repo_with_tag.split(':')[0]

			# Call Docker Hub API
			url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/"
			response = requests.get(url, timeout=5)

			if response.status_code == 200:
				data = response.json()
				full_description = data.get('full_description', '')
				if full_description:
					# Sanitize before truncating so we don't break HTML/entities
					sanitized = sanitize_dockerhub_description(full_description)
					if sanitized and len(sanitized) > 300:
						return sanitized[:300].rsplit(' ', 1)[0] + '...'
					return sanitized or None

		return None
	except Exception as e:
		debug(f"Could not get Docker Hub description: {e}")
		return None

def build_registry_url(image_tag):
	"""
	Build registry URL for an image (Docker Hub, GitHub Container Registry, etc.).
	Returns tuple (url, registry_name) or (None, None) if unknown.
	"""
	try:
		parts = image_tag.split('/')

		# Check if it's a Docker Hub image
		if len(parts) == 1 or (len(parts) == 2 and '.' not in parts[0]):
			if len(parts) == 1:
				# Official image - use /_/ format
				repo_with_tag = parts[0]
				repo = repo_with_tag.split(':')[0]
				return (f"https://hub.docker.com/_/{repo}", "Docker Hub")
			else:
				# User image - use /r/namespace/repo format
				namespace = parts[0]
				repo_with_tag = parts[1]
				repo = repo_with_tag.split(':')[0]
				return (f"https://hub.docker.com/r/{namespace}/{repo}", "Docker Hub")

		# GitHub Container Registry (ghcr.io)
		elif len(parts) >= 2 and parts[0] == 'ghcr.io':
			# ghcr.io/owner/repo -> https://github.com/owner/repo/pkgs/container/repo
			owner = parts[1]
			repo_with_tag = parts[2] if len(parts) > 2 else parts[1]
			repo = repo_with_tag.split(':')[0]
			return (f"https://github.com/{owner}/{repo}/pkgs/container/{repo}", "GitHub")

		# Google Container Registry (gcr.io)
		elif len(parts) >= 2 and parts[0] in ['gcr.io', 'us.gcr.io', 'eu.gcr.io', 'asia.gcr.io']:
			# gcr.io/project/image -> https://gcr.io/project/image
			image_path = '/'.join(parts[1:]).split(':')[0]
			return (f"https://{parts[0]}/{image_path}", "Google Container Registry")

		# Quay.io
		elif len(parts) >= 2 and parts[0] == 'quay.io':
			# quay.io/namespace/repo -> https://quay.io/repository/namespace/repo
			namespace = parts[1]
			repo_with_tag = parts[2] if len(parts) > 2 else parts[1]
			repo = repo_with_tag.split(':')[0]
			return (f"https://quay.io/repository/{namespace}/{repo}", "Quay.io")

		# Amazon ECR Public
		elif len(parts) >= 2 and 'public.ecr.aws' in parts[0]:
			# public.ecr.aws/namespace/repo -> https://gallery.ecr.aws/namespace/repo
			namespace = parts[1]
			repo_with_tag = parts[2] if len(parts) > 2 else parts[1]
			repo = repo_with_tag.split(':')[0]
			return (f"https://gallery.ecr.aws/{namespace}/{repo}", "Amazon ECR Public")

		else:
			# Unknown registry
			return (None, None)
	except:
		return (None, None)

def confirm_update(containerId, containerName):
	debug(f"Running command: confirm_update for container {containerName}")

	# Show loading message
	loading_msg = send_message(message=get_text("fetching_image_data"))

	# Get detailed comparison
	comparison = get_image_comparison(containerId, containerName)

	# Delete loading message
	if loading_msg:
		delete_message(loading_msg.message_id)

	if not comparison:
		# Fallback to simple confirmation if comparison fails
		markup = create_confirm_cancel_keyboard(f"update|{containerId}", "button_confirm_update")
		send_message(message=get_text("confirm_update", containerName), reply_markup=markup)
		return

	# Check if images are identical (no update available)
	if comparison['current_digest'] == comparison['new_digest']:
		# Same image, no update needed - save to cache as updated
		image_with_tag = comparison['current_tag']
		save_container_update_status(image_with_tag, containerName, False, ref_host(containerId))
		send_message(message=f'{host_label(ref_host(containerId))}{get_text("already_updated", containerName)}')
		return

	# Build changes list
	changes = []
	changes.append(f"{get_text('update_size_change')}: {comparison['size_diff']}")
	if comparison['days_diff'] > 0:
		changes.append(f"{comparison['days_diff']} {get_text('update_days_newer')}")
	elif comparison['days_diff'] < 0:
		changes.append(f"{abs(comparison['days_diff'])} {get_text('update_days_older')}")

	# Format changes (use bullet points only if multiple items)
	if len(changes) > 1:
		changes_text = "\n   • " + "\n   • ".join(changes)
	else:
		changes_text = "\n   " + changes[0]

	# Build detailed message
	message = f"""📦 <b>{containerName}</b>

📌 <b>{get_text('update_current_image')}:</b>
   {get_text('update_tag')}: <code>{comparison['current_tag']}</code>
   {get_text('update_created')}: {comparison['current_date']}
   {get_text('update_size')}: {comparison['current_size']}
   {get_text('update_digest')}: <code>{comparison['current_digest']}</code>

🆕 <b>{get_text('update_new_image')}:</b>
   {get_text('update_tag')}: <code>{comparison['new_tag']}</code>
   {get_text('update_created')}: {comparison['new_date']}
   {get_text('update_size')}: {comparison['new_size']}
   {get_text('update_digest')}: <code>{comparison['new_digest']}</code>

📊 <b>{get_text('update_changes')}:</b>{changes_text}"""

	if comparison['description']:
		message += f"\n\n📝 <b>{get_text('update_description')}:</b>\n{comparison['description']}"

	if comparison['registry_url']:
		# Use dynamic text based on registry
		if comparison['registry_name']:
			link_text = f"{get_text('update_more_info_registry', comparison['registry_name'])}"
		else:
			link_text = get_text('update_more_info')
		message += f"\n\n🔗 <a href=\"{comparison['registry_url']}\">{link_text}</a>"

	markup = create_confirm_cancel_keyboard(f"update|{containerId}", "button_confirm_update")
	send_message(message=message, reply_markup=markup)

def confirm_update_selected(chatId, messageId):
	containers, selected = load_update_data(chatId, messageId)
	# Build id -> name map from cached containers (list of [id, name] pairs)
	id_to_name = {cid: cname for cid, cname in containers}
	# If only one container is selected, show the detailed comparison view
	if len(selected) == 1:
		container_id = next(iter(selected))
		container_name = id_to_name.get(container_id)
		if container_id and container_name:
			clear_update_data(chatId, messageId)
			confirm_update(container_id, container_name)
			return
	containersToUpdate = ""
	for cid in selected:
		containersToUpdate += f"· <b>{id_to_name.get(cid, cid)}</b>\n"
	markup = create_confirm_cancel_keyboard(f"updateSelected|{messageId}", "button_confirm_update")
	send_message(message=get_text("confirm_update_all", containersToUpdate), reply_markup=markup)

def build_generic_keyboard(container_available, selected_containers, originalMessageId, action_type, button_text, button_text_all=None):
	"""Generic keyboard builder for the multi-select update flow.

	container_available: list of [id, name] pairs.
	selected_containers: set of container IDs.
	"""
	markup = InlineKeyboardMarkup(row_width=button_columns())
	botones = []
	for cid, cname in container_available:
		icono = ICON_CONTAINER_MARKED_FOR_UPDATE if cid in selected_containers else ICON_CONTAINER_MARK_FOR_UPDATE
		botones.append(
			InlineKeyboardButton(f"{icono} {cname}", callback_data=f"toggle{action_type}|{cid}")
		)
	markup.add(*botones)

	fixed_buttons = []
	if selected_containers:
		fixed_buttons.append(InlineKeyboardButton(button_text, callback_data=f"confirm{action_type}Selected|{originalMessageId}"))
	else:
		# Use button_text_all if provided, otherwise use button_text
		text_to_use = button_text_all if button_text_all else button_text
		fixed_buttons.append(InlineKeyboardButton(text_to_use, callback_data=f"toggle{action_type}All"))

	fixed_buttons.append(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	markup.add(*fixed_buttons)
	return markup

def count_actionable_buttons(markup):
	"""
	Number of container/project buttons in a menu keyboard, ignoring the last
	row (which always holds the fixed cancel/close/back buttons). Zero means the
	menu has nothing left to act on.
	"""
	rows = markup.keyboard or []
	return sum(len(row) for row in rows[:-1])

def build_hierarchical_keyboard(containers, action_type, bot_container_name, filter_standalone_status=None, filter_projects_with_all_status=None, marked_names=None, host_id=None):
	"""
	Build hierarchical keyboard with Compose projects and standalone containers.
	Level 1: Shows projects (📦) and standalone containers (🐳)

	Args:
		containers: List of container objects
		action_type: Type of action (Restart, Stop, Run)
		bot_container_name: Name of the bot container to exclude
		filter_standalone_status: Optional list of statuses to filter standalone containers (e.g., ['running', 'restarting'])
		filter_projects_with_all_status: Optional list of statuses - hide projects where ALL containers have these statuses
		marked_names: Optional set of container names already acted upon in a
			multi-action session; they show a check instead of their status emoji

	Returns:
		InlineKeyboardMarkup: Keyboard with projects and standalone containers
	"""
	host_id = host_id or host_registry.local_host_id()
	owner = manager(host_id)
	markup = InlineKeyboardMarkup(row_width=button_columns())

	# Separate containers into projects and standalone
	project_containers = {}  # {project_name: [containers]}
	standalone_containers = []

	for container in containers:
		if container.name == bot_container_name:
			continue

		labels = container.labels or {}
		project_name = labels.get('com.docker.compose.project')

		if project_name:
			if project_name not in project_containers:
				project_containers[project_name] = []
			project_containers[project_name].append(container)
		else:
			standalone_containers.append(container)

	# Apply rule: projects with only 1 container are shown as standalone
	single_container_projects = []
	for project_name, project_conts in list(project_containers.items()):
		if len(project_conts) == 1:
			standalone_containers.extend(project_conts)
			single_container_projects.append(project_name)

	# Remove single-container projects from project_containers
	for project_name in single_container_projects:
		del project_containers[project_name]

	# Build buttons
	botones = []

	# Add project buttons (sorted)
	for project_name in sorted(project_containers.keys()):
		# Apply project filter if specified (hide projects where ALL containers have the specified statuses)
		if filter_projects_with_all_status:
			project_info = owner.get_project_info(project_name)
			if project_info:
				all_containers = project_info.containers
				# Check if ALL containers in the project have one of the filtered statuses
				if all_containers and all(c.status in filter_projects_with_all_status for c in all_containers):
					continue  # Skip this project

		# Get container count (filtered by status if applicable)
		project_info = owner.get_project_info(project_name)
		if project_info:
			if filter_standalone_status:
				# Count only containers matching the filter
				container_count = sum(1 for c in project_info.containers if c.status in filter_standalone_status)
			else:
				# Show total count
				container_count = project_info.get_container_count()
		else:
			container_count = len(project_containers[project_name])
		botones.append(
			InlineKeyboardButton(
				f"📦 {project_name} ({container_count})",
				callback_data=f"enter{action_type}Project|{register_project_hash(project_name, host_id)}"
			)
		)

	# Action configuration map for standalone containers
	standalone_action_config = {
		'delete': 'confirmDelete',
		'exec': 'askCommand',
		'logs': 'logs',
		'logfile': 'logfile',
		'checkupdate': 'checkUpdate',
		'changetag': 'changeTagContainer',
		'info': 'info',
		'ports': 'ports'
	}

	# Add standalone container buttons (sorted: bot first, then running, then stopped - all alphabetically)
	for container in sort_containers_by_priority(standalone_containers):
		# Apply status filter if specified (only for standalone containers)
		if filter_standalone_status and container.status not in filter_standalone_status:
			continue

		# Get status emoji. Containers already acted upon in a multi-action
		# session show a check instead, so the list stays scannable
		if marked_names and container.name in marked_names:
			status_emoji = ICON_CONTAINER_ACTION_DONE
		else:
			status_emoji = get_status_emoji(container.status, container.name, container)

		# Determine callback action based on action_type
		action_lower = action_type.lower()
		if action_lower in standalone_action_config:
			# Use configured callback for special actions
			callback_action = standalone_action_config[action_lower]
		elif container.status in ['running', 'restarting']:
			# For running containers, use the action type (restart/stop)
			callback_action = action_lower
		else:
			# For stopped containers, always use "run"
			callback_action = "run"

		botones.append(
			InlineKeyboardButton(
				f"{status_emoji} {container.name}",
				callback_data=f"{callback_action}|{container_ref(host_id, container)}"
			)
		)

	markup.add(*botones)

	# Add cancel button. Once something has been acted upon there is nothing
	# left to cancel, only to close
	markup.add(InlineKeyboardButton(get_text("button_close" if marked_names else "button_cancel"), callback_data="cerrar"))

	return markup, standalone_containers

# Every action that offers a level-1 container picker, and the parameters it
# uses. Kept here rather than in each command so that repainting a host picker
# can look them up instead of carrying them in callback_data.
PICKER_ACTIONS = {
	"Run": {
		"container_callback": "run",
		"prompt_key": "start_a_container",
		"empty_key": "no_containers_to_start",
		"comando": "",
		"bot_container_name": CONTAINER_NAME,
		"filter_standalone_status": ["exited", "stopped", "paused", "created"],
		"filter_projects_with_all_status": ["running", "restarting"],
		"multi_action": "Run",
	},
	"Stop": {
		"container_callback": "stop",
		"prompt_key": "stop_a_container",
		"empty_key": "no_containers_to_stop",
		"bot_container_name": CONTAINER_NAME,
		"filter_standalone_status": ["running", "restarting"],
		"filter_projects_with_all_status": ["exited", "stopped", "paused", "created"],
		"multi_action": "Stop",
	},
	"Restart": {
		"container_callback": "restart",
		"prompt_key": "restart_a_container",
		"empty_key": "no_containers_to_restart",
		"bot_container_name": CONTAINER_NAME,
		"multi_action": "Restart",
	},
	"Delete": {
		"container_callback": "confirmDelete",
		"prompt_key": "delete_container",
		"empty_key": "no_containers_to_delete",
		"bot_container_name": CONTAINER_NAME,
	},
	"Logs": {"container_callback": "logs", "prompt_key": "logs_command_container", "empty_key": "no_containers_for_logs"},
	"Logfile": {"container_callback": "logfile", "prompt_key": "show_logsfile", "empty_key": "no_containers_for_logs"},
	"Info": {"container_callback": "info", "prompt_key": "info_command_container", "empty_key": "no_containers_for_info"},
	"Compose": {"container_callback": "compose", "prompt_key": "show_compose", "empty_key": "error_no_containers_available"},
	"CheckUpdate": {
		"container_callback": "checkUpdate",
		"prompt_key": "checkupdate_command_container",
		"empty_key": "no_containers_for_checkupdate",
	},
	"ChangeTag": {
		"container_callback": "changeTagContainer",
		"prompt_key": "change_tag_container",
		"empty_key": "error_no_containers_available",
	},
	"Exec": {
		"container_callback": "askCommand",
		"prompt_key": "exec_command_container",
		"empty_key": "no_containers_for_exec",
		"filter_standalone_status": ["running", "restarting"],
		"filter_projects_with_all_status": ["exited", "paused", "dead", "created"],
	},
}


def send_picker(action_type):
	"""Sends the level-1 picker for one of PICKER_ACTIONS."""
	spec = PICKER_ACTIONS[action_type]
	return send_container_picker(
		action_type,
		spec["prompt_key"],
		spec["empty_key"],
		comando=spec.get("comando", ""),
		bot_container_name=spec.get("bot_container_name"),
		filter_standalone_status=spec.get("filter_standalone_status"),
		filter_projects_with_all_status=spec.get("filter_projects_with_all_status"),
		multi_action=spec.get("multi_action"))


# Which action each command opens a picker for, so a typed argument and a
# button end up in the same place.
COMMAND_PICKERS = {
	"/run": "Run", "/stop": "Stop", "/restart": "Restart", "/delete": "Delete",
	"/logs": "Logs", "/logfile": "Logfile", "/info": "Info", "/compose": "Compose",
	"/checkupdate": "CheckUpdate", "/changetag": "ChangeTag", "/exec": "Exec",
}


def find_containers_by_name(container_name, host_id=None):
	"""
	Every container with this name, as (host_entry, container).

	Names are unique within a daemon but not between them, so this can return
	more than one and the caller has to decide what that means. `host_id`
	restricts the search to one machine.
	"""
	matches = []
	for entry in host_registry.hosts():
		if host_id and entry["id"] != host_id:
			continue
		try:
			owner = manager(entry["id"])
			containers = owner.list_containers()
		except host_registry.HostUnavailable as e:
			debug(f"Skipping {entry.get('alias', entry['id'])} while looking for {container_name}: {e.reason}")
			continue
		except Exception as e:
			debug(f"Could not search {entry.get('alias', entry['id'])}: {e}")
			continue
		for container in containers:
			if container.name == container_name:
				matches.append((entry, container))
	return matches


def resolve_container_argument(argument):
	"""
	Turns what someone typed after a command into a container reference.

	Accepts a bare name, searched across every host, and "ganimedes:plex" to
	name the host outright. Returns (ref, name, candidates):

	  - ref and name when exactly one host has it
	  - candidates, as [(host_entry, container)], when several do
	  - all empty when nothing matches

	Searching everywhere by default is what keeps a single-host bot feeling
	unchanged while a multi-host one needs no extra typing: names rarely repeat
	between machines, and when they do the caller asks instead of guessing.
	"""
	text = (argument or "").strip()
	if not text:
		return None, None, []

	host_id = None
	if CONTAINER_REF_SEPARATOR in text:
		prefix, _, remainder = text.partition(CONTAINER_REF_SEPARATOR)
		host = host_registry.find_by_alias(prefix)
		if host is not None and remainder:
			host_id, text = host["id"], remainder.strip()

	matches = find_containers_by_name(text, host_id)
	if not matches:
		return None, text, []
	if len(matches) == 1:
		entry, container = matches[0]
		return container_ref(entry["id"], container), container.name, []
	return None, text, matches


def send_container_disambiguation(action_type, container_name, candidates):
	"""
	Asks which host was meant, offering only the ones that have that name.

	The buttons are the action's ordinary container buttons, so choosing one
	goes through exactly the same path as picking it from a menu.
	"""
	spec = PICKER_ACTIONS.get(action_type)
	if spec is None or not spec.get("container_callback"):
		warning(f"No container callback for action {action_type}")
		return
	markup = InlineKeyboardMarkup(row_width=1)
	for entry, container in candidates:
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}  ·  {get_status_emoji(container.status, container.name, container)}',
			callback_data=f'{spec["container_callback"]}|{container_ref(entry["id"], container)}'))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	sent = send_message(message=get_text("container_on_several_hosts", container_name), reply_markup=markup)
	if sent:
		# One entry per candidate, each with its own host: they come from
		# different machines, so a single host id would mislabel all but one.
		save_container_refs(sent.chat.id, sent.message_id, [
			(container_ref(entry["id"], container), container.name)
			for entry, container in candidates
		])
	return sent


PRUNE_TYPES = (
	("Containers", "button_containers"),
	("Images", "button_images"),
	("Networks", "button_networks"),
	("Volumes", "button_volumes"),
)


def host_question(action_type):
	"""
	The text of a host-selection screen: what is about to happen, and the
	question.

	Reusing an action's own prompt put its instruction in here too, so /prune
	asked for an object type and for a host in the same message, and /stop said
	"press a project or container" above a list of hosts. The instruction
	belongs on the next screen, where it is actionable.

	The titles are the short labels the /start menu already uses, so this needs
	no strings of its own.
	"""
	title = get_text(f"start_cmd_{action_type.lower()}").replace(" - ", " ", 1)
	return f'{title}\n\n<i>{get_text("pick_a_host")}</i>'


def send_prune_menu():
	"""
	Opens /prune, asking which host first when there is more than one.

	The machine before the object type on purpose: this deletes things, and
	knowing where you are about to delete them is worth a tap.
	"""
	if host_registry.is_single_host():
		send_prune_types(host_registry.local_host_id())
		return

	markup = InlineKeyboardMarkup(row_width=1)
	for entry in host_registry.hosts():
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}',
			callback_data=f'pruneHost|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	send_message(message=host_question("prune"), reply_markup=markup)


def prune_types_keyboard(host_id):
	"""The object-type keyboard for one host."""
	markup = InlineKeyboardMarkup(row_width=button_columns())
	markup.add(*[
		InlineKeyboardButton(get_text(label), callback_data=f"prune|confirmPrune{kind}|{host_id}")
		for kind, label in PRUNE_TYPES
	])
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	return markup


def prune_prompt(host_id):
	"""The /prune prompt, naming the host when there is more than one."""
	if host_registry.is_single_host():
		return get_text("prune_system")
	return f'{get_text("list_host_header", host_alias(host_id))}\n{get_text("prune_system")}'


def send_prune_types(host_id):
	send_message(message=prune_prompt(host_id), reply_markup=prune_types_keyboard(host_id))


def render_prune_types(chat_id, message_id, host_id):
	edit_message_text(prune_prompt(host_id), chat_id, message_id,
					reply_markup=prune_types_keyboard(host_id))


def _picker_has_anything(containers, bot_container_name, filter_standalone_status,
						filter_projects_with_all_status, owner):
	"""
	Whether an action has anything to offer on one host.

	Mirrors the filtering the keyboard applies, so a host button never leads to
	an empty list.
	"""
	for container in containers:
		if bot_container_name and container.name == bot_container_name:
			continue
		project_name = (container.labels or {}).get("com.docker.compose.project")
		if project_name:
			if not filter_projects_with_all_status:
				return True
			info = owner.get_project_info(project_name)
			if not info or not info.containers:
				return True
			if not all(c.status in filter_projects_with_all_status for c in info.containers):
				return True
			continue
		if not filter_standalone_status or container.status in filter_standalone_status:
			return True
	return False


def send_container_picker(action_type, prompt_key, empty_key, comando="",
						bot_container_name=None, filter_standalone_status=None,
						filter_projects_with_all_status=None, multi_action=None):
	"""
	Sends the level-1 picker for an action and remembers what it offered.

	Every reachable host is consulted. The host level only appears when more
	than one of them has something to offer: a host button leading to a single
	container is a tap that disambiguates nothing, and with one host the menu
	has to look exactly as it always did.

	Returns the sent message, or None when nothing was offered.
	"""
	sections = []
	for entry, owner, containers in hosts_with_containers(comando):
		if _picker_has_anything(containers, bot_container_name, filter_standalone_status,
								filter_projects_with_all_status, owner):
			sections.append((entry, owner, containers))

	if not sections:
		send_message(message=get_text(empty_key))
		return None

	if len(sections) == 1:
		entry, _, containers = sections[0]
		return _send_picker_for_host(
			entry, containers, action_type, prompt_key, bot_container_name,
			filter_standalone_status, filter_projects_with_all_status, multi_action,
			name_host=not host_registry.is_single_host())

	# More than one host has something: offer the hosts first.
	markup = InlineKeyboardMarkup(row_width=1)
	for entry, _, containers in sections:
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}',
			callback_data=f'pickHost|{action_type}|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	# No session yet: which host the menu is showing is only decided once one
	# is picked, and that is where the session is opened.
	return send_message(message=host_question(action_type), reply_markup=markup)


def _send_picker_for_host(entry, containers, action_type, prompt_key, bot_container_name,
						filter_standalone_status, filter_projects_with_all_status,
						multi_action, name_host=False):
	"""Sends one host's level-1 keyboard, naming the host when it is not the only one."""
	markup, standalone = build_hierarchical_keyboard(
		containers, action_type, bot_container_name,
		filter_standalone_status=filter_standalone_status,
		filter_projects_with_all_status=filter_projects_with_all_status,
		host_id=entry["id"])

	prompt = get_text(prompt_key)
	if name_host:
		prompt = f'{get_text("list_host_header", host_alias(entry["id"]))}\n{prompt}'

	sent = send_message(message=prompt, reply_markup=markup)
	if sent and standalone:
		save_container_cache(sent.chat.id, sent.message_id, standalone, entry["id"])
	if sent and multi_action and store.get("bot.multi_selection"):
		save_multi_action(sent.chat.id, sent.message_id, multi_action, host_id=entry["id"])
	return sent


def render_picker_for_host(chat_id, message_id, action_type, host_id):
	"""
	Repaints a host picker as that host's container list.

	The parameters of the original action are looked up rather than carried in
	the callback: they are fixed per action, and putting them in callback_data
	would spend bytes on something already known.
	"""
	spec = PICKER_ACTIONS.get(action_type)
	if spec is None:
		warning(f"Unknown picker action: {action_type}")
		return
	try:
		owner = manager(host_id)
		containers = owner.list_containers(comando=spec.get("comando", ""))
	except host_registry.HostUnavailable as e:
		edit_message_text(get_text("host_unreachable", host_alias(host_id), html.escape(str(e.reason))),
						chat_id, message_id)
		return
	except Exception as e:
		# A daemon can build a client and then fail the very next call. Left
		# uncaught this surfaced as a generic "error processing request",
		# which says nothing about the machine being unreachable.
		host_registry.drop(host_id)
		forget_managers()
		edit_message_text(get_text("host_unreachable", host_alias(host_id), html.escape(str(e))),
						chat_id, message_id)
		return

	markup, standalone = build_hierarchical_keyboard(
		containers, action_type, spec.get("bot_container_name"),
		filter_standalone_status=spec.get("filter_standalone_status"),
		filter_projects_with_all_status=spec.get("filter_projects_with_all_status"),
		host_id=host_id)

	if standalone:
		save_container_cache(chat_id, message_id, standalone, host_id)
	# The session opens here, now that a host has been chosen, so every repaint
	# after a press rebuilds from that machine and not from the local one.
	if spec.get("multi_action") and store.get("bot.multi_selection"):
		save_multi_action(chat_id, message_id, spec["multi_action"], host_id=host_id)
	prompt = f'{get_text("list_host_header", host_alias(host_id))}\n{get_text(spec["prompt_key"])}'
	edit_message_text(prompt, chat_id, message_id, reply_markup=markup)


# Status filters applied inside a Compose project (Level 2). They mirror the
# Level 1 filters so the count on the project button and the list you get after
# entering it always agree: /run only offers stopped services, /stop only
# running ones. Actions missing here show every service in the project.
PROJECT_LEVEL2_STATUS_FILTERS = {
	'run': ['exited', 'stopped', 'paused', 'created'],
	'stop': ['running', 'restarting'],
	'exec': ['running', 'restarting'],
}

def build_project_level2_menu(action_type, project_name, chatId, messageId, marked_names=None, host_id=None):
	"""
	Builds the Level 2 menu for a Compose project and refreshes the container
	name cache attached to the message.

	Args:
		action_type: Type of action ('restart', 'run', 'stop', 'delete', 'exec', 'logs', 'logfile', 'checkupdate', 'info', 'changetag', 'compose')
		project_name: Name of the project
		chatId: Chat ID for saving cache
		messageId: Message ID for saving cache
		marked_names: Optional set of container names already acted upon

	Returns:
		tuple: (markup, text) or None if the project does not exist
	"""
	host_id = host_id or host_registry.local_host_id()
	try:
		project_info = manager(host_id).get_project_info(project_name)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach {host_id} for project {project_name}: {e.reason}")
		return None

	if not project_info:
		return None

	# Configuration map for message keys
	message_config = {
		'delete': 'select_container_or_project_delete',
		'restart': 'select_container_or_project',
		'run': 'select_container_or_project',
		'stop': 'select_container_or_project',
	}

	# Default message key
	message_key = message_config.get(action_type.lower(), 'select_container_from_project')

	# Build Level 2 keyboard using generic function
	markup = build_compose_project_level2_keyboard(
		project_info,
		project_name,
		action_type.lower(),
		f'backTo{action_type}Level1',  # Use action_type as-is (already has correct capitalization)
		filter_status=PROJECT_LEVEL2_STATUS_FILTERS.get(action_type.lower()),
		marked_names=marked_names,
		host_id=host_id
	)

	# Save container cache
	save_container_cache(chatId, messageId, project_info.containers, host_id)

	return markup, get_text(message_key, project_name)

def handle_enter_project_level2(action_type, project_name, chatId, messageId, marked_names=None, host_id=None):
	"""
	Generic function to handle "enter...Project" callbacks (Level 2).

	Args:
		action_type: Type of action ('restart', 'run', 'stop', 'delete', 'exec', 'logs', 'logfile', 'checkupdate', 'info', 'changetag', 'compose')
		project_name: Name of the project
		chatId: Chat ID for saving cache
		messageId: Message ID for saving cache
		marked_names: Optional set of container names already acted upon

	Returns:
		None (sends message directly)
	"""
	menu = build_project_level2_menu(action_type, project_name, chatId, messageId, marked_names, host_id)

	if not menu:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	markup, text = menu
	edit_message_text(text, chatId, messageId, reply_markup=markup)

def get_project_container_names(project_name, host_id=None):
	"""
	Names of every container in a Compose project, empty when it is unknown or
	its host cannot be reached.

	Broad except on purpose: a host can build its client and fail on the very
	next call, and get_project_info does not absorb that. Raising here would
	take down whatever was iterating over projects.
	"""
	try:
		project_info = manager(host_id or host_registry.local_host_id()).get_project_info(project_name)
	except Exception as e:
		debug(f"Could not read project {project_name} on {host_id}: {e}")
		return []
	return [c.name for c in project_info.containers] if project_info else []

def enter_project_multi_aware(action_type, project_name, chatId, messageId, host_id=None):
	"""
	Enters a Compose project, pointing any multi-action session at it so later
	repaints land on the project list instead of the top level.
	"""
	session = load_multi_action(chatId, messageId)
	done = session["done"] if session else None
	host_id = host_id or (session["host"] if session else None)
	if session:
		save_multi_action(chatId, messageId, session["action"], 2, project_name, done, host_id)
	handle_enter_project_level2(action_type, project_name, chatId, messageId,
								marked_names=done, host_id=host_id)

def back_to_level1_multi_aware(action_type, chatId, messageId, host_id=None):
	"""Returns to the top-level list, keeping any multi-action session in sync."""
	session = load_multi_action(chatId, messageId)
	done = session["done"] if session else None
	host_id = host_id or (session["host"] if session else None)
	if session:
		save_multi_action(chatId, messageId, session["action"], 1, None, done, host_id)
	result = build_back_to_level1_keyboard(action_type, chatId, messageId, marked_names=done, host_id=host_id)
	if result:
		markup, message_key = result
		edit_message_text(get_text(message_key), chatId, messageId, reply_markup=markup)

def close_multi_action_menu(chatId, messageId):
	"""Closes a multi-action menu and drops the state attached to its message."""
	delete_message(messageId, chat_id=chatId)
	clear_multi_action(chatId, messageId)
	clear_container_cache(chatId, messageId)

def refresh_multi_action_menu(chatId, messageId, container_names=None, succeeded=True):
	"""
	Repaints a /run, /stop or /restart menu after one of its buttons was pressed,
	so the user can keep acting on more containers without reopening it.

	The keyboard is rebuilt from the live Docker state, so this must run *after*
	the Docker action has finished. Containers already acted upon are marked with
	a check; for /run and /stop the status filters drop them from the list
	altogether. A project list that runs out of services steps back to the top
	level, and a top level with nothing left closes itself.

	Args:
		chatId: Chat ID
		messageId: ID of the menu message
		container_names: Names of the containers just acted upon, or None to
			only repaint
		succeeded: False when the action failed, so nothing is marked as done
	"""
	# Serialised: two presses landing at once must not build their keyboards from
	# interleaved state and repaint out of order
	with _menu_refresh_lock:
		session = load_multi_action(chatId, messageId)
		if not session:
			return

		action = session["action"]
		done = session["done"]
		host_id = session["host"]

		if container_names and succeeded:
			done.update(container_names)
			save_multi_action(chatId, messageId, action, session["level"], session["project"], done, host_id)

		# Level 2: repaint the project, or fall through to level 1 when it is empty
		if session["level"] == 2 and session["project"]:
			menu = build_project_level2_menu(action, session["project"], chatId, messageId,
											marked_names=done, host_id=host_id)
			if menu and count_actionable_buttons(menu[0]):
				markup, text = menu
				edit_message_text(text, chatId, messageId, reply_markup=markup)
				return
			# Nothing left here (or the project is gone): go back to the top level
			save_multi_action(chatId, messageId, action, 1, None, done, host_id)

		# Level 1: repaint, or close the menu when there is nothing left to do
		result = build_back_to_level1_keyboard(action, chatId, messageId, marked_names=done, host_id=host_id)
		if not result or not count_actionable_buttons(result[0]):
			close_multi_action_menu(chatId, messageId)
			return

		markup, message_key = result
		edit_message_text(get_text(message_key), chatId, messageId, reply_markup=markup)

def build_back_to_level1_keyboard(action_type, chatId, messageId, bot_container_name=CONTAINER_NAME, marked_names=None, host_id=None):
	"""
	Generic function to build "backTo...Level1" keyboards.

	Args:
		action_type: Type of action ('Restart', 'Run', 'Stop', 'Delete', 'Exec', 'Logs', 'Logfile', 'Compose', 'CheckUpdate', 'Info', 'ChangeTag')
		chatId: Chat ID for saving cache
		messageId: Message ID for saving cache
		bot_container_name: Name of bot container to exclude (None to include all)
		marked_names: Optional set of container names already acted upon

	Returns:
		tuple: (markup, message_key) or None if no containers
	"""
	# Configuration map for each action type
	action_config = {
		'Restart': {
			'no_containers_key': 'no_containers_to_restart',
			'message_key': 'restart_a_container',
			'exclude_bot': True,
			'check_only_bot': True,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'Run': {
			'no_containers_key': 'no_containers_to_start',
			'message_key': 'start_a_container',
			'exclude_bot': True,
			'check_only_bot': True,
			'filter_standalone_status': ['exited', 'stopped', 'paused', 'created'],
			'filter_projects_with_all_status': ['running', 'restarting']
		},
		'Stop': {
			'no_containers_key': 'no_containers_to_stop',
			'message_key': 'stop_a_container',
			'exclude_bot': True,
			'check_only_bot': True,
			'filter_standalone_status': ['running', 'restarting'],
			'filter_projects_with_all_status': ['exited', 'stopped', 'paused', 'created']
		},
		'Delete': {
			'no_containers_key': 'no_containers_to_delete',
			'message_key': 'delete_container',
			'exclude_bot': True,
			'check_only_bot': True,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'Exec': {
			'no_containers_key': 'no_containers_for_exec',
			'message_key': 'exec_command_container',
			'exclude_bot': True,
			'check_only_bot': True,
			'filter_standalone_status': ['running', 'restarting'],
			'filter_projects_with_all_status': ['exited', 'paused', 'dead', 'created']
		},
		'Logs': {
			'no_containers_key': 'no_containers_for_logs',
			'message_key': 'logs_command_container',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'Logfile': {
			'no_containers_key': 'no_containers_for_logs',
			'message_key': 'show_logsfile',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'Compose': {
			'no_containers_key': 'error_no_containers_available',
			'message_key': 'show_compose',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'CheckUpdate': {
			'no_containers_key': 'no_containers_for_checkupdate',
			'message_key': 'checkupdate_command_container',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'Info': {
			'no_containers_key': 'no_containers_for_info',
			'message_key': 'info_command_container',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		},
		'ChangeTag': {
			'no_containers_key': 'error_no_containers_available',
			'message_key': 'change_tag_container',
			'exclude_bot': False,
			'check_only_bot': False,
			'filter_standalone_status': None,
			'filter_projects_with_all_status': None
		}
	}

	config = action_config.get(action_type, {})
	if not config:
		return None

	host_id = host_id or host_registry.local_host_id()
	try:
		containers = manager(host_id).list_containers()
	except host_registry.HostUnavailable as e:
		debug(f"Cannot rebuild the menu for {host_id}: {e.reason}")
		return None

	# Check if no containers or only bot container
	if not containers:
		send_message(message=get_text(config['no_containers_key']))
		return None

	if config['check_only_bot'] and all(c.name == bot_container_name for c in containers):
		send_message(message=get_text(config['no_containers_key']))
		return None

	# Build hierarchical keyboard
	exclude_container = bot_container_name if config['exclude_bot'] else None
	markup, standalone_containers = build_hierarchical_keyboard(
		containers,
		action_type,
		exclude_container,
		filter_standalone_status=config['filter_standalone_status'],
		filter_projects_with_all_status=config['filter_projects_with_all_status'],
		marked_names=marked_names,
		host_id=host_id
	)

	# Save container cache for standalone containers
	if standalone_containers:
		save_container_cache(chatId, messageId, standalone_containers, host_id)

	return markup, config['message_key']

def build_compose_project_level2_keyboard(project_info, project_name, action_type, back_callback, filter_status=None, marked_names=None, host_id=None):
	"""
	Generic function to build Level 2 keyboard for Compose projects.

	Args:
		project_info: Project information object
		project_name: Name of the project
		action_type: Type of action ('restart', 'run', 'stop', 'delete', 'exec', 'logs', 'logfile', 'info', 'changetag', 'checkupdate', 'ports')
		back_callback: Callback data for the back button
		filter_status: Optional list of statuses to keep (see PROJECT_LEVEL2_STATUS_FILTERS)
		marked_names: Optional set of container names already acted upon in a
			multi-action session; they show a check instead of their status emoji

	Returns:
		InlineKeyboardMarkup: Configured keyboard
	"""
	markup = InlineKeyboardMarkup(row_width=button_columns())
	botones = []

	# Action configuration map
	action_config = {
		'restart': {'icon': '🔄', 'button_key': 'button_restart_project', 'whole_callback': 'restartWholeProject', 'use_emoji': False},
		'run': {'icon': '▶️', 'button_key': 'button_run_project', 'whole_callback': 'runWholeProject', 'use_emoji': False},
		'stop': {'icon': '⏹️', 'button_key': 'button_stop_project', 'whole_callback': 'stopWholeProject', 'use_emoji': False},
		'delete': {'icon': '🗑️', 'button_key': 'button_delete_project', 'whole_callback': 'confirmDeleteWholeProject', 'use_emoji': True, 'callback_prefix': 'confirmDelete'},
		'exec': {'icon': '⚙️', 'button_key': None, 'whole_callback': None, 'use_emoji': True, 'callback_prefix': 'askCommand'},
		'logs': {'icon': '📄', 'button_key': None, 'whole_callback': None, 'use_emoji': True},
		'logfile': {'icon': '📁', 'button_key': None, 'whole_callback': None, 'use_emoji': True},
		'info': {'icon': 'ℹ️', 'button_key': None, 'whole_callback': None, 'use_emoji': True},
		'changetag': {'icon': '🏷️', 'button_key': None, 'whole_callback': None, 'use_emoji': True, 'callback_prefix': 'changeTagContainer'},
		'checkupdate': {'icon': '🔄', 'button_key': None, 'whole_callback': None, 'use_emoji': True, 'callback_prefix': 'checkUpdate'},
		'ports': {'icon': '🔌', 'button_key': None, 'whole_callback': None, 'use_emoji': True}
	}

	host_id = host_id or host_registry.local_host_id()
	config = action_config.get(action_type.lower(), {})

	# Add individual container buttons (sorted by status and service name)
	for service_name in sort_project_services(project_info):
		container = project_info.services[service_name]

		# Filter by status if requested
		if filter_status and container.status not in filter_status:
			continue

		# Determine status indicator
		if marked_names and container.name in marked_names:
			# Already acted upon in a multi-action session
			status_indicator = ICON_CONTAINER_ACTION_DONE
		elif action_type.lower() == 'checkupdate':
			# Special case: use update emoji for checkupdate
			status_indicator = get_update_emoji(container.name, host_id)
		elif config.get('use_emoji', False):
			status_indicator = get_status_emoji(container.status, container.name, container)
		else:
			status_indicator = "🟢" if container.status in ['running', 'restarting'] else "🔴"

		# Determine callback action based on action_type and container status
		if config.get('callback_prefix'):
			# Use custom callback prefix (e.g., 'confirmDelete' instead of 'delete', 'checkUpdate' instead of 'checkupdate', 'changeTag' instead of 'changetag')
			callback_action = config['callback_prefix']
		elif action_type.lower() in ['delete', 'info', 'logs', 'logfile', 'ports']:
			# These actions work regardless of status
			callback_action = action_type.lower()
		elif container.status in ['running', 'restarting']:
			# For running containers, use the action type (restart/stop/exec)
			callback_action = action_type.lower()
		else:
			# For stopped containers, always use "run"
			callback_action = "run"

		botones.append(
			InlineKeyboardButton(
				f"{status_indicator} {container.name}",
				callback_data=f"{callback_action}|{container_ref(host_id, container)}"
			)
		)

	# Add container buttons to markup
	markup.add(*botones)

	# Add bottom row with action button and back button (if configured)
	bottom_buttons = []

	# Special case for 'info': add "View project info" button
	if action_type.lower() == 'info':
		bottom_buttons.append(
			InlineKeyboardButton(
				get_text("button_view_project_info"),
				callback_data=f"showProjectInfo|{register_project_hash(project_name, host_id)}"
			)
		)
	elif config.get('whole_callback'):
		bottom_buttons.append(
			InlineKeyboardButton(
				f"{config['icon']} {get_text(config['button_key'])}",
				callback_data=f"{config['whole_callback']}|{register_project_hash(project_name, host_id)}"
			)
		)

	bottom_buttons.append(
		InlineKeyboardButton(
			get_text('button_back'),
			callback_data=back_callback
		)
	)

	markup.add(*bottom_buttons)

	return markup

def is_admin(userId):
	return str(userId) in str(TELEGRAM_ADMIN).split(',')

def update_available(container, host_id=None):
	"""
	Whether a container has a pending update, according to the cache.

	`host_id` is which host it lives on. It defaults to the local one so the
	single-host callers read as they always did, but anything iterating over
	hosts has to pass it or it would read another machine's cache entry.
	"""
	image_with_tag = container.attrs['Config']['Image']
	update = False
	if store.get("bot.check_updates"):
		try:
			if read_container_update_status(image_with_tag, container.name, host_id) is True:
				update = True
		except:
			pass
	return update

def display_containers(containers, host_id=None):
	"""
	Renders a list of containers, grouped into Compose projects and standalone.

	`host_id` is which host they came from, needed to read their update state
	from the right machine's cache.
	"""
	# Calculate statistics
	total_containers = len(containers)
	running_containers = sum(1 for c in containers if c.status in ['running', 'restarting'])
	stopped_containers = sum(1 for c in containers if c.status in ['exited', 'dead'])

	# Cache update status and project info to avoid repeated calls
	update_cache = {}  # {container.id: bool}
	container_info_cache = {}  # {container.id: (project_name, service_name)}

	# Separate containers into projects and standalone
	project_containers = {}  # {project_name: [containers]}
	standalone_containers = []
	pending_updates = 0

	for container in containers:
		# Read labels directly for better performance
		labels = container.labels or {}
		project_name = labels.get('com.docker.compose.project')
		service_name = labels.get('com.docker.compose.service')

		# Cache the info
		container_info_cache[container.id] = (project_name, service_name)

		# Cache update status
		has_update = update_available(container, host_id)
		update_cache[container.id] = has_update
		if has_update:
			pending_updates += 1

		if project_name:
			if project_name not in project_containers:
				project_containers[project_name] = []
			project_containers[project_name].append(container)
		else:
			standalone_containers.append(container)

	# Apply rule: projects with only 1 container are shown as standalone
	single_container_projects = []
	for project_name, project_conts in list(project_containers.items()):
		if len(project_conts) == 1:
			standalone_containers.extend(project_conts)
			single_container_projects.append(project_name)

	# Remove single-container projects from project_containers
	for project_name in single_container_projects:
		del project_containers[project_name]

	# Build summary with project count
	project_count = len(project_containers)
	result = f"📊 <b>{get_text('containers')}:</b> {total_containers}\n"
	if project_count > 0:
		result += f"📦 <b>{get_text('status_projects')}:</b> {project_count}\n"
	result += f"🟢 {get_text('status_running')}: {running_containers}\n"
	result += f"🔴 {get_text('status_stopped')}: {stopped_containers}\n"
	result += f"⬆️ {get_text('status_updates')}: {pending_updates}\n\n"

	# Build container list
	result += "<pre>"

	# Separate bot container from other standalone containers
	bot_container = None
	other_standalone = []
	if standalone_containers:
		for container in standalone_containers:
			if container.name == CONTAINER_NAME:
				bot_container = container
			else:
				other_standalone.append(container)

	# Show bot container first if present
	if bot_container:
		result += f"🐳 {get_status_emoji(bot_container.status, bot_container.name, bot_container)} {bot_container.name}"
		if update_cache[bot_container.id]:
			result += " ⬆️"
		result += "\n"
		# Add empty line after bot if there are projects or other containers
		if project_containers or other_standalone:
			result += "\n"

	# Show multi-container projects after bot
	for project_name in sorted(project_containers.keys()):
		project_conts = project_containers[project_name]
		container_count = len(project_conts)
		result += f"📦 {project_name} ({get_text('compose_project_containers', container_count)})\n"

		# Sort containers within project: running first, then stopped (all alphabetically)
		sorted_project_conts = sort_containers_by_priority(project_conts)
		for container in sorted_project_conts:
			# Use cached info, fall back to container name when the compose
			# service label is missing (e.g. Nextcloud AIO spawns siblings
			# tagged only with the project label, no service label).
			_, service_name = container_info_cache[container.id]
			display_name = service_name or container.name
			result += f"  {get_status_emoji(container.status, container.name, container)} {display_name}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"
		result += "\n"  # Empty line between projects

	# Show other standalone containers last (running first, then stopped - all alphabetically)
	if other_standalone:
		sorted_standalone = sort_containers_by_priority(other_standalone)
		for container in sorted_standalone:
			result += f"🐳 {get_status_emoji(container.status, container.name, container)} {container.name}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"

	result += "</pre>"
	return result

def sort_containers_by_priority(containers):
	"""
	Sort containers with consistent priority:
	1. Bot container (CONTAINER_NAME) first
	2. Running/restarting containers (alphabetically)
	3. Stopped/paused/exited containers (alphabetically)

	Args:
		containers: List of container objects

	Returns:
		List of sorted containers
	"""
	def sort_key(container):
		# Priority 1: Bot container first
		is_bot = 0 if container.name == CONTAINER_NAME else 1

		# Priority 2: Running containers before stopped
		is_running = 0 if container.status in ['running', 'restarting'] else 1

		# Priority 3: Alphabetical by name
		name_lower = container.name.lower()

		return (is_bot, is_running, name_lower)

	return sorted(containers, key=sort_key)

def sort_project_services(project_info):
	"""
	Sort project services with consistent priority:
	1. Running/restarting containers (alphabetically by service name)
	2. Stopped/paused/exited containers (alphabetically by service name)

	Args:
		project_info: ComposeProjectInfo object

	Returns:
		List of service names sorted by priority
	"""
	def sort_key(service_name):
		container = project_info.services[service_name]
		# Priority 1: Running containers before stopped
		is_running = 0 if container.status in ['running', 'restarting'] else 1
		# Priority 2: Alphabetical by service name
		name_lower = service_name.lower()
		return (is_running, name_lower)

	return sorted(project_info.get_service_names(), key=sort_key)

def get_container_health_status(container):
	"""Get the health status of a container. Returns 'healthy', 'unhealthy', 'starting', or None"""
	try:
		state = container.attrs.get('State', {})
		health = state.get('Health', {})
		if health:
			return health.get('Status')  # 'healthy', 'unhealthy', 'starting'
	except:
		pass
	return None

def get_health_status_text(container):
	"""Get formatted health status text with emoji for display"""
	health = get_container_health_status(container)
	if health == "healthy":
		return f"💚 {get_text('health_healthy')}"
	elif health == "unhealthy":
		return f"🟢 (💔) {get_text('health_unhealthy')}"
	elif health == "starting":
		return f"🟡 {get_text('health_starting')}"
	return None

def get_status_emoji(statusStr, containerName, container=None):
	status = "🟢"
	if statusStr == "exited" or statusStr == "dead":
		status = "🔴"
	elif statusStr == "restarting" or statusStr == "removing":
		status = "🟡"
	elif statusStr == "paused":
		status = "🟠"
	elif statusStr == "created":
		status = "🔵"
	elif statusStr == "running" and container:
		# Check health status if container is running
		health = get_container_health_status(container)
		if health == "healthy":
			status = "💚"  # Healthy running container
		elif health == "unhealthy":
			status = "🟢 (💔)"  # Unhealthy running container
		elif health == "starting":
			status = "🟡"  # Health check in progress

	if CONTAINER_NAME == containerName:
		status = "👑"
	return status

def get_update_emoji(containerName, host_id=None):
	"""
	Whether a container has a pending update, as an emoji.

	`host_id` says which machine to ask. It defaults to the local one, but a
	project list on a remote host has to pass it or it would read the local
	cache for a container that is not there.
	"""
	status = "✅"
	host_id = host_id or host_registry.local_host_id()

	container_id = find_container_id_on_host(host_id, containerName)
	if not container_id:
		return status

	try:
		container = manager(host_id).client.containers.get(container_id)
		image_with_tag = container.attrs['Config']['Image']
		if read_container_update_status(image_with_tag, container.name, host_id) is True:
			status = "⬆️"
	except Exception as e:
		error(f"Could not check update: [{e}]")

	return status

def get_random_available_port():
	"""
	Generate a random available port (10000-60000) that is truly available on the system
	Checks both Docker containers and system-level port availability
	Returns the port number or None if no available port found
	"""
	return port_manager.get_random_available_port()

def show_container_ports(host_id=None):
	"""
	Show all ports used by containers on one host.

	Per host rather than merged: a port is only in use or free on a given
	machine, and a single list would suggest a conflict where there is none.
	"""
	host_id = host_id or host_registry.local_host_id()
	try:
		containers = manager(host_id).list_containers()
	except host_registry.HostUnavailable as e:
		send_message(message=get_text("host_unreachable", host_alias(host_id), html.escape(str(e.reason))))
		return

	# Sort containers: bot first, then running, then stopped (all alphabetically)
	sorted_containers = sort_containers_by_priority(containers)

	container_blocks = []

	for container in sorted_containers:
		try:
			ports, is_host_network = port_manager.get_container_ports(container)
			block = []

			if is_host_network:
				# Host network shares the host's network namespace: we don't
				# list ports here, just indicate the container uses host mode.
				if container.status in ['running', 'restarting']:
					emoji = "🟢"
					block.append(f"{emoji} {container.name} (host)")
			elif ports:
				status = container.status
				emoji = "🟢" if status == "running" else "🔴"
				# Remove duplicates and sort
				unique_ports = sorted(set(ports), key=lambda x: (int(x.split('/')[0]), x.split('/')[1]))
				block.append(f"{emoji} {container.name}:")
				# Add each port on a separate line with indentation
				for port in unique_ports:
					block.append(f"  - {port}")

			if block:
				container_blocks.append(block)
		except Exception as e:
			debug(f"Error getting ports from container {container.name}: {e}")
			continue

	# Add inline keyboard with Generate, Check and Close buttons
	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(
		InlineKeyboardButton(get_text("ports_button_generate"), callback_data="generatePort"),
		InlineKeyboardButton(get_text("ports_button_check"), callback_data="checkPort")
	)
	markup.add(
		InlineKeyboardButton(get_text("button_close"), callback_data="cerrar")
	)

	if not container_blocks:
		send_message(message=get_text("ports_no_containers"), reply_markup=markup)
		return

	# Pack blocks into chunks below Telegram's message size limit
	max_length = 3500
	header = get_text("ports_list_header") + "\n\n"
	pre_open, pre_close = "<pre>", "</pre>"

	def overhead(is_first_chunk):
		return len(pre_open) + len(pre_close) + (len(header) if is_first_chunk else 0)

	chunks = []
	current_lines = []
	current_len = 0

	for block in container_blocks:
		block_len = sum(len(line) + 1 for line in block)
		is_first = (len(chunks) == 0)

		# If adding this block would overflow the current chunk, flush it first
		if current_lines and current_len + block_len + overhead(is_first) > max_length:
			chunks.append(current_lines)
			current_lines = []
			current_len = 0
			is_first = False

		# If a single block is larger than the limit, split it line by line
		if not current_lines and block_len + overhead(is_first) > max_length:
			for line in block:
				line_len = len(line) + 1
				if current_lines and current_len + line_len + overhead(is_first) > max_length:
					chunks.append(current_lines)
					current_lines = []
					current_len = 0
					is_first = False
				current_lines.append(line)
				current_len += line_len
		else:
			current_lines.extend(block)
			current_len += block_len

	if current_lines:
		chunks.append(current_lines)

	# Send each chunk: header only on the first, keyboard only on the last
	for i, chunk_lines in enumerate(chunks):
		prefix = header if i == 0 else ""
		message = f"{prefix}{pre_open}" + "\n".join(chunk_lines) + pre_close
		is_last = (i == len(chunks) - 1)
		send_message(message=message, reply_markup=markup if is_last else None)

def ask_port_to_check(userId):
	"""Ask user for a port number to check"""
	debug(f"Running command: ask_port_to_check for user {userId}")
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelCheckPort"))
	x = send_message(message=get_text("ports_ask_port"), reply_markup=markup)
	if x:
		save_port_check_request_state(userId, x.message_id)

def send_ports_menu():
	"""
	Opens /ports, asking which host first when there is more than one.

	A port being taken is a property of one machine, so the answer only means
	anything once a host is chosen.
	"""
	if host_registry.is_single_host():
		show_container_ports(host_registry.local_host_id())
		return

	markup = InlineKeyboardMarkup(row_width=1)
	for entry in host_registry.hosts():
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}',
			callback_data=f'portsHost|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	send_message(message=host_question("ports"), reply_markup=markup)


def check_specific_port(port_number):
	"""
	Check if a specific port is available
	Returns tuple (is_available, message)
	"""
	is_available, message_key, container_name = port_manager.check_port_availability(port_number)

	if container_name:
		return is_available, get_text(message_key, port_number, container_name)
	else:
		return is_available, get_text(message_key, port_number)

def print_donors():
	donors = get_array_donors_online()
	if donors:
		result = ""
		for donor in donors:
			result += f"· {donor}\n"
		send_message(message=get_text("donors_list", result))
	else:
		send_message(message=get_text("error_getting_donors"))

def get_array_donors_online():
	headers = {
		'Cache-Control': 'no-cache',
		'Pragma': 'no-cache'
	}

	response = requests.get(DONORS_URL, headers=headers)
	if response.status_code == 200:
		try:
			data = response.json()
			if isinstance(data, list):
				data.sort()
				return data
			else:
				error(f"Error getting donors: data is not a list [{str(data)}]")
				return []
		except ValueError:
			error(f"Error getting donors: data is not a json [{response.text}]")
			return []
	else:
		error(f"Error getting donors: error code [{response.status_code}]")
		return []

# --- REFERENCIAS DE CONTENEDOR -------------------------------------------
#
# A container is identified by which host it is on plus its short id, because
# five hex characters are only unique within one daemon. The two travel
# together through callback_data and the message caches as
#
#     <hostId>:<shortId>        e.g. h_5f55:9a3b1
#
# so that a button carries everything needed to act on the right machine. That
# is what keeps every operation from having to grow a host argument of its
# own: whatever holds a reference can find its way back to the host.
CONTAINER_REF_SEPARATOR = ":"


def make_ref(host_id, container_id):
	"""Builds the reference for a container on a host."""
	return f"{host_id}{CONTAINER_REF_SEPARATOR}{container_id[:CONTAINER_ID_LENGTH]}"


def container_ref(host_id, container):
	"""The reference for a container object on a host."""
	return make_ref(host_id, container.id)


def parse_ref(ref):
	"""
	Splits a reference into (host_id, short_id).

	A bare short id means the local host. That is what a button sent before the
	upgrade carries, and pressing one of those has to keep working rather than
	failing with something inscrutable.
	"""
	text = str(ref or "")
	if CONTAINER_REF_SEPARATOR in text:
		host_id, _, short_id = text.partition(CONTAINER_REF_SEPARATOR)
		return host_id, short_id
	return host_registry.local_host_id(), text


def ref_host(ref):
	"""The host a reference points at."""
	return parse_ref(ref)[0]


def ref_id(ref):
	"""The short container id a reference points at."""
	return parse_ref(ref)[1]


def manager_for(ref):
	"""
	The manager for the host a reference points at.

	Raises host_registry.HostUnavailable when that host cannot be reached, so
	a caller acting on one container gets the same treatment as one sweeping
	all of them.
	"""
	return manager(ref_host(ref))


def find_container(ref):
	"""
	(manager, container) for a reference, or (None, None) when it cannot be
	found — an unreachable host, or a container that is gone.
	"""
	try:
		owner = manager_for(ref)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach the host for {ref}: {e.reason}")
		return None, None
	try:
		return owner, owner.client.containers.get(ref_id(ref))
	except Exception as e:
		debug(f"Container {ref} not found: {e}")
		return None, None


def display_all_hosts(comando=""):
	"""
	The container listing for every host, one section each.

	Rendering per host and stitching the sections together, rather than
	threading a host through the renderer, keeps the single-host output
	byte-identical to what it has always been.
	"""
	if host_registry.is_single_host():
		host_id = host_registry.local_host_id()
		try:
			containers = manager(host_id).list_containers(comando=comando)
		except host_registry.HostUnavailable as e:
			return get_text("list_host_unreachable", host_alias(host_id), html.escape(str(e.reason)))
		return display_containers(containers, host_id)

	sections = hosts_with_containers(comando)
	rendered = []
	for entry, _, containers in sections:
		body = display_containers(containers, entry["id"]) if containers else get_text("list_host_empty")
		rendered.append(f'{get_text("list_host_header", host_alias(entry["id"]))}\n{body}')

	for entry in unreachable_hosts(sections):
		rendered.append(get_text("list_host_unreachable", host_alias(entry["id"]), ""))

	return "\n\n".join(rendered) if rendered else get_text("error_no_containers_available")


def hosts_with_containers(comando=""):
	"""
	Every reachable host and its containers, in the order the hosts are
	configured.

	Returns [(host_entry, manager, containers)]. Hosts that do not answer are
	left out: one machine being down has to degrade what it can and nothing
	else.
	"""
	sections = []
	for entry in host_registry.hosts():
		try:
			owner = manager(entry["id"])
		except host_registry.HostUnavailable as e:
			debug(f"Skipping host {entry.get('alias', entry['id'])}: {e.reason}")
			continue
		try:
			sections.append((entry, owner, owner.list_containers(comando=comando)))
		except Exception as e:
			warning(f"Could not list containers on {entry.get('alias', entry['id'])}: {e}")
			host_registry.drop(entry["id"])
			forget_managers()
	return sections


def unreachable_hosts(sections):
	"""
	The configured hosts missing from `sections`, so the interface can say so
	instead of quietly showing less than the user has.
	"""
	present = {entry["id"] for entry, _, _ in sections}
	return [entry for entry in host_registry.hosts() if entry["id"] not in present]


def get_container_id_by_name(container_name, debugging=False):
	"""
	The short id of a container by name, on the local host.

	Deliberately local-only, and only used for the bot's own container and its
	updater, which exist on exactly one machine. Anything that could be on
	another host goes through find_container_id_on_host or
	resolve_container_argument instead.
	"""
	if debugging:
		debug(f"Finding container {container_name}")
	containers = docker_manager.list_containers()
	for container in containers:
		if container.name == container_name:
			if debugging:
				debug(f"Container {container_name} found")
			return container.id[:CONTAINER_ID_LENGTH]
	if debugging:
		debug(f"Container {container_name} not found")
	return None

def sanitize_text_for_filename(text):
	sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', text)
	sanitized = re.sub(r'_+', '_', sanitized)
	return sanitized

def _cache_dir():
	directory = os.path.join(store.state_dir(), "cache")
	os.makedirs(directory, exist_ok=True)
	return directory

def _cache_path(key):
	"""
	Location of one entry of the session cache.

	This cache holds short-lived interface state: which containers an open menu
	is offering, what a user is being asked to type, the mapping from the
	hashes in callback_data back to project names. It lives under the storage
	volume rather than in the container filesystem so that buttons on messages
	sent before an update still work afterwards.

	The key becomes a file name, so it is sanitised: every key is built by the
	bot today, but one that ever carried a slash would write outside the cache.
	"""
	return os.path.join(_cache_dir(), f"{sanitize_text_for_filename(str(key))}.json")

# An entry not read for this long is gone: this is interface state for menus
# nobody is looking at any more, and it sits in the storage volume, which is
# the one directory that survives every update.
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Sets do not survive a round trip through JSON, and two of the entries here
# hold one (which containers are selected, which are already done). Tagged on
# the way out and rebuilt on the way in, so callers keep working with sets.
_SET_TAG = "__set__"

def _encode_cache_value(value):
	if isinstance(value, (set, frozenset)):
		return {_SET_TAG: sorted(value, key=str)}
	raise TypeError(f"{type(value).__name__} is not cacheable")

def _decode_cache_value(document):
	if len(document) == 1 and _SET_TAG in document:
		return set(document[_SET_TAG])
	return document

def write_cache_item(key, value):
	"""
	Stores one entry of the session cache, as JSON.

	JSON and not pickle: this directory is in the storage volume the user
	mounts, and unpickling is running whatever is in the file. Nothing in here
	needs more than what JSON carries.
	"""
	with _cache_lock:
		path = _cache_path(key)
		temporary = f"{path}.tmp"
		try:
			with open(temporary, "w", encoding="utf-8") as handle:
				json.dump(value, handle, default=_encode_cache_value, ensure_ascii=False)
			os.replace(temporary, path)
		except Exception as e:
			error(f"Error writing cache item: {key} - {e}")
			try:
				os.remove(temporary)
			except OSError:
				pass

def read_cache_item(key):
	"""
	One entry of the session cache, or None when it is missing or expired.

	Reading pushes the expiry back: an entry is dropped after a week of nobody
	touching it, not a week after it was written, so a menu still in use does
	not stop working underneath someone.
	"""
	with _cache_lock:
		path = _cache_path(key)
		try:
			if time.time() - os.path.getmtime(path) > CACHE_TTL_SECONDS:
				os.remove(path)
				return None
			with open(path, "r", encoding="utf-8") as handle:
				value = json.load(handle, object_hook=_decode_cache_value)
			os.utime(path, None)
			return value
		except (OSError, ValueError):
			return None

def delete_cache_item(key):
	"""Delete cache item with thread-safe lock to prevent corruption."""
	with _cache_lock:
		try:
			os.remove(_cache_path(key))
		except OSError as e:
			if not isinstance(e, FileNotFoundError):
				debug(f"Could not delete cache item {key}: {e}")

def sweep_cache():
	"""
	Drops expired entries, and anything left by a version that wrote pickles.

	Without this the directory only ever grows: every menu, every prompt and
	every update selection leaves a file behind in a volume nobody looks at.
	Returns how many files were removed, which is what the test asserts on.
	"""
	removed = 0
	with _cache_lock:
		try:
			names = os.listdir(_cache_dir())
		except OSError as e:
			debug(f"Could not sweep the cache: {e}")
			return 0
		for name in names:
			path = os.path.join(_cache_dir(), name)
			try:
				# Anything that is not a .json is either a half-written
				# temporary or a pickle from before this format, and neither is
				# ever read again.
				if not name.endswith(".json") or time.time() - os.path.getmtime(path) > CACHE_TTL_SECONDS:
					os.remove(path)
					removed += 1
			except OSError as e:
				debug(f"Could not remove cache file {name}: {e}")
	if removed:
		debug(f"Session cache: removed {removed} stale entries")
	return removed

def _schedule_cache_sweep():
	"""Sweeps the session cache now and once a day after that."""
	sweep_cache()
	timer = threading.Timer(24 * 60 * 60, _schedule_cache_sweep)
	timer.daemon = True
	timer.start()

def save_container_update_status(image_with_tag, container_name, has_update, host_id=None):
	"""
	Records whether `container_name` has a pending update.

	`has_update` is a boolean, or None to forget what was known. 4.x stored the
	translated status message here instead and detected pending updates by
	looking for the current language's wording inside it, which meant changing
	the language silently emptied the cache.
	"""
	if has_update is None:
		store.forget_update_status(host_id or LOCAL_HOST_ID, container_name)
		return
	store.set_update_status(
		host_id or LOCAL_HOST_ID,
		container_name,
		image_with_tag,
		has_update,
		checked_at=datetime.now().isoformat(timespec="seconds"),
	)

def read_container_update_status(image_with_tag, container_name, host_id=None):
	"""
	Whether `container_name` has a pending update: True, False, or None when
	nothing usable is cached.

	None also covers an entry recorded against a different image, since a tag
	change makes what was cached say nothing about what runs now.
	"""
	return store.update_status(host_id or LOCAL_HOST_ID, container_name, image_with_tag)

def update_status_text(has_update):
	"""
	The status line shown for a container, rendered from the cached boolean at
	display time so it always comes out in the language configured now.
	"""
	if has_update is True:
		return get_text("NEED_UPDATE_CONTAINER_TEXT")
	if has_update is False:
		return get_text("UPDATED_CONTAINER_TEXT")
	return ""

def save_update_data(chat_id, message_id, containers, selected=None):
	if selected is None:
		selected = set()
	data = {
		"containers": containers,
		"selected": selected
	}
	write_cache_item(f"update_data_{chat_id}_{message_id}", data)

def load_update_data(chat_id, message_id):
	data = read_cache_item(f"update_data_{chat_id}_{message_id}")
	if data is None or not isinstance(data, dict):
		return [], set()
	containers = data.get("containers", [])
	selected = data.get("selected", set())
	if not isinstance(selected, set):
		selected = set(selected)
	# Reject pre-upgrade format (list of plain names instead of [id, name] pairs)
	if containers and not all(isinstance(e, (list, tuple)) and len(e) >= 2 for e in containers):
		return [], set()
	return containers, selected

def clear_update_data(chat_id, message_id):
	delete_cache_item(f"update_data_{chat_id}_{message_id}")

# Generic cache helpers
def _save_cache(prefix, identifier, value):
	"""Generic save to cache with prefix and identifier"""
	key = f"{prefix}_{identifier}"
	write_cache_item(key, value)

def _load_cache(prefix, identifier):
	"""Generic load from cache with prefix and identifier"""
	key = f"{prefix}_{identifier}"
	return read_cache_item(key)

def _clear_cache(prefix, identifier):
	"""Generic clear from cache with prefix and identifier"""
	key = f"{prefix}_{identifier}"
	delete_cache_item(key)

# Generic keyboard helpers
def create_simple_keyboard(button_text_key, callback_data="cerrar", row_width=1):
	"""Create a simple keyboard with one button"""
	markup = InlineKeyboardMarkup(row_width=row_width)
	markup.add(InlineKeyboardButton(get_text(button_text_key), callback_data=callback_data))
	return markup

def create_confirm_cancel_keyboard(confirm_callback, confirm_text_key="button_confirm", cancel_callback="cerrar", cancel_text_key="button_cancel"):
	"""Create a keyboard with confirm and cancel buttons"""
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text(confirm_text_key), callback_data=confirm_callback))
	markup.add(InlineKeyboardButton(get_text(cancel_text_key), callback_data=cancel_callback))
	return markup

# Command cache functions
def save_command_cache(command):
	command_id = uuid.uuid4().hex[:8]
	_save_cache("exec", command_id, command)
	return command_id

def load_command_cache(command_id):
	return _load_cache("exec", command_id)

def clear_command_cache(command_id):
	_clear_cache("exec", command_id)

# Command request state functions
def save_command_request_state(user_id, containerId, containerName, deleteMessage):
	value = {"containerId": containerId, "containerName": containerName, "deleteMessage": deleteMessage}
	_save_cache("pending_command", user_id, value)

def load_command_request_state(user_id):
	return _load_cache("pending_command", user_id)

def clear_command_request_state(user_id):
	_clear_cache("pending_command", user_id)

# Port check request state functions
def save_port_check_request_state(user_id, deleteMessage):
	value = {"deleteMessage": deleteMessage}
	_save_cache("pending_port_check", user_id, value)

def load_port_check_request_state(user_id):
	return _load_cache("pending_port_check", user_id)

def clear_port_check_request_state(user_id):
	_clear_cache("pending_port_check", user_id)

def save_container_refs(chat_id, message_id, pairs):
	"""
	Remembers a set of (reference, name) pairs for a message.

	Used where the buttons on one message point at containers on different
	hosts, which the per-host variant cannot express.
	"""
	from datetime import datetime
	write_cache_item(f"containers_{chat_id}_{message_id}", {
		"_timestamp": datetime.now().isoformat(),
		"containers": {ref: name for ref, name in pairs},
	})


def save_container_cache(chat_id, message_id, containers, host_id=None):
	"""
	Remembers which container each button on a message refers to, for 7 days.

	Keyed by reference rather than by short id: the same five characters can
	name a different container on another host, so a bare id would let a
	button resolve to the wrong machine's container.
	"""
	host_id = host_id or host_registry.local_host_id()
	from datetime import datetime
	cache_data = {
		"_timestamp": datetime.now().isoformat(),
		"containers": {}
	}
	for container in containers:
		cache_data["containers"][container_ref(host_id, container)] = container.name

	write_cache_item(f"containers_{chat_id}_{message_id}", cache_data)

def load_container_name(chat_id, message_id, container_id):
	"""
	Obtiene el nombre de un contenedor desde la caché

	Args:
		chat_id: ID del chat
		message_id: ID del mensaje
		container_id: ID corto del contenedor

	Returns:
		str: Nombre del contenedor o None si no está en caché o expiró
	"""
	from datetime import datetime, timedelta
	cache_data = read_cache_item(f"containers_{chat_id}_{message_id}")

	if cache_data is None:
		return None

	# Check expiry (7 days)
	if "_timestamp" in cache_data:
		try:
			timestamp = datetime.fromisoformat(cache_data["_timestamp"])
			if datetime.now() - timestamp > timedelta(days=7):
				clear_container_cache(chat_id, message_id)
				return None
		except:
			pass

	names = cache_data.get("containers", {})
	if container_id in names:
		return names[container_id]
	# Entries written before hosts existed are keyed by bare short id, and a
	# button from one of those messages still has to resolve.
	return names.get(ref_id(container_id))

def clear_container_cache(chat_id, message_id):
	"""Clears the container cache for a message"""
	delete_cache_item(f"containers_{chat_id}_{message_id}")

def save_multi_action(chat_id, message_id, action, level=1, project=None, done=None, host_id=None):
	"""
	Stores the multi-action session attached to a /run, /stop or /restart menu.

	While this session exists the menu message is kept alive after every press:
	the action runs, the keyboard is rebuilt from the real Docker state and the
	containers already acted upon are marked with a check.

	Args:
		chat_id: Chat ID
		message_id: ID of the menu message
		action: 'Run', 'Stop' or 'Restart'
		level: 1 for the top-level list, 2 for a Compose project list
		project: Project name when level is 2
		done: Set of container names already acted upon (successfully)
	"""
	from datetime import datetime
	write_cache_item(f"multi_action_{chat_id}_{message_id}", {
		"_timestamp": datetime.now().isoformat(),
		"action": action,
		"level": level,
		"project": project,
		# Which host the menu is showing. Without it the repaint after every
		# press rebuilt from the local host, so acting on a remote container
		# swapped the list for the local machine's.
		"host": host_id or host_registry.local_host_id(),
		"done": set(done) if done else set()
	})

def load_multi_action(chat_id, message_id):
	"""Returns the multi-action session for a message, or None if absent/expired."""
	from datetime import datetime, timedelta
	data = read_cache_item(f"multi_action_{chat_id}_{message_id}")
	if not isinstance(data, dict) or not data.get("action"):
		return None

	# Same 7 day expiry as the container name cache
	timestamp = data.get("_timestamp")
	if timestamp:
		try:
			if datetime.now() - datetime.fromisoformat(timestamp) > timedelta(days=7):
				clear_multi_action(chat_id, message_id)
				return None
		except:
			pass

	if not isinstance(data.get("done"), set):
		data["done"] = set(data.get("done") or [])
	# A session started before hosts existed carries no host, and means the
	# local one.
	data.setdefault("host", host_registry.local_host_id())
	return data

def clear_multi_action(chat_id, message_id):
	"""Clears the multi-action session for a message"""
	delete_cache_item(f"multi_action_{chat_id}_{message_id}")

def get_container_name_by_id(container_ref_or_id):
	"""
	The name of a container, asked of the host its reference points at.

	Returns None when the host cannot be reached or the container is gone,
	which the caller reports as "does not exist" either way.
	"""
	_, container = find_container(container_ref_or_id)
	return container.name if container is not None else None


def find_container_id_on_host(host_id, container_name):
	"""
	The short id of a container by name, on one host only.

	Deliberately not a search across hosts: this resolves the id of a button
	whose container was recreated, and the button already says which machine
	it meant. Searching everywhere could land the action on a container of the
	same name on a different host, which is the one mistake in all of this
	that would be both silent and destructive.
	"""
	try:
		owner = manager(host_id)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach host {host_id} to look up {container_name}: {e.reason}")
		return None
	try:
		container = owner.container_named(container_name)
		if container is not None:
			return container.id[:CONTAINER_ID_LENGTH]
	except Exception as e:
		debug(f"Could not look up {container_name} on {host_id}: {e}")
	return None

def get_container_name(chat_id, message_id, container_id):
	"""
	Returns the container name from cache with fallback to the Docker API

	Args:
		chat_id: Chat ID
		message_id: Message ID
		container_id: Container ID

	Returns:
		str: Container name or None if it doesn't exist
	"""
	# 1. Try the cache
	name = load_container_name(chat_id, message_id, container_id)
	if name:
		return name

	# 2. Fallback to the Docker API
	return get_container_name_by_id(container_id)

def short_hash(text, length=30):
	hash_obj = hashlib.sha256(text.encode())
	return hash_obj.hexdigest()[:length]

# --- Project-name hashing for callback_data (avoid 64-byte Telegram limit) ---
PROJECT_HASH_CACHE_KEY = "project_hash_map"
PROJECT_HASH_LENGTH = 8
_project_hash_lock = threading.Lock()  # Atomic read-modify-write for the mapping

def register_project_hash(project_name, host_id=None):
	"""
	Returns a short hash standing for a project on a host, remembering the
	mapping.

	The host goes into the hash, not just into the stored value: two machines
	can run a project of the same name, and hashing only the name would give
	both the same button.
	"""
	if not project_name:
		return project_name
	host_id = host_id or host_registry.local_host_id()
	h = short_hash(f"{host_id}{CONTAINER_REF_SEPARATOR}{project_name}", PROJECT_HASH_LENGTH)
	entry = {"host": host_id, "name": project_name}
	with _project_hash_lock:
		mapping = read_cache_item(PROJECT_HASH_CACHE_KEY) or {}
		if mapping.get(h) != entry:
			mapping[h] = entry
			write_cache_item(PROJECT_HASH_CACHE_KEY, mapping)
	return h

def resolve_project_hash(value):
	"""
	Resolves a hash back to (host_id, project_name), or (None, None).

	Entries written before hosts existed are bare strings, and mean the local
	host: a button from an older message still has to work.
	"""
	if not value:
		return None, None
	mapping = read_cache_item(PROJECT_HASH_CACHE_KEY) or {}
	entry = mapping.get(value)
	if entry is None:
		return None, None
	if isinstance(entry, str):
		return host_registry.local_host_id(), entry
	return entry.get("host") or host_registry.local_host_id(), entry.get("name")

def resolve_project_name(value):
	"""Just the project name for a hash, or None. Kept for callers that have
	the host from somewhere else."""
	return resolve_project_hash(value)[1]

def generate_docker_compose(container):
	"""
	Builds the docker-compose of a container.

	Reuses `extract_container_config`, the same extractor every update relies
	on, so the compose reflects what the user actually configured instead of
	re-reading a handful of `attrs` fields: values inherited from the image
	(PATH, the image ENTRYPOINT, its LABELs...) are already filtered out there.
	"""
	config = extract_container_config(container)
	return ComposeGenerator(container.name, config).to_yaml()

# ============================================================================
# INTERNAL TELEGRAM FUNCTIONS (without queue)
# ============================================================================
def _send_message_direct(chat_id, message, reply_markup, parse_mode, disable_web_page_preview, message_thread_id=None):
	"""Sends a message directly without using the queue"""
	try:
		if message is None:
			message = ""
		if message_thread_id is None:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
		else:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview, message_thread_id=message_thread_id)
	except Exception as e:
		error(f"Error sending message to chat {chat_id}. Message: [{str(message)}]. Error: [{str(e)}]")
		raise

def _send_document_direct(chat_id, document, reply_markup, caption, parse_mode, message_thread_id=None):
	"""Sends a document directly without using the queue"""
	try:
		if message_thread_id is None:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, parse_mode=parse_mode)
		else:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, message_thread_id=message_thread_id, parse_mode=parse_mode)
	except Exception as e:
		error(f"Error sending document to chat {chat_id}. Error: [{e}]")
		raise

def _delete_message_direct(chat_id, message_id):
	"""Deletes a message directly without using the queue"""
	try:
		if chat_id and message_id:
			bot.delete_message(chat_id, message_id)
	except Exception as e:
		# Silently ignore errors when deleting messages (they may have been deleted already)
		pass

def _is_message_not_modified(exception):
	"""
	True when Telegram rejected an edit because the message is already exactly
	what we are sending. It happens legitimately (e.g. re-restarting a container
	that is already marked as done in a multi-action menu) and must not go
	through the queue retry loop, which would stall every pending message.
	"""
	return "message is not modified" in str(exception).lower()

def _edit_message_text_direct(chat_id, message_id, text, parse_mode, reply_markup):
	"""Edits the text of a message directly without using the queue"""
	try:
		return bot.edit_message_text(text, chat_id, message_id, parse_mode=parse_mode, reply_markup=reply_markup)
	except Exception as e:
		if _is_message_not_modified(e):
			debug(f"Message {message_id} already up to date, nothing to edit")
			return None
		debug(f"Could not edit message {message_id}: {e}")
		raise

def _edit_message_reply_markup_direct(chat_id, message_id, reply_markup):
	"""Edits the markup of a message directly without using the queue"""
	try:
		return bot.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
	except Exception as e:
		if _is_message_not_modified(e):
			debug(f"Markup of message {message_id} already up to date, nothing to edit")
			return None
		debug(f"Could not edit markup of message {message_id}: {e}")
		raise

# ============================================================================
# PUBLIC FUNCTIONS USING THE MESSAGE QUEUE
# ============================================================================
def delete_message(message_id, chat_id=None):
	"""Deletes a message using the queue (async)"""
	if chat_id is None:
		chat_id = get_reply_chat_id()
	message_queue.add_message(_delete_message_direct, chat_id, message_id, wait_for_result=False)

def send_message(chat_id=None, message=None, reply_markup=None, parse_mode="html", disable_web_page_preview=True):
	"""Sends a message using the queue (waits for result to get the message_id)"""
	if chat_id is None:
		chat_id = get_reply_chat_id()
	message_thread_id = get_reply_thread_id(chat_id)
	return message_queue.add_message(_send_message_direct, chat_id, message, reply_markup, parse_mode, disable_web_page_preview, message_thread_id, wait_for_result=True)

def send_message_to_notification_channel(chat_id=None, message=None, reply_markup=None, parse_mode="html", disable_web_page_preview=True):
	"""
	Sends a container status change notification. It goes to
	the notification channel when one is configured, otherwise it
	is delivered like any other message (the chat being answered, or
	TELEGRAM_GROUP when there is no interaction going on).
	"""
	if chat_id is None:
		chat_id = notification_channel()
	if chat_id is None or chat_id == '':
		return send_message(message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
	return send_message(chat_id=chat_id, message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)

def send_document(chat_id=None, document=None, reply_markup=None, caption=None, parse_mode="html"):
	"""Sends a document using the queue (waits for result to get the message_id)"""
	if chat_id is None:
		chat_id = get_reply_chat_id()
	message_thread_id = get_reply_thread_id(chat_id)
	return message_queue.add_message(_send_document_direct, chat_id, document, reply_markup, caption, parse_mode, message_thread_id, wait_for_result=True)

def edit_message_text(text, chat_id, message_id, parse_mode="html", reply_markup=None):
	"""Edits the text of a message using the queue (async, does not block on failure)"""
	message_queue.add_message(_edit_message_text_direct, chat_id, message_id, text, parse_mode, reply_markup, wait_for_result=False)

def edit_message_reply_markup(chat_id, message_id, reply_markup):
	"""Edits the markup of a message using the queue (async)"""
	message_queue.add_message(_edit_message_reply_markup_direct, chat_id, message_id, reply_markup, wait_for_result=False)

def edit_message_reply_markup_sync(chat_id, message_id, reply_markup):
	"""Edits the markup of a message using the queue (sync, waits for confirmation)"""
	return message_queue.add_message(_edit_message_reply_markup_direct, chat_id, message_id, reply_markup, wait_for_result=True)

def delete_updater():
	container_id = get_container_id_by_name(UPDATER_CONTAINER_NAME)
	if container_id:
		container = docker_manager.client.containers.get(container_id)
		try:
			updater_image = container.image.id
			container.stop()
			container.remove()
			docker_manager.client.images.remove(updater_image)
			send_message(message=get_text("updated_container", CONTAINER_NAME))
		except Exception as e:
			error(f"Could not delete container {UPDATER_CONTAINER_NAME}. Error: [{e}]")

def check_CONTAINER_NAME():
	container_id = get_container_id_by_name(CONTAINER_NAME)
	if not container_id:
		error(get_text("error_bot_container_name"))
		sys.exit(1)

def parse_schedule_expression(line):
	"""
	Parse a schedule line into schedule expression and action+params.

	Supports two formats:
	1. Special cron: @daily run container
	2. Normal cron: 0 0 * * * run container

	Returns: (schedule_expression, action, params) or (None, None, None) if invalid
	"""
	parts = line.strip().split()

	if not parts:
		return None, None, None

	# Check if it's a special cron expression (starts with @)
	if parts[0].startswith("@"):
		schedule = parts[0]
		action_and_params = parts[1:]
	else:
		# Normal cron expression (5 parts: minute hour day month weekday)
		if len(parts) < 5:
			return None, None, None

		schedule = " ".join(parts[:5])
		action_and_params = parts[5:]

	# Extract action and parameters
	if not action_and_params:
		return None, None, None

	action = action_and_params[0].lower()
	params = action_and_params[1:]

	return schedule, action, params


def parse_cron_line(line):
	"""
	Parse a complete schedule line and validate all components.

	Format: [CRON_EXPRESSION] ACTION [PARAMS...]

	Returns: dict with schedule, action, and parsed parameters, or None if invalid
	"""
	schedule, action, params = parse_schedule_expression(line)

	if schedule is None or action is None:
		return None

	# Validate schedule expression
	if not is_valid_cron(schedule):
		return None

	# Validate action and parameters using SCHEDULE_PATTERNS
	if action not in SCHEDULE_PATTERNS:
		return None  # Unknown action

	pattern = SCHEDULE_PATTERNS[action]
	required_params = pattern.get("params", [])
	validators = pattern.get("validators", {})

	# Check if we have enough parameters
	if len(params) < len(required_params):
		return None

	result = {
		"schedule": schedule,
		"action": action,
	}

	# Parse and validate parameters
	for i, param_name in enumerate(required_params):
		param_value = params[i] if i < len(params) else None

		if param_value is None:
			return None

		# Apply validator if exists
		if param_name in validators:
			validator = validators[param_name]
			try:
				if not validator(param_value):
					return None
			except Exception as e:
				# Validator threw an exception, consider it invalid
				error(f"Validator error for {param_name}: {str(e)}")
				return None

		# Special handling for command parameter (joins remaining params)
		if param_name == "command":
			result[param_name] = " ".join(params[i:])
		# Special handling for show_output (convert to int)
		elif param_name == "show_output":
			try:
				result[param_name] = int(param_value)
			except (ValueError, TypeError):
				return None
		else:
			result[param_name] = param_value

	return result

def is_valid_cron(cron_expression):
	"""
	Validate a cron expression.

	Supports:
	- Special expressions: @reboot, @daily, @hourly, etc.
	- Normal cron: 0 0 * * *, etc.
	"""
	# @reboot is not a valid croniter expression, but we support it
	if cron_expression == "@reboot":
		return True

	# Check other special cron expressions (supported by croniter)
	if cron_expression in SPECIAL_CRON_EXPRESSIONS:
		try:
			croniter(cron_expression)
			return True
		except Exception:
			return False

	# Try to validate as normal cron expression
	try:
		# Split and check that we have exactly 5 fields (minute, hour, day, month, weekday)
		# croniter accepts 6 fields (with seconds), but we only want standard 5-field cron
		fields = cron_expression.split()
		if len(fields) != 5:
			return False

		croniter(cron_expression)
		return True
	except Exception:
		return False

def get_my_architecture():
	try:
		info = docker_manager.client.info()
		architecture_docker = info['Architecture']
		return docker_architectures.get(architecture_docker, architecture_docker)
	except Exception as e:
		error(f"Error getting Docker architecture: [{e}]")
		return None

def get_docker_tags(repo_name):
	"""Get available tags for a Docker image"""
	try:
		if repo_name.startswith("ghcr.io/"):
			debug(f"Getting tags from ghcr.io registry for {repo_name}")
			try:
				tags = get_docker_tags_from_ghcr(repo_name.replace("ghcr.io/", ""))
				return tags if tags else []
			except Exception as e:
				error(f"Failed to get tags from ghcr.io for {repo_name}: {str(e)}")
				return []
		elif repo_name.startswith("lscr.io/"):
			debug(f"Getting tags from DockerHub for {repo_name}")
			try:
				architecture = get_my_architecture()
				if architecture is None:
					error(f"Could not determine system architecture for {repo_name}")
					return []
				return get_docker_tags_from_DockerHub(repo_name.replace("lscr.io/", ""))
			except Exception as e:
				error(f"Failed to get tags from DockerHub for {repo_name}: {str(e)}")
				return []
		else:
			debug(f"Getting tags from DockerHub for {repo_name}")
			try:
				architecture = get_my_architecture()
				if architecture is None:
					error(f"Could not determine system architecture for {repo_name}")
					return []
				return get_docker_tags_from_DockerHub(repo_name)
			except Exception as e:
				error(f"Failed to get tags from DockerHub for {repo_name}: {str(e)}")
				return []
	except Exception as e:
		error(f"Failed to get tags for {repo_name}: {str(e)}")
		return []

def get_docker_tags_from_DockerHub(repo_name):
	architecture = get_my_architecture()
	if architecture is None:
		return []

	# Handle official Docker Hub images (e.g., redis, nginx, postgres)
	# Official images need 'library/' prefix in the API URL
	if '/' not in repo_name:
		# Official image - add library/ prefix
		full_repo_name = f"library/{repo_name}"
	else:
		# User image - use as-is
		full_repo_name = repo_name

	url = f"https://hub.docker.com/v2/repositories/{full_repo_name}/tags?page_size=99"
	try:
		response = requests.get(url, timeout=10)
		if response.status_code == 404:
			raise Exception(f'Repository not found: {repo_name}')
		elif response.status_code != 200:
			raise Exception(f'Error calling to {url}: {response.status_code}')

		data = response.json()
		tags = data.get('results', [])
		filtered_tags = []
		for tag in tags:
			images = tag.get('images', [])
			for image in images:
				if image['architecture'] == architecture:
					filtered_tags.append(tag['name'])
					break

		# If no tags found for this architecture, return all tags
		if not filtered_tags and tags:
			debug(f"No tags found for architecture {architecture} in {repo_name}, returning all tags")
			filtered_tags = [tag['name'] for tag in tags]

		return filtered_tags
	except Exception as e:
		error(f"Error getting tags from DockerHub for {repo_name}: {e}")
		raise

def get_docker_tags_from_ghcr(repo_name):
	"""Get tags from ghcr.io using Docker Registry V2 API"""
	try:
		# Get auth token
		token_url = f'https://ghcr.io/token?service=ghcr.io&scope=repository:{repo_name}:pull'
		token = requests.get(token_url, timeout=10).json().get('token')
		if not token:
			error(f"Could not get an auth token from ghcr.io for {repo_name}")
			return []

		# Get tags
		tags_url = f'https://ghcr.io/v2/{repo_name}/tags/list'
		tags = requests.get(tags_url, headers={'Authorization': f'Bearer {token}'}, timeout=10).json().get('tags', [])

		if not tags:
			debug(f"No tags returned by ghcr.io for {repo_name}")
			return []

		# Sort: version tags first (newest), then others
		version_tags = sorted([t for t in tags if t and t[0] == 'v' and any(c.isdigit() for c in t)], reverse=True)
		other_tags = sorted([t for t in tags if t not in version_tags])

		return (version_tags + other_tags)[:20]  # Limit to 20

	except Exception as e:
		error(f"Error getting tags from ghcr.io/{repo_name}: {e}")
		return []

# Global schedule monitor instance (used by /schedule command)
schedule_monitor = None

def register_bot_commands():
	"""
	Publishes the command menu to Telegram.

	Telegram keeps this list on its side, already translated, so it has to be
	sent again whenever the language changes or the menu would stay in the
	language that was configured when the bot started.
	"""
	bot.set_my_commands([
		telebot.types.BotCommand("/start", get_text("menu_start")),
		telebot.types.BotCommand("/list", get_text("menu_list")),
		telebot.types.BotCommand("/run", get_text("menu_run")),
		telebot.types.BotCommand("/stop", get_text("menu_stop")),
		telebot.types.BotCommand("/restart", get_text("menu_restart")),
		telebot.types.BotCommand("/delete", get_text("menu_delete")),
		telebot.types.BotCommand("/exec", get_text("menu_exec")),
		telebot.types.BotCommand("/checkupdate", get_text("menu_update")),
		telebot.types.BotCommand("/updateall", get_text("menu_update_all")),
		telebot.types.BotCommand("/changetag", get_text("menu_change_tag")),
		telebot.types.BotCommand("/logs", get_text("menu_logs")),
		telebot.types.BotCommand("/logfile", get_text("menu_logfile")),
		telebot.types.BotCommand("/schedule", get_text("menu_schedule")),
		telebot.types.BotCommand("/settings", get_text("menu_settings")),
		telebot.types.BotCommand("/compose", get_text("menu_compose")),
		telebot.types.BotCommand("/prune", get_text("menu_prune")),
		telebot.types.BotCommand("/mute", get_text("menu_mute")),
		telebot.types.BotCommand("/info", get_text("menu_info")),
		telebot.types.BotCommand("/ports", get_text("menu_ports")),
		telebot.types.BotCommand("/version", get_text("menu_version")),
		telebot.types.BotCommand("/donate", get_text("menu_donate")),
		telebot.types.BotCommand("/donors", get_text("menu_donors"))
		])


def main():
	"""
	Starts the daemons, publishes the command menu and begins polling.

	Called by the entry point once every module that registers commands
	and callbacks has been imported, so nothing is missing by the time
	the first message arrives.
	"""
	debug(f"Starting bot version {VERSION}")

	# One event stream per host, kept in step with what is configured.
	event_monitors = EventMonitorSupervisor()
	event_monitors.start()
	# The update daemon always starts. It consults the check_updates setting on
	# every pass, so switching checks back on from /settings takes effect without
	# recreating the container.
	updateMonitor = DockerUpdateMonitor()
	updateMonitor.demonio_update()
	debug(f"Update daemon started (checks {'enabled' if store.get('bot.check_updates') else 'disabled'})")

	schedule_monitor = DockerScheduleMonitor()
	schedule_monitor.demonio_schedule()
	debug("Schedule daemon started")

	# The session cache is interface state in the storage volume: without a
	# sweep it only ever grows, one file per menu opened.
	_schedule_cache_sweep()

	register_bot_commands()
	delete_updater()
	check_CONTAINER_NAME()
	check_mute()
	starting_message = f"🫡 <b>{CONTAINER_NAME}</b>\n{get_text('active')}"
	if store.get("bot.check_updates"):
		starting_message += f"\n✅ {get_text('check_for_updates')}"
	else:
		starting_message += f"\n❌ {get_text('check_for_updates')}"
	if store.get("bot.multi_selection"):
		starting_message += f"\n✅ {get_text('multi_selection')}"
	else:
		starting_message += f"\n❌ {get_text('multi_selection')}"
	starting_message += f"\n<i>⚙️ v{VERSION}</i>"
	starting_message += f"\n{get_text('channel')}"
	send_message(message=starting_message)
	if _migration.ask_for_language:
		ask_initial_language()
	bot.infinity_polling(timeout=60)
