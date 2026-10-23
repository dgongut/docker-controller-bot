import contextvars
import docker
import functools
import hashlib
import html
import ipaddress
import io
import json
import os
import queue
import re
import requests
import shlex
import signal
import sys
import telebot
import threading
import time
import uuid
import migration
import store
import telemetry
from config import *
from croniter import croniter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from compose_generator import ComposeGenerator
from formatting import (
	EVENT_LOG_LINES, by_host, render_container_info, comparison_version_change, comparison_version_line, comparison_version_notes, container_line, describe_exit_code, human_duration, log_excerpt, release_notes_url, version_lines,
)
from docker_update import container_platform, extract_container_config, image_repository, image_version, perform_update, pull_checked, stop_container, updates_in_progress
from docker_compose_manager import (
    ComposeDetector,
    ComposeProjectManager
)
from schedule_manager import ScheduleManager
from own_container import candidates as own_container_ids
from port_manager import PortManager
import callback_registry
import container_listing
import host_registry
from i18n import get_text, language
from logger import debug, error, warning
from message_queue import MessageQueue

VERSION = "5.2.0"

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
# The administrators' ids, read once. Spaces around the commas, or one after
# the last, are how a list is commonly written: compared as typed, "123, 456"
# left the second administrator out with no hint of why.
ADMIN_IDS = frozenset(a.strip() for a in str(TELEGRAM_ADMIN or "").split(",") if a.strip())
if not ADMIN_IDS:
	error("You need to configure the chatId of the user who will interact with the bot with the TELEGRAM_ADMIN variable")
	sys.exit(1)
if str(ANONYMOUS_USER_ID) in ADMIN_IDS:
	error("You cannot be anonymous to control the bot. In the variable TELEGRAM_ADMIN you have to put your user id.")
	sys.exit(1)
if TELEGRAM_GROUP is None or TELEGRAM_GROUP.strip() == '':
	if len(ADMIN_IDS) > 1:
		error("Multiple administrators can only be specified if used in a group (using the TELEGRAM_GROUP variable)")
		sys.exit(1)
	TELEGRAM_GROUP = next(iter(ADMIN_IDS))
else:
	TELEGRAM_GROUP = TELEGRAM_GROUP.strip()

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

def topic_of(message):
	"""
	The forum topic a message was written in, 1 for the general one.

	Only forums have topics. Elsewhere Telegram still fills message_thread_id
	in a reply —with the message the thread of replies started from— and
	taking that for a topic ignored, without a word, a command written as a
	reply to someone else.
	"""
	if not getattr(message.chat, "is_forum", False):
		return 1
	return message.message_thread_id or 1


# How long a question the bot asked can be answered by writing: /exec's
# command, a setting, a port, a step of /schedule. The answer carries only who
# wrote it, so the question also remembers the chat it was asked in. Without
# either, any loose message from the admin, days later and in another chat,
# was taken as the answer: posted to the group as a command to confirm, or
# saved as a host's alias.
PENDING_ANSWER_SECONDS = 15 * 60

def asked_here(value):
	"""A pending question's state, stamped with where and when it was asked."""
	value = dict(value)
	value["asked_in"] = str(get_reply_chat_id())
	value["asked_at"] = time.time()
	return value

def answerable(state, chat_id):
	"""The pending state if a message in `chat_id` now can answer it, else None."""
	if not isinstance(state, dict):
		return None
	asked_at = state.get("asked_at")
	if not isinstance(asked_at, (int, float)) or time.time() - asked_at > PENDING_ANSWER_SECONDS:
		return None
	if str(state.get("asked_in")) != str(chat_id):
		return None
	return state

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

	def __init__(self, host_id=None, client=None):
		self.host_id = host_id or host_registry.local_host_id()
		self.client = client or host_registry.client(self.host_id)
		self.compose_manager = ComposeProjectManager(self.client, where=self.alias)

	@property
	def alias(self):
		"""The host's display name, for messages that have to say where."""
		return host_registry.alias(self.host_id)

	def list_containers(self, comando="", inspect=False):
		"""
		This host's containers: the bot first, then by state, then by name.

		Built from the daemon's list in one request —see container_listing—
		because what the menus, /list and the caches read is in the list
		itself. The SDK's list() is that request plus an inspect per
		container, and over ssh:// each of those is a round-trip to another
		machine. `inspect` asks for the full objects anyway, for the callers
		that read what only an inspect carries: /ports and its PortBindings.
		"""
		comando = comando.split('@', 1)[0]
		if comando == "/run":
			query = {"filters": {'status': ['paused', 'exited', 'created', 'dead']}}
		elif comando == "/stop" or comando == "/restart":
			query = {"filters": {'status': ['running', 'restarting']}}
		elif comando == "/exec":
			query = {"filters": {'status': ['running']}}
		else:
			query = {"all": True}
		if inspect:
			containers = self.client.containers.list(**query)
		else:
			containers = container_listing.list_containers(self.client, where=self.alias, **query)
		status_order = {'running': 0, 'restarting': 1, 'paused': 2, 'exited': 3, 'created': 4, 'dead': 5}
		sorted_containers = sorted(containers, key=lambda x: (
			0 if is_own_container(self.host_id, x.id, x.name) else 1,
			status_order.get(x.status, 6), x.name.lower()))
		return sorted_containers

	def container_named(self, name):
		"""
		The container with exactly this name on this host, or None.

		The daemon does the filtering rather than list_containers: this runs on
		every button press, and over ssh pulling the whole list to discard all
		but one is a round-trip the size of the machine instead of the size of
		the answer. Docker's name filter matches by substring, hence the exact
		comparison afterwards. From the list alone, without an inspect for
		each substring match: the callers want the id, and the update emoji
		the image, both of which the list says.
		"""
		for container in container_listing.list_containers(self.client, all=True, filters={"name": name}, where=self.alias):
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
			if is_own_container(self.host_id, container_id, container_name):
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			stop_container(container)
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=f'{get_text("stopped_container", container_name)}{host_suffix(self.host_id)}')
			return None
		except Exception as e:
			error(f"Could not stop container {container_name}. Error: [{e}]")
			return get_text("error_stopping_container", container_name)

	def restart_container(self, container_id, container_name, from_schedule=False):
		try:
			if is_own_container(self.host_id, container_id, container_name):
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.restart()
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=f'{get_text("restarted_container", container_name)}{host_suffix(self.host_id)}')
			return None
		except Exception as e:
			error(f"Could not restart container {container_name}. Error: [{e}]")
			return get_text("error_restarting_container", container_name)

	def start_container(self, container_id, container_name, from_schedule=False):
		try:
			if is_own_container(self.host_id, container_id, container_name):
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.start()
			# Send confirmation only for manual commands when muted: the event
			# monitor is silent while muted, so the user gets no other feedback
			if from_schedule is False and is_muted():
				send_message(message=f'{get_text("started_container", container_name)}{host_suffix(self.host_id)}')
			return None
		except Exception as e:
			error(f"Could not start container {container_name}. Error: [{e}]")
			return get_text("error_starting_container", container_name)

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			# Only the end, which is all the message shows. Without a tail the
			# daemon sent the whole history, gigabytes of it under json-file,
			# which never rotates unless told to. And whatever bytes it holds: a
			# single one that was not UTF-8 failed every /logs of that container.
			logs = container.logs(tail=LOGS_TAIL_LINES).decode("utf-8", errors="replace")
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
		"""
		The container's log as (text, cut), or None when it could not be read.

		Read as a stream keeping only the last LOGFILE_MAX_BYTES: a log of
		gigabytes read whole, two or three copies of it in memory, took the bot
		down on a small machine, and Telegram would not have taken more than
		50 MB of it anyway. None rather than an error text, which was sent as
		if it were the log.
		"""
		try:
			container = self.client.containers.get(container_id)
			stream = container.logs(stream=True, follow=False)
			kept = bytearray()
			cut = False
			for chunk in ([stream] if isinstance(stream, (bytes, bytearray)) else stream):
				kept += chunk
				if len(kept) > 2 * LOGFILE_MAX_BYTES:
					del kept[:len(kept) - LOGFILE_MAX_BYTES]
					cut = True
			if len(kept) > LOGFILE_MAX_BYTES:
				del kept[:len(kept) - LOGFILE_MAX_BYTES]
				cut = True
			text = kept.decode("utf-8", errors="replace")
			# Every byte that was not UTF-8 grows to three once written out again.
			if len(text.encode("utf-8")) > LOGFILE_MAX_BYTES:
				text = text[-(LOGFILE_MAX_BYTES // 3):]
				cut = True
			if cut:
				# From the first whole line.
				text = text.split("\n", 1)[-1]
			return text, cut
		except Exception as e:
			error(f"The logs for container {container_name} could not be shown. Error: [{e}]")
			return None

	def get_docker_compose(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return generate_docker_compose(container)
		except Exception as e:
			error(f"Could not show docker compose for container {container_name}. Error: [{e}]")
			# None, not the error text: compose() looked for an "Error" prefix
			# that no translation has, and sent the error as docker-compose.txt.
			return None

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
				return get_text("error_project_not_found", html.escape(str(project_name)))

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
				status_emoji = get_status_emoji(container.status, container.name, container, self.host_id)

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
			return get_text("error_showing_info_project", html.escape(str(project_name)))

	def get_info(self, container_id, container_name):
		"""
		The /info of a container: (text, has_update, can_start, info).

		`info` is what was gathered, for the button that sets what to do with
		a new version and to draw the text again when it is pressed; None when
		it could not be gathered and there is nothing to set.

		Everything is gathered here and drawn in formatting.render_container_info,
		so what the message says and how it looks are tested apart.
		"""
		try:
			container = self.client.containers.get(container_id)
			info = gather_container_info(self, container)
			return (render_container_info(info), info.get("has_update") is True,
					info["status"] in ("exited", "dead", "created"), info)
		except Exception as e:
			error(f"Could not display information for container {container_name}. Error: [{e}]")
			return get_text("error_showing_info_container", container_name), False, False, None

	def update(self, container_id, container_name, message, bot, tag=None):
		"""
		Update a container with a new image while preserving all configuration.
		Uses docker_update module for the actual update logic.
		"""
		try:
			if is_own_container(self.host_id, container_id, container_name):
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
				# The version it is leaving, for the bot that comes back to say
				# what it updated from: it is the only one that will know.
				store.state_set("self_update_from", VERSION)
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
					save_status_func=lambda image, name, has_update: save_container_update_status(image, name, has_update, self.host_id),
					container_id_length=CONTAINER_ID_LENGTH,
					telegram_group=message.chat.id if message else get_reply_chat_id(),
					# Where the progress went: an auto-update reports to the
					# notification channel, a button to the chat it was pressed in.
					notify_func=update_notify(self.host_id, message.chat.id if message else None),
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
				notify_func=update_notify(self.host_id, None),
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
		versions = None
		try:
			container = self.client.containers.get(container_id)
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', '')
			local_image = container.image.id

			# Show loading message while pulling the image (can be slow for big images)
			loading_msg = send_message(message=get_text("fetching_image_data"))

			try:
				remote_image = pull_image(self.client, image_with_tag, container)
				if not remote_image or not remote_image.id:
					error(f"Failed to pull image {image_with_tag}. Verify that the image exists in the registry.")
					has_update = None
					save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
					if loading_msg:
						delete_message(loading_msg.message_id)
						loading_msg = None
					send_message(message=f'{host_label(self.host_id)}{get_text("error_pulling_image", image_with_tag)}')
					return
			except docker.errors.ImageNotFound:
				error(f"Image {image_with_tag} not found in registry. Check the image name.")
				has_update = None
				save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
				if loading_msg:
					delete_message(loading_msg.message_id)
					loading_msg = None
				send_message(message=f'{host_label(self.host_id)}{get_text("error_pulling_image", image_with_tag)}')
				return
			except docker.errors.APIError as e:
				error(f"Error pulling image {image_with_tag}. Error: [{e}]")
				has_update = None
				save_container_update_status(image_with_tag, container.name, has_update, self.host_id)
				if loading_msg:
					delete_message(loading_msg.message_id)
					loading_msg = None
				send_message(message=f'{host_label(self.host_id)}{get_text("error_pulling_image", image_with_tag)}')
				return

			local_image_normalized = local_image.replace('sha256:', '')
			remote_image_normalized = remote_image.id.replace('sha256:', '')

			debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image_normalized[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image_normalized[:CONTAINER_ID_LENGTH]}]")

			if loading_msg:
				delete_message(loading_msg.message_id)
				loading_msg = None

			if local_image_normalized != remote_image_normalized \
					and not same_running_manifest(self.client, container, local_image, remote_image):
				# Keep the pulled image cached locally so the subsequent update
				# operation does not need to re-download it.
				debug(f"{container.name} update detected! Keeping downloaded image [{remote_image_normalized[:CONTAINER_ID_LENGTH]}] for upcoming update")
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container_ref(self.host_id, container)}"))
				has_update = True
				versions = (running_version(self.host_id, container), available_version(remote_image, image_with_tag))
				sent_message = send_message(
					message=f'{get_text("available_update", container.name)}{host_suffix(self.host_id)}{version_lines(*versions)}',
					reply_markup=markup)
				# Save container cache for this notification
				if sent_message:
					save_container_cache(sent_message.chat.id, sent_message.message_id, [container], self.host_id)
			else:
				has_update = False
				send_message(message=f'{get_text("already_updated", container.name)}{host_suffix(self.host_id)}')
		except Exception as e:
			error(f"Could not check update for {getattr(container, 'name', container_id)} "
				  f"({image_with_tag or '?'}) on {self.alias}: [{e}]")
			has_update = None
			if loading_msg:
				try:
					delete_message(loading_msg.message_id)
				except:
					pass

		if image_with_tag and container is not None and getattr(container, 'name', None):
			save_container_update_status(image_with_tag, container.name, has_update, self.host_id, versions)

	def delete(self, container_id, container_name):
		try:
			if is_own_container(self.host_id, container_id, container_name):
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container_is_running = container.status in ['running', 'restarting', 'paused', 'created']
			if container_is_running:
				debug(f"Container {container_name} is running. It will be stopped.")
				stop_container(container)
			container.remove()
			forget_container(self.host_id, container.name)
			return get_text("deleted_container", container_name)
		except Exception as e:
			error(f"Could not delete container {container_name}. Error: [{e}]")
			return get_text("error_deleting_container", container_name)

	# Each prune returns (message, details): the details go out as a file, and
	# are None when it failed, so there is no file to send. A failure used to
	# return the message alone, which the callers' unpacking turned into a
	# ValueError: the /prune button answered nothing, and a scheduled one only
	# left a line in the log.
	def prune_containers(self):
		return self._prune("containers", "prune_containers", "error_prune_containers", "docker container prune -f", reclaims=True)

	def prune_images(self):
		return self._prune("images", "prune_images", "error_prune_images", "docker image prune -a -f", reclaims=True, filters={'dangling': False})

	def prune_networks(self):
		return self._prune("networks", "prune_networks", "error_prune_networks", "docker network prune -f", reclaims=False)

	def prune_volumes(self):
		return self._prune("volumes", "prune_volumes", "error_prune_volumes", "docker volume prune -f", reclaims=True)

	def _prune(self, collection, done_key, error_key, command, reclaims, **kwargs):
		"""
		Runs one prune through the host's slow client.

		The working client gives up after thirty seconds, and the daemon
		cancels a prune as soon as the client hangs up: on a slow disk with
		gigabytes of old images every attempt was cut short, nothing was freed
		and the next one had just as much to do. A timeout that still runs out
		says so, with the command that does the same on the server, instead of
		pointing at the logs for an error there is nothing to do about.
		"""
		try:
			connection = host_registry.slow_client(self.host_id)
		except host_registry.HostUnavailable:
			connection = self.client
		try:
			pruned = getattr(connection, collection).prune(**kwargs)
			if not reclaims:
				debug(f"Deleted: [{str(pruned)}]")
				return get_text(done_key), str(pruned)
			file_size_bytes = sizeof_fmt((pruned or {}).get('SpaceReclaimed') or 0)
			debug(f"Deleted: [{str(pruned)}] - Space reclaimed: {str(file_size_bytes)}")
			return get_text(done_key, str(file_size_bytes)), str(pruned)
		except requests.exceptions.Timeout as e:
			error(f"Docker did not finish pruning {collection} in time, so it cancelled it. Error: [{e}]")
			return get_text("error_prune_timeout", str(host_registry.SLOW_TIMEOUT_SECONDS // 60), command), None
		except docker.errors.APIError as e:
			# The daemon runs one prune at a time. A scheduled one and a /prune
			# can meet, and that is not an error worth reading the logs for.
			if e.status_code == 409 and "already running" in str(e):
				warning(f"Not pruning {collection}: Docker is already running a prune. [{e}]")
				return get_text("error_prune_busy"), None
			error(f"An error occurred deleting unused {collection}. Error: [{e}]")
			return get_text(error_key), None
		except Exception as e:
			error(f"An error occurred deleting unused {collection}. Error: [{e}]")
			return get_text(error_key), None

	def execute_command(self, container_id, container_name, command):
		"""
		The command's output, or None when it could not be run.

		None rather than an error text: returned the same way as the output,
		the error was shown as if it were what the command had printed.
		"""
		try:
			container = self.client.containers.get(container_id)
			# Some minimal/distroless images do not ship a shell.
			# Try common shells first and, if none is available, run the command directly.
			for exec_command in (['sh', '-c', command], ['bash', '-c', command]):
				try:
					result = container.exec_run(exec_command)
				except docker.errors.APIError:
					continue
				if not self._is_executable_missing(result, exec_command[0]):
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
			return None

	def _decode_exec_output(self, result):
		# Whatever the command printed: a byte that is not UTF-8 —a file name
		# in Latin-1, a binary— made a command that had run read as one that
		# could not be.
		output = result.output.decode('utf-8', errors='replace') if result.output else ''
		if not output:
			output = get_text("command_executed_without_output")
		return output

	def _is_executable_missing(self, result, executable):
		"""
		Whether the daemon could not start `executable` at all.

		Only the daemon's own refusal counts, naming that executable. Finding
		the words anywhere in the output took a command that ran and printed
		them —a deploy script reporting a service that would not start— for a
		missing shell, and ran it twice more, under bash and then directly.
		"""
		if result.exit_code not in (None, 126, 127):
			return False
		output = (result.output or b'').decode('utf-8', errors='replace').strip()
		named = f'"{executable}"' in output or f'`{executable}`' in output
		if output.startswith("OCI runtime exec failed"):
			return named
		return named and ("executable file not found" in output or "no such file or directory" in output.lower())

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
	# Outside the lock: reaching a host that is not answering takes as long as
	# its timeout, and over ssh longer. Holding the one lock every host shares
	# meant a press on a dead machine stalled the menus of all the others,
	# until the parallel sweeps gave up on them and marked them down too.
	current = host_registry.client(host_id)
	with _managers_lock:
		existing = _managers.get(host_id)
		if existing is not None and existing.client is current:
			return existing
	built = DockerManager(host_id, client=current)
	with _managers_lock:
		existing = _managers.get(host_id)
		if existing is not None and existing.client is current:
			# Another thread built one for the same client meanwhile.
			return existing
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


def forget_manager(host_id):
	"""Drops the cached manager for one host, so its next use reconnects."""
	with _managers_lock:
		_managers.pop(host_id, None)


def forget_managers():
	"""Drops every cached manager, so the next use reconnects."""
	with _managers_lock:
		_managers.clear()


def disconnect_host(host_id):
	"""
	Gives up on one host's connection: its clients are closed and its manager
	forgotten, so the next use of it reconnects.

	Always both, and always only this host. The pair used to be written out at
	every call site with the manager half clearing the whole fleet, so one
	machine failing made every other one reconnect — and reconnecting to an ssh
	host is neither instant nor free. Closing the client empties its pool, so
	nothing reuses a connection that went bad; a request already waiting on one
	is bounded by its own timeout, over ssh too.
	"""
	host_registry.drop(host_id)
	forget_manager(host_id)


def local_manager():
	"""
	The local host's manager, asked for each time.

	A manager built once at import kept the client it started with. Once that
	client was dropped —the local daemon restarting is enough— it went on
	using a closed one, and finding the bot's updater or the architecture
	failed until the bot was recreated.
	"""
	return manager(host_registry.local_host_id())

# Instantiate the PortManager
port_manager = PortManager(local_manager)

_own_container = None
_own_container_lock = threading.Lock()


def own_container():
	"""
	Which container the bot is, as (host_id, container_id, name), or None.

	Resolved once and remembered: a process cannot move to another container
	halfway through its life.

	`own_container.candidates()` reads the ids out of /proc — see that module
	for why mountinfo and not the hostname — and this is where they get
	checked, because only here is there a daemon to ask. A candidate that does
	not resolve is discarded, so a path that merely looked like a container id
	cannot be mistaken for one.

	Always against the **local** daemon: the bot's container exists on the
	machine the bot runs on and nowhere else.

	None means it could not tell. That happens outside a container, and could
	happen on a runtime that lays out /proc differently. Callers fall back to
	CONTAINER_NAME when there is one, and lose the safeguards when there is
	not — which is what start-up warns about.
	"""
	global _own_container
	with _own_container_lock:
		if _own_container is not None:
			return _own_container or None

		local = host_registry.local_host_id()
		unanswered = False
		for candidate in own_container_ids():
			try:
				me = manager(local).client.containers.get(candidate)
			except docker.errors.NotFound:
				debug(f"Candidate {candidate[:12]} is not a container on this daemon")
				continue
			except Exception as e:
				# Not an answer about the candidate: the daemon could not be
				# asked. Remembering this as "not a container" would leave the
				# bot unable to recognise itself for the life of the process —
				# and free to update itself the ordinary way, deleting itself
				# halfway through.
				debug(f"Could not ask the local daemon about {candidate[:12]}: {e}")
				unanswered = True
				continue
			identifier = getattr(me, "id", None)
			name = getattr(me, "name", None)
			# What came back has to be the container that was asked about, and
			# has to look like one. Neither check is paranoia: a daemon that
			# answers something else, or a stand-in that answers everything,
			# would otherwise be believed — and being believed here means every
			# container is treated as the bot and none of them can be managed.
			if not isinstance(identifier, str) or not isinstance(name, str):
				debug(f"Candidate {candidate[:12]} came back as something other than a container")
				continue
			if not identifier.startswith(candidate[:12]):
				debug(f"Candidate {candidate[:12]} resolved to {identifier[:12]}, which is not it")
				continue
			_own_container = (local, identifier, name)
			debug(f"Identified myself as {name} ({identifier[:12]}) on the local host")
			return _own_container

		if unanswered:
			# Asked again next time, when the daemon may be back.
			return None
		# Nothing resolved, and the daemon said so. Remember the failure so the
		# /proc read and the daemon calls are not repeated on every press.
		_own_container = ()
		return None


def forget_own_container():
	"""Drops the resolved identity. For tests."""
	global _own_container
	with _own_container_lock:
		_own_container = None


def is_own_container(host_id=None, container_id=None, container_name=None):
	"""
	Whether the container being acted on is the bot itself.

	By id **and** host. Matching on the name alone is what let a container
	called the same on another machine be mistaken for the bot: on the update
	path that meant pressing update on a remote namesake ran the self-updater
	against the local one.

	`container_id` may be a short id — the references the buttons carry are
	five characters — so it is compared as a prefix, which is what the Docker
	API accepts too.
	"""
	own = own_container()
	if own is None:
		# Could not identify ourselves, so fall back to the name, and only on
		# the local host: without an id, the name is all there is, and a
		# namesake elsewhere must still not be taken for us.
		if not CONTAINER_NAME or container_name != CONTAINER_NAME:
			return False
		return host_id is None or host_id == host_registry.local_host_id()

	own_host, own_id, own_name = own
	if host_id is not None and host_id != own_host:
		return False
	if isinstance(container_id, str) and container_id:
		return own_id.startswith(container_id) or container_id.startswith(own_id)
	# No id to compare: the name, on our own host, is the best that is left.
	return container_name == own_name


def bot_shares_namespace_with(owner, container):
	"""
	Whether the bot lives inside this container's network, IPC, PID or UTS
	namespace — the bot behind gluetun, `network_mode: service:vpn`.

	Updating that container means recreating the bot to follow it into the new
	namespace, and the bot cannot recreate itself halfway through a job: it
	would stop, and nothing would start it again. Left alone instead, it would
	keep running inside a namespace that is gone, without a network to reach
	Telegram by.
	"""
	if owner.host_id != host_registry.local_host_id():
		return False
	try:
		own = own_container()
		bot = owner.client.containers.get(own[1] if own else own_container_name())
	except Exception:
		return False
	refs = {f"container:{container.id}", f"container:{container.id[:12]}"}
	host_config = bot.attrs.get("HostConfig") or {}
	return any((host_config.get(field) or "") in refs for field, _ in _NAMESPACE_HOSTCONFIG_FIELDS)


def own_container_name():
	"""The bot's own container name, however it was found out."""
	own = own_container()
	return own[2] if own else CONTAINER_NAME


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


def host_suffix(host_id):
	"""
	How a sentence says which machine it is about, or nothing with one host.

	The tail form of host_label, and the one to reach for by default: "nginx
	updated on casa" is how a person says it, while "casa · nginx updated"
	is how a log line does.

	It is not always the right shape, though, so the rule is:

	- A result that reads as **one statement** takes the suffix. That is most
	  of what the user keeps in the chat.
	- A message that ends in a **second sentence** ("...check the bot's logs"),
	  a **question**, or a **code block** keeps the prefix: a clause tacked
	  after any of those reads wrong in every language.
	- A **destructive confirmation** keeps the prefix on purpose, so the
	  machine is the first thing read rather than the last.
	- **Progress messages** keep the prefix too. They end in "..", so a suffix
	  would land after the dots — and they are deleted seconds later anyway.
	"""
	if host_registry.is_single_host():
		return ""
	return get_text("on_host", host_alias(host_id))


# Containers whose start/stop events are not announced, because an update
# batch is recreating them and will report the outcome in one summary. Keyed by
# (host_id, name); the value is when the hold expires, or None while the update
# is still running.
#
# The expiry exists because the event stream lags behind the API calls: the
# last "started" of a container can arrive a moment after its update returned,
# and releasing the name right then would let exactly that one through.
_held_events = {}
_held_events_lock = threading.Lock()
HELD_EVENTS_GRACE_SECONDS = 30


def hold_container_events(host_id, names):
	"""Stops announcing these containers' events until they are released."""
	with _held_events_lock:
		for name in names:
			_held_events[(host_id, name)] = None


def release_container_events(host_id, names):
	"""Lets these containers' events through again, after a grace period."""
	expiry = time.time() + HELD_EVENTS_GRACE_SECONDS
	with _held_events_lock:
		for name in names:
			if (host_id, name) in _held_events:
				_held_events[(host_id, name)] = expiry


def container_events_held(host_id, name):
	"""Whether an event for this container is part of an update batch."""
	with _held_events_lock:
		key = (host_id, name)
		if key not in _held_events:
			return False
		expiry = _held_events[key]
		if expiry is not None and expiry <= time.time():
			del _held_events[key]
			return False
		return True


class RestartLoopTracker:
	"""
	Turns a container stuck restarting into two messages instead of endless.

	A container with `restart: always` and an image that dies on start goes
	down and up again forever, and each of those is an event the monitor would
	announce. So after THRESHOLD stops within WINDOW_SECONDS it says once that
	the container is in a restart loop, and keeps quiet about it from then on.

	The loop ends one of two ways. The container stays up for STABLE_SECONDS,
	and that is announced as it stabilising. Or it stops and does not come
	back: Docker never waits more than a minute between restarts, so no start
	within GONE_SECONDS of a stop means the loop is over with it down — the
	user stopped it, or an `on-failure:N` gave up — and that is announced as
	the plain stop it is.

	One per host, like the monitor that owns it: names are only unique within
	a daemon.
	"""

	THRESHOLD = 3
	WINDOW_SECONDS = 300
	STABLE_SECONDS = 300
	GONE_SECONDS = 120
	# How long after a kill a stop counts as asked for. Docker sends "kill"
	# when something stops or restarts a container on purpose —`docker stop`,
	# a restart from the bot, a scheduled one— and never when it falls over by
	# itself, which is the only kind of stop a restart loop is made of.
	KILL_SECONDS = 60

	def __init__(self, notify, clock=time.time, schedule=None):
		# notify(key, name) announces one of the tracker's own messages.
		self._notify = notify
		self._clock = clock
		self._schedule = schedule or self._start_timer
		self._lock = threading.Lock()
		self._stops = {}    # name -> recent stop times, oldest first
		self._looping = {}  # name -> the timer waiting on it
		self._killed = {}   # name -> when it was last killed on purpose

	@staticmethod
	def _start_timer(delay, fn):
		timer = threading.Timer(delay, fn)
		timer.daemon = True
		timer.start()
		return timer

	def should_announce(self, action, name):
		"""
		Whether a start or stop event is announced as usual.

		False for the ones a restart loop is standing in for, including the
		stop that reveals the loop: that one is announced as the loop itself.
		"""
		with self._lock:
			now = self._clock()
			if action == "kill":
				self._killed[name] = now
				return True
			if action == "die":
				if name in self._looping:
					self._wait(name, self.GONE_SECONDS, self._gone)
					return False
				killed = self._killed.pop(name, None)
				if killed is not None and now - killed <= self.KILL_SECONDS:
					# Stopped or restarted on purpose. Three manual restarts in
					# five minutes, or a task restarting it every minute, were
					# reported as a loop and silenced its notices from then on.
					return True
				self._forget_quiet(now)
				stops = [t for t in self._stops.get(name, []) if t > now - self.WINDOW_SECONDS]
				stops.append(now)
				self._stops[name] = stops
				if len(stops) < self.THRESHOLD:
					return True
				self._wait(name, self.GONE_SECONDS, self._gone)
			elif action == "start" and name in self._looping:
				self._wait(name, self.STABLE_SECONDS, self._stable)
				return False
			else:
				return True
		self._notify("restart_loop", name)
		return False

	def stopped_on_purpose(self, name):
		"""
		Whether a stop of this container was asked for: it was killed — by
		`docker stop`, the bot, a task — moments ago. Looked at, not consumed:
		should_announce still needs it.
		"""
		with self._lock:
			killed = self._killed.get(name)
			return killed is not None and self._clock() - killed <= self.KILL_SECONDS

	def _forget_quiet(self, now):
		"""
		Drops what is known about containers that have been quiet for a while.

		Called with the lock held. Otherwise every name that ever stopped, or
		was ever killed, stayed for the life of the process.
		"""
		for name in [n for n, stops in self._stops.items()
						if not stops or stops[-1] <= now - self.WINDOW_SECONDS]:
			del self._stops[name]
		for name in [n for n, when in self._killed.items() if when <= now - self.KILL_SECONDS]:
			del self._killed[name]

	def _wait(self, name, delay, then):
		"""Replaces whatever the loop was waiting on. Called with the lock held."""
		previous = self._looping.get(name)
		if previous is not None:
			previous.cancel()
		timer = None

		def fire():
			with self._lock:
				# A later event may have replaced this wait after it fired.
				if self._looping.get(name) is not timer:
					return
				del self._looping[name]
				self._stops.pop(name, None)
			then(name)

		timer = self._schedule(delay, fire)
		self._looping[name] = timer

	def _stable(self, name):
		self._notify("restart_loop_stable", name)

	def _gone(self, name):
		self._notify("stopped_container", name)


def _stream_reader(stream):
	"""The file object a docker-py stream reads from, or None."""
	try:
		return stream._response.raw._fp.fp
	except AttributeError:
		return None


def _keep_alive(stream):
	"""
	Turns on TCP keepalive for an event stream's socket, when it has one.

	An event stream only ever receives. A host that reboots in less time than
	the supervisor takes to look again answers its next ping as if nothing had
	happened, while the old connection is dead on its side with nothing ever
	going to arrive on it. With keepalive the kernel asks, gets a reset, and
	the read fails: the loop reconnects within a minute or so.

	Best effort: a unix socket ignores it, ssh has its own (the image sets
	ServerAliveInterval), and the per-probe timings only exist on Linux.
	"""
	import socket as _socket
	reader = _stream_reader(stream)
	raw = getattr(reader, "raw", None)
	sock = getattr(raw, "_sock", None) or getattr(raw, "sock", None)
	if sock is None or getattr(sock, "family", None) not in (_socket.AF_INET, _socket.AF_INET6):
		return
	try:
		sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
		for option, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
			if hasattr(_socket, option):
				sock.setsockopt(_socket.IPPROTO_TCP, getattr(_socket, option), value)
	except OSError as e:
		debug(f"Could not turn on keepalive for an event stream: {e}")


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

	# How far back a reconnection asks the daemon to replay what it missed.
	# Enough for a dropped connection; a host that was gone for longer comes
	# back with a burst of stale "started" and "stopped" nobody wants.
	MAX_REPLAY_SECONDS = 600

	def __init__(self, host_id):
		self.host_id = host_id
		# The URL its events come from, for telling when the host moved.
		self._url = (host_registry.host(host_id) or {}).get("url")
		self._stop = threading.Event()
		# Set to cut the wait before the next reconnection short.
		self._retry_now = threading.Event()
		self._stream = None
		self._stream_lock = threading.Lock()
		# The newest event seen, in nanoseconds, so a reconnection can ask
		# for what came after it and skip what it had already reported.
		self._last_event_ns = None
		# The newest event the previous connection reported: what a replay
		# up to it would repeat.
		self._replay_until = None
		self.thread = None
		self.restart_loops = RestartLoopTracker(
			lambda key, name: self._announce(get_text(key, name)))
		# Names whose healthcheck is failing, announced once each: the next
		# news about them is that they are healthy again. And when each was
		# last killed for running out of memory, which arrives as an event of
		# its own just before the stop it causes. Both forgotten when the
		# container is removed.
		self._unhealthy = set()
		self._oom = {}

	@property
	def alias(self):
		return host_registry.alias(self.host_id)

	@property
	def listening(self):
		"""Whether a stream is open right now."""
		with self._stream_lock:
			return self._stream is not None

	def stop(self):
		"""
		Asks the monitor to finish, closing the stream it is blocked on, and
		the host's client with it: a host that is paused or removed has no use
		for a connection left open, and over ssh that is a process.
		"""
		self._stop.set()
		self._retry_now.set()
		self.reset_stream()
		host_registry.drop(self.host_id)

	def retry_now(self):
		"""
		Reconnects without waiting out the backoff, because the host answers
		again. After a long outage the wait is five minutes, and the host's
		notifications would arrive that much after the news that it is back.
		"""
		self._retry_now.set()

	def reset_stream(self):
		"""
		Closes the open stream, so the loop reconnects.

		Closing the stream itself and not the client: closing a client only
		empties its pool of idle connections, and the one the stream reads
		from is never idle. A host that rebooted or lost power without closing
		the connection left the monitor reading a socket nothing would ever
		write to again, silent until the bot was restarted.
		"""
		with self._stream_lock:
			stream, self._stream = self._stream, None
		if stream is not None:
			try:
				stream.close()
			except Exception as e:
				# docker-py cannot cancel a stream over ssh: close() fails
				# looking for a socket the ssh transport does not have. The
				# transport leaves itself on the pipe it reads from, and
				# closing it ends the ssh process, which ends the read.
				channel = getattr(_stream_reader(stream), "channel", None)
				if channel is None:
					debug(f"Event monitor ({self.alias}): closing the stream: {e}")
					return
				try:
					channel.close()
				except Exception as ssh_error:
					debug(f"Event monitor ({self.alias}): closing the ssh stream: {ssh_error}")

	def detectar_eventos_contenedores(self):
		url = (host_registry.host(self.host_id) or {}).get("url")
		if url != self._url:
			# Its host was moved to another URL, and the supervisor has not yet
			# put a monitor of its own there. Asking the new daemon for what
			# came after the last event of the old one announced its last ten
			# minutes as news: it starts from now instead.
			self._url = url
			self._last_event_ns = None
		client = host_registry.client(self.host_id)
		since = None
		if self._last_event_ns is not None:
			since = max(self._last_event_ns // 1_000_000_000, int(time.time()) - self.MAX_REPLAY_SECONDS)
		stream = client.events(decode=True, since=since) if since else client.events(decode=True)
		# Only what is replayed can be a repeat: what the last connection had
		# already reported. Comparing every live event against the newest one
		# seen dropped real ones, since Docker does not deliver events strictly
		# in time order —a compose project starting its services in parallel
		# is enough— nor does a clock that NTP sets back.
		self._replay_until = self._last_event_ns
		_keep_alive(stream)
		with self._stream_lock:
			if self._stop.is_set():
				stream.close()
				return
			self._stream = stream
		try:
			for event in stream:
				if self._stop.is_set():
					return
				self._handle_event(event)
		finally:
			with self._stream_lock:
				if self._stream is stream:
					self._stream = None

	def _handle_event(self, event):
		"""Reports one event from the stream, when it is one worth reporting."""
		stamp = event.get("timeNano") or (event.get("time") or 0) * 1_000_000_000
		if stamp:
			if self._replay_until is not None and stamp <= self._replay_until:
				# Replayed after a reconnection, and already reported.
				return
			self._last_event_ns = max(self._last_event_ns or 0, stamp)

		# Only process container events
		event_type = event.get('Type', '')
		if event_type != 'container':
			return

		# Support both 'Action' (Docker Desktop/newer) and 'status' (Docker Engine/older) formats
		action = event.get('Action', '') or event.get('status', '')
		actor = event.get('Actor', {})
		attributes = actor.get('Attributes', {})
		container_name = attributes.get('name', '')

		container_id = actor.get('ID') or event.get('id')

		if action == "kill":
			# Never announced, but it is what tells a stop that was asked for
			# from one that was not.
			self.restart_loops.should_announce(action, container_name)
			return
		if action == "oom":
			self._oom[container_name] = time.time()
			return
		if action == "destroy":
			self._unhealthy.discard(container_name)
			self._oom.pop(container_name, None)
			return

		message = None
		detail = None
		if action == "start":
			message = get_text("started_container", container_name)
		elif action == "die":
			message, detail = self._death_message(container_name, attributes, container_id)
		elif action == "create" and store.get("bot.extended_messages"):
			message = get_text("created_container", container_name)
		elif action.startswith("health_status"):
			status = action.split(":", 1)[1].strip() if ":" in action else attributes.get("health_status", "")
			message, detail = self._health_message(container_name, status, container_id)

		if not message:
			return

		if container_events_held(self.host_id, container_name):
			debug(f"Message [{message}] omitted because an update batch will summarise it")
			return

		if action in ("start", "die") and not self.restart_loops.should_announce(action, container_name):
			debug(f"Message [{message}] omitted because {container_name} is in a restart loop")
			return

		if detail:
			self._announce(message, detail)
		else:
			self._announce(message)

	# How long after an "oom" event the stop that follows is put down to it.
	OOM_SECONDS = 30

	def _death_message(self, name, attributes, container_id):
		"""
		What to say about a container that stopped, and the log lines to show.

		"Stopped" said nothing about why. Asked for —`docker stop`, the bot, a
		task— is still that. Otherwise the exit code says it: 0 is a process
		that finished, anything else one that failed, shown with what it last
		wrote. Out of memory is told apart, because the fix is a different one.
		"""
		oom = self._oom.pop(name, None)
		if oom is not None and time.time() - oom <= self.OOM_SECONDS:
			return get_text("container_oom", name), None
		if self.restart_loops.stopped_on_purpose(name):
			return get_text("stopped_container", name), None
		code = str(attributes.get('exitCode', '')).strip()
		if code in ("", "0"):
			return get_text("container_finished" if code == "0" else "stopped_container", name), None
		return get_text("container_failed", name, describe_exit_code(code)), self._last_logs(container_id)

	def _health_message(self, name, status, container_id):
		"""
		What to say when a healthcheck changes, or (None, None).

		Docker reports every change, and only changes: "healthy" on every
		start is not news, and is announced only when it ends a failure that
		was. A failure is announced once, with what the check last said.
		"""
		if status == "unhealthy":
			if name in self._unhealthy:
				return None, None
			self._unhealthy.add(name)
			return get_text("container_unhealthy", name), self._last_health_output(container_id)
		if status == "healthy" and name in self._unhealthy:
			self._unhealthy.discard(name)
			return get_text("container_healthy_again", name), None
		return None, None

	def _inspect(self, container_id):
		try:
			return host_registry.client(self.host_id).containers.get(container_id)
		except Exception as e:
			debug(f"Event monitor ({self.alias}): could not read {container_id}: {e}")
			return None

	def _last_logs(self, container_id):
		"""The last lines a container wrote, ready for the message, or None."""
		container = self._inspect(container_id) if container_id else None
		if container is None:
			return None
		try:
			raw = container.logs(tail=EVENT_LOG_LINES, stdout=True, stderr=True)
		except Exception as e:
			debug(f"Event monitor ({self.alias}): could not read the logs of {container_id}: {e}")
			return None
		return log_excerpt(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw))

	def _last_health_output(self, container_id):
		"""What the failing healthcheck last printed, when it printed anything."""
		container = self._inspect(container_id) if container_id else None
		if container is None:
			return None
		entries = ((container.attrs.get('State') or {}).get('Health') or {}).get('Log') or []
		output = entries[-1].get('Output') if entries and isinstance(entries[-1], dict) else None
		return log_excerpt(output) if output and output.strip() else None

	def _announce(self, message, detail=None):
		"""
		Sends one of this host's container notifications. False when it failed.

		Every key that reaches here is a single statement, so the host goes at
		the end of it — before the detail, a block of log lines, when there is
		one. And this is where most of these arrive from, which is what stays
		in the chat.
		"""
		if self._stop.is_set():
			return True
		message = f"{message}{host_suffix(self.host_id)}"
		if detail:
			message = f"{message}\n\n{detail}"
		try:
			if is_muted():
				debug(f"Message [{message}] omitted because muted")
				return True
			send_message_to_notification_channel(message=message)
			return True
		except Exception as e:
			error(f"Could not send notification [{message}]. Error: [{e}]")
			return False

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
			self._retry_now.wait(backoff)
			self._retry_now.clear()
			if self._stop.is_set():
				break
		debug(f"Event monitor ({self.alias}) stopped")

	def demonio_event(self):
		"""Start event daemon in a background thread."""
		self.thread = threading.Thread(target=self._event_loop_with_retry, daemon=True)
		self.thread.start()
		debug(f"Event monitor daemon started for {self.alias}")
		return self.thread


class HostAvailability:
	"""
	Says when a remote host stops answering, and when it answers again.

	Without it a machine that went down took its notifications with it and
	nothing said so: its event stream only wrote to the log, and the user found
	out the next time they tried to do something there.

	Remote hosts only. The one behind the local socket is the daemon the bot
	itself runs on: without Docker's live-restore, a daemon that stops takes
	the bot down with it and there is nobody left to say so, and with it, a
	restart is over in seconds.

	Fed by the event monitor supervisor, which already asks every host whether
	it answers every RECONCILE_SECONDS.
	"""

	# How long a host has to go without answering before it is announced. A
	# Docker restart or a blip in the network is over well before this.
	DOWN_AFTER_SECONDS = 120

	def __init__(self):
		# {host_id: (since, reason)} for every watched host failing right now.
		self._failing = {}
		# {host_id: since} for the ones announced as down.
		self._announced = {}
		# {host_id: seconds it was down} for recoveries that came while muted.
		# Said once the mute ends: the user heard it go down, and silence
		# afterwards would read as still down.
		self._pending_back = {}

	@staticmethod
	def watched(entry):
		"""Whether a host's outages are announced: remote and not paused."""
		url = entry.get("url") or host_registry.LOCAL_SOCKET_URL
		return not entry.get("paused") and not url.startswith("unix://")

	def update(self, statuses, now=None):
		"""
		Takes one round of answers, as {host_id: (ok, reason)} for the watched
		hosts, and announces what changed.

		A host missing from the round —removed, paused— is forgotten without
		a word: whoever did that knows.
		"""
		now = time.time() if now is None else now
		for book in (self._failing, self._announced, self._pending_back):
			for host_id in [h for h in book if h not in statuses]:
				book.pop(host_id)
		muted = is_muted()
		for host_id, (ok, reason) in statuses.items():
			if ok:
				self._failing.pop(host_id, None)
				since = self._announced.pop(host_id, None)
				if since is not None:
					self._pending_back[host_id] = now - since
				continue
			# Down again before its recovery could be told: that is old news.
			self._pending_back.pop(host_id, None)
			since = self._failing.get(host_id, (now, ""))[0]
			self._failing[host_id] = (since, reason)
			if host_id in self._announced or now - since < self.DOWN_AFTER_SECONDS or muted:
				continue
			self._announced[host_id] = since
			debug(f"Host {host_registry.alias(host_id)} has not answered for {int(now - since)} s ({reason}): announcing it")
			self._send(get_text("host_down", host_alias(host_id),
								human_duration(now - since), self._reason(reason)))
		if muted:
			return
		for host_id, downtime in list(self._pending_back.items()):
			debug(f"Host {host_registry.alias(host_id)} answers again after {int(downtime)} s: announcing it")
			self._send(get_text("host_back", host_alias(host_id), human_duration(downtime)))
		self._pending_back.clear()

	@staticmethod
	def _reason(reason):
		"""Why it failed, as text: in the user's language when the words are the bot's own."""
		if isinstance(reason, host_registry.NoAnswer):
			return get_text("host_no_answer", reason.seconds)
		return html.escape(str(reason or "?"))

	def _send(self, message):
		try:
			send_message_to_notification_channel(message=message)
		except Exception as e:
			error(f"Could not send notification [{message}]. Error: [{e}]")


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
		# The URL each monitor was started on. A host keeps its id when its URL
		# changes, and its stream would otherwise stay on the old daemon.
		self._urls = {}
		self._lock = threading.Lock()
		self.availability = HostAvailability()

	def reconcile(self):
		"""
		Starts monitors for new hosts, stops them for removed ones, and brings
		back any that died or went deaf. A host that now has another URL counts
		as removed and added: its stream is started again, on the new one.
		"""
		configured = {entry["id"]: entry.get("url") for entry in host_registry.hosts()}
		with self._lock:
			for host_id in [h for h in self._monitors if h in configured and self._urls.get(h) != configured[h]]:
				debug(f"Host {host_id} has a new URL: starting its event monitor again on it")
				self._monitors.pop(host_id).stop()
			for host_id in set(configured) - set(self._monitors):
				monitor = DockerEventMonitor(host_id)
				self._monitors[host_id] = monitor
				self._urls[host_id] = configured[host_id]
				monitor.demonio_event()
			for host_id in set(self._monitors) - set(configured):
				debug(f"Host {host_id} is gone: stopping its event monitor")
				self._monitors.pop(host_id).stop()
				self._urls.pop(host_id, None)
			for host_id, monitor in list(self._monitors.items()):
				if monitor.thread is not None and not monitor.thread.is_alive():
					# Its loop never gives up, so a dead thread means something
					# escaped it; a new monitor carries on where it left off.
					warning(f"Event monitor ({monitor.alias}) had stopped: starting it again")
					replacement = DockerEventMonitor(host_id)
					replacement._last_event_ns = monitor._last_event_ns
					self._monitors[host_id] = replacement
					replacement.demonio_event()
			monitors = list(self._monitors.values())
		self._check_hosts(monitors)

	def _check_hosts(self, monitors):
		"""
		Asks every host whether it answers, and acts on it.

		A stream over tcp that the other end dropped without closing —a reboot,
		a power cut— looks exactly like a quiet one: nothing arrives either
		way. Asking the daemon tells them apart, and the stream is reconnected.
		ssh needs this less, its own keepalive ends the connection, but asking
		costs the same.

		A monitor waiting to reconnect to a host that answers again stops
		waiting. And the answers go to the availability, which says when a
		remote host goes down and comes back.
		"""
		if not monitors:
			self.availability.update({})
			return
		entries = {m.host_id: host_registry.host(m.host_id) for m in monitors}
		statuses = host_registry.status_snapshot(entries=[e for e in entries.values() if e])
		for monitor in monitors:
			ok, reason = statuses.get(monitor.host_id, (True, ""))
			listening = monitor.listening
			if listening and not ok:
				debug(f"Event monitor ({monitor.alias}): host not answering ({reason}), reconnecting")
				monitor.reset_stream()
			elif ok and not listening:
				monitor.retry_now()
		self.availability.update({host_id: statuses[host_id] for host_id, entry in entries.items()
									if entry and host_id in statuses and HostAvailability.watched(entry)})

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


def update_interval_hours():
	"""
	The interval between update checks, falling back to 4 h when what is stored
	is not a usable number — a hand edit, or a "nan" typed before 5.0.0_RC7.
	"""
	try:
		return as_interval_hours(store.get("bot.check_update_every_hours"))
	except (TypeError, ValueError):
		return 4.0

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
		interval_hours = update_interval_hours()
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
		# What the pass found to update on its own, as (host_id, reference,
		# name, label): updated together once every host has been checked.
		# `label` is the DCB-* label that asked for it, or None when it was the
		# setting chosen from the bot. On the instance and emptied by every
		# pass: a list on the class would be one list for every monitor.
		self._auto_targets = []
		# How many containers the pass reached a decision on —checked, or left
		# out on purpose: stopped, ignored— and how many it failed to check.
		# A pass is the first done when it decided and nothing failed: with a
		# pull failing at boot, an ignored container alone does not make the
		# next pass announce every update there was before the install.
		self._decided = 0
		self._failed = 0

	def detectar_actualizaciones(self):
		while True:
			if not store.get("bot.check_updates"):
				# The daemon runs even with checks disabled so that turning them
				# back on from /settings takes effect without a restart.
				time.sleep(UPDATE_CHECK_POLL_SECONDS)
				continue

			# The first pass on this install fills the cache quietly: anything
			# it finds is pre-existing rather than new, and announcing every
			# pending update at once is what used to happen after every
			# container recreation.
			# One bad pass must not end the checks for the life of the process:
			# anything that raises outside the per-host guard —listing the
			# hosts, sending the message, saving its state on a full disk—
			# used to kill this thread with nothing said, and updates were
			# never checked again until a restart.
			try:
				cold_cache = not store.update_check_done()
				if cold_cache:
					debug("Update cache is empty: this pass will fill it without notifying")
				self._check_fleet(cold_cache)
				# Only once it has decided on something. Waiting for the cache
				# to have an entry left an install whose containers were all
				# ignored, or stopped, cold for good: the first update found
				# after one came back was saved as announced without a word.
				if store.has_update_cache() or (self._decided and not self._failed):
					store.mark_update_check_done()
			except Exception as e:
				error(f"Update check pass failed, retrying at the next interval: [{e}]")
			wait_for_next_update_check()

	def _check_fleet(self, cold_cache):
		"""
		One pass over every host, ending in a single message for what it found.

		Kept out of the loop so a pass can be run on its own in a test: all the
		loop adds is the waiting, and the waiting is measured in hours.
		"""
		count_usage("auto_update_check")
		# Every reachable host, in the order they are configured. One being
		# down degrades its own check and nothing else.
		all_updates = []  # list of [reference, name] pairs, across every host
		anything_new = False
		self._auto_targets = []
		self._decided = 0
		self._failed = 0
		listed = hosts_with_containers()
		for entry, owner, containers in listed:
			try:
				# The cache written once for the host, not once per container.
				with store.batch("updates"):
					found, has_new = self._check_host(entry, owner, cold_cache, containers)
				all_updates.extend(found)
				anything_new = anything_new or has_new
			except Exception as e:
				self._failed += 1
				error(f"Update check failed on {entry.get('alias', entry['id'])}: [{e}]")
		try:
			forget_gone_container_settings(listed)
		except Exception as e:
			error(f"Could not tidy the per-container settings: [{e}]")

		# What updates on its own goes first, then the message with buttons for
		# the rest, so that one is left at the bottom of the chat and nothing
		# it invites runs alongside the batch. The bot goes last of all and on
		# its own: from there it is replaced by the new one, and anything after
		# it would never happen. Each step in a finally, because the one before
		# failing —a full disk saving the message— must not cost the next.
		own = [t for t in self._auto_targets if is_own_container(t[0], ref_id(t[1]), t[2])]
		others = [t for t in self._auto_targets if t not in own]
		try:
			self._auto_update(others)
		finally:
			try:
				self._announce(all_updates, anything_new)
			finally:
				self._auto_update(own)

	def _announce(self, all_updates, anything_new):
		"""The single fleet-wide message for what a pass found."""

		# One message for the whole fleet rather than one per host: with four
		# hosts that was four taps to update everything, and the keyboard
		# already names the host on each button when the list spans machines.
		# The bot's own update keeps its dedicated message — updating it
		# recreates the bot mid-batch, which would leave the rest undone.
		#
		# A host only reports something new on a warm cache, so anything_new
		# carries the "the first pass announces nothing" rule with it.
		if not (all_updates and anything_new):
			return
		if is_muted():
			debug(f"Message [{get_text('available_updates', len(all_updates))}] omitted because muted")
			return
		# The same builder the toggles repaint with: a keyboard assembled by
		# hand here drifts from the one that replaces it on the first tap, and
		# both have to agree on what a button says.
		markup = build_generic_keyboard(all_updates, set(), None, "Update",
										get_text("button_update"), get_text("button_update_all"))
		message = send_message(message=available_updates_text(all_updates), reply_markup=markup)
		if message:
			save_update_data(message.chat.id, message.message_id, all_updates)
			# The name cache the callback parser reads, written from the
			# references themselves: they already carry host and name, so
			# nothing has to be asked of each daemon a second time.
			save_container_refs(message.chat.id, message.message_id, all_updates)

	def _auto_update(self, targets):
		"""
		Updates what the pass found to update on its own, in a single batch.

		The batch /updateall runs when they are ticked by hand: one progress
		message and one summary for the whole pass, across hosts. Updated one
		by one as they were found, each was a batch of its own, and four
		containers on one machine left four messages.

		What was updated from somewhere else in the meantime, or removed, is
		left out without a word: the pass can take minutes, and a summary
		saying "already up to date" about something nobody asked for is noise.
		"""
		live = []
		for host_id, reference, name, label in targets:
			owner, container = find_live_container(reference, name)
			if container is None:
				debug(f"Auto-update of {name} skipped: it is gone since the check")
				continue
			# Asked again: a tap from "tell me" to "do not check" goes through
			# "update on its own", and the check may have passed by just then.
			if update_mode(owner.host_id, container.name, container.labels)[0] != "auto":
				debug(f"Auto-update of {name} skipped: it is no longer set to update on its own")
				continue
			reference = container_ref(owner.host_id, container)
			if runs_latest_image(reference, container.name):
				debug(f"Auto-update of {name} skipped: it already runs the newest image")
				continue
			debug(f"Auto-updating container {name}")
			count_usage("auto_label_update" if label else "auto_bot_update")
			live.append((owner.host_id, reference, container.name))
		if not live:
			return
		if store.get("bot.extended_messages"):
			# Step by step, as extended messages are asked for: each one said
			# right before its own update, not all of them up front.
			for host_id, reference, name in live:
				auto_update_send(f'{host_label(host_id)}{get_text("auto_update", name)}')
				update_containers([(reference, name)], send=auto_update_send, report_up_to_date=False)
			return
		update_containers([(reference, name) for _, reference, name in live],
						  send=auto_update_send, report_up_to_date=False)

	def _check_host(self, entry, owner, cold_cache, containers):
		"""
		Checks one host for updates, returning ([reference, name], has_new).

		What it finds feeds the single fleet-wide message _check_fleet sends,
		and what is to update on its own goes to self._auto_targets for the
		batch at the end of the pass. This sends nothing itself except the
		bot's own "update available" — kept apart on purpose, since updating it
		recreates the bot mid-batch.

		`containers` is what the sweep already listed for this host. Asking the
		daemon again here would be a second round trip for one answer, and
		sorting it again without naming the host would put another machine's
		container ahead of the bot's own whenever the two share a name.

		Hosts are checked one at a time, in sequence, rather than a thread each
		— unlike the listing that feeds this, which is parallel because nothing
		is downloaded to list. Here every host would be pulling images, and
		doing that at once saturates the same network and disk; with an
		interval measured in hours there is nothing to gain by it either.
		"""
		host_id = entry["id"]
		grouped_updates_containers = []  # list of [reference, name] pairs
		should_notify = False
		# One pull per image, not per container: five services on the same
		# image were five manifest requests, each one counted against Docker
		# Hub's anonymous rate limit. A failure is remembered too, so the rest
		# of that image's containers do not ask again in the same pass.
		pulled = {}
		for container in containers:
			if (container.status == "exited" or container.status == "dead") and not store.get("bot.check_update_stopped_containers"):
				debug(f"Ignoring update check for container {container.name} (stopped)")
				self._decided += 1
				continue

			mode, label = update_mode(host_id, container.name, container.labels)
			if mode == "ignore":
				debug(f"Ignoring update check for container {container.name} ({'label' if label else 'setting'})")
				self._decided += 1
				continue

			# Listed, not inspected: the platform the pull has to ask for is
			# only in the inspect (ImageManifestDescriptor), so it is fetched
			# here, after the containers skipped above that the list could
			# answer for.
			#
			# A container gone since the listing —removed, or recreated under a
			# new id from outside the bot— is skipped. Failing here, outside
			# the try below, threw away every update already found on the host,
			# and those were saved as announced without ever being announced.
			try:
				if getattr(container, "listed", False):
					container.reload()
				image_with_tag = container.attrs['Config']['Image']
			except docker.errors.NotFound:
				debug(f"Update check: {container.name} is gone since it was listed, skipped")
				continue
			versions = None
			try:
				local_image = container.image.id
				# Per image and platform: two containers on the same tag may run
				# different architectures of it.
				pull_key = (image_with_tag, container_platform(container))
				if pull_key not in pulled:
					try:
						pulled[pull_key] = pull_image(owner.client, image_with_tag, container)
					except Exception as pull_error:
						pulled[pull_key] = pull_error
				remote_image = pulled[pull_key]
				if isinstance(remote_image, Exception):
					raise remote_image
				debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}]")
				if local_image != remote_image.id and not same_running_manifest(owner.client, container, local_image, remote_image):
					if mode == "auto":
						# Not here: all together at the end of the pass, in one
						# batch. The bot among them, which updating would
						# recreate halfway through, with the hosts after this one
						# unchecked.
						debug(f"{container.name} update detected! Updated at the end of the pass")
						self._auto_targets.append((host_id, container_ref(host_id, container), container.name, label))
						self._decided += 1
						continue
					old_has_update = read_container_update_status(image_with_tag, container.name, host_id)
					has_update = True
					versions = (running_version(host_id, container), available_version(remote_image, image_with_tag))
					# Keep the pulled image cached locally so the subsequent update
					# operation does not need to re-download it.
					debug(f"{container.name} update detected! Keeping downloaded image [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}] for upcoming update")

					if not is_own_container(host_id, container.id, container.name):
						grouped_updates_containers.append([container_ref(host_id, container), container.name])

					if old_has_update is True:
						debug("Update already notified")
						# Not announced again, but a newer release than the one
						# announced is what the update will now bring, and what
						# every later message about it has to say.
						if store.update_versions(host_id, container.name) != versions:
							save_container_update_status(image_with_tag, container.name, has_update, host_id, versions)
						continue

					if is_own_container(host_id, container.id, container.name):
						markup = InlineKeyboardMarkup(row_width = 1)
						markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container_ref(host_id, container)}"))
						if not is_muted() and not cold_cache:
							sent_message = send_message(message=f'{get_text("available_update", container.name)}{host_suffix(host_id)}{version_lines(*versions)}', reply_markup=markup)
							# Save container cache for this notification
							if sent_message:
								save_container_cache(sent_message.chat.id, sent_message.message_id, [container], host_id)
						else:
							debug(f"Message [{get_text('available_update', container.name)}] omitted because muted")
						# Persist the "already notified" status so the bot is not spammed
						# every cycle. Other containers reach the equivalent save below
						# via the grouped-updates flow; the bot's self-update has its
						# own dedicated message and would otherwise skip it.
						save_container_update_status(image_with_tag, container.name, has_update, host_id, versions)
						continue

					should_notify = not cold_cache
				else: # Contenedor actualizado
					has_update = False
			except Exception as e:
				# What was known stays as it was. Forgetting it on a failed pull
				# —a Docker Hub rate limit, a registry down for the night— made
				# the next pass that worked take an update it had already
				# announced for a new one, and announce it again.
				# Named, because the error alone —a registry that does not
				# resolve, say— does not always say which container it was.
				error(f"Could not check update for {container.name} ({image_with_tag}) "
					  f"on {host_registry.alias(host_id)}: [{e}]")
				self._failed += 1
				continue
			save_container_update_status(image_with_tag, container.name, has_update, host_id, versions)
			self._decided += 1

		return grouped_updates_containers, should_notify


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

	Returns None only when the answer is that the container is not there, which
	disables the task. A host that cannot be asked raises HostUnavailable
	instead: an ssh machine rebooting during a nightly restart, or one still
	coming up when the @reboot tasks run, would otherwise burn the task for
	good over a minute of downtime.
	"""
	if host_registry.host(host_id) is None:
		# Removed from the bot: there is nowhere this task can ever run.
		return None
	owner = manager(host_id)
	try:
		container = owner.container_named(container_name)
	except Exception as e:
		raise host_registry.HostUnavailable(host_id, str(e))
	return container_ref(host_id, container) if container is not None else None


# What a scheduled prune can clean, and the name its output file goes by.
SCHEDULED_PRUNE_LABELS = {
	"containers": "button_containers",
	"images": "button_images",
	"networks": "button_networks",
	"volumes": "button_volumes",
}


class DockerScheduleMonitor:
	# How many minutes the loop makes up for when it falls behind —a slow
	# action, a busy machine— before it gives up on them. Past that the gap is
	# a suspended host or a clock that jumped, and replaying it would fire a
	# burst of tasks for times long gone.
	MAX_CATCH_UP_MINUTES = 5

	def __init__(self):
		super().__init__()
		self.schedule_manager = schedule_manager  # Use the global instance
		# The last minute whose tasks were evaluated. Each minute is looked at
		# once, which is what keeps a task from running twice in it.
		self._last_minute = None
		# Hosts whose tasks are being skipped because they do not answer, as
		# {host_id: True}. The user hears once when it starts and once when
		# it ends, not on every skipped run.
		self._down_hosts = {}
		# Scheduled prunes, run by a worker of their own in the order they came
		# due. Started on the first one: most installations never have any.
		self._prunes = queue.Queue()
		self._prune_lock = threading.Lock()
		self._prune_worker = None

	def _execute_reboot_tasks(self):
		"""Execute all @reboot tasks, once, when the daemon starts"""
		try:
			for schedule in self.schedule_manager.get_all_schedules():
				if schedule.get("enabled", True) and schedule.get("cron") == "@reboot":
					self._execute_schedule_action(schedule)
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
			# `or ""` y no el segundo argumento de get(): get devuelve el valor
			# guardado cuando la clave existe, aunque sea None, así que el
			# default no protege de una tarea con action a nulo.
			action = (schedule.get("action") or "").lower()
			container = schedule.get("container", "")
			# Tasks created before hosts existed carry none, and mean the local
			# machine: that is where they were set up to run.
			schedule_host = schedule.get("host") or host_registry.local_host_id()
			minutes = schedule.get("minutes")
			command = schedule.get("command", "")
			show_output = bool(schedule.get("show_output", False))
			prune_type = schedule.get("prune_type", "")
			schedule_name = schedule.get("name", "")

			if action in HOST_SCOPED_SCHEDULE_ACTIONS and host_registry.is_paused(schedule_host):
				# Skipped, and deliberately not through handle_error: that
				# disables the task for good. A paused machine cannot resolve
				# the container, so every run against it used to burn one more
				# schedule — which is the opposite of what pausing a host you
				# intend to bring back is for.
				debug(f"Schedule {schedule_name or action} skipped: host "
					f"{host_registry.alias(schedule_host)} is paused")
				return True

			if action in SCHEDULE_ACTIONS:
				count_usage(f"sched_{action}")

			# Helper function to handle errors consistently
			def handle_error(error_msg, notice="error_schedule_disabled"):
				error(error_msg)
				# Disable the schedule instead of deleting it
				if schedule_name:
					self.schedule_manager.update_schedule(schedule_name, enabled=False)
					send_message(message=get_text(notice, html.escape(schedule_name)))
				return False

			# A removed host is not coming back, unlike a paused or unreachable
			# one, so the task is disabled rather than skipped. A prune used to
			# reach manager() and be skipped as a host not answering, on every
			# firing and naming the bare id; a container task was disabled, but
			# with a notice that only said it had failed.
			if schedule_host_missing(schedule):
				return handle_error(f"Host {schedule_host} of schedule {schedule_name or action} no longer exists",
					notice="schedule_disabled_host_missing")
			# With settings.json unreadable the host list is the defaults, and
			# the local host an id made up in memory, so every task's host looks
			# gone. That is what schedule_host_missing answers then, so that
			# nothing is switched off for it; the task waits here instead.
			if (action.lower() in HOST_SCOPED_SCHEDULE_ACTIONS and store.settings_unreadable()
					and host_registry.host(schedule_host) is None):
				warning(f"Schedule {schedule_name or action} skipped: settings.json is unreadable")
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
					mute(minutes, extend_only=True)
				except (ValueError, TypeError):
					return handle_error(f"Invalid minutes value: {minutes}")

			elif action == "exec":
				containerId = schedule_container_ref(schedule_host, container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				# Not waited for: a long or endless command would hold up every
				# task after it. Its output, if asked for, arrives when it ends.
				execute_command(containerId, container, command, show_output, wait=False)

			elif action == "prune":
				if prune_type not in SCHEDULED_PRUNE_LABELS:
					return handle_error(f"Unknown prune type: {prune_type}")
				# Handed to its own worker: a prune can take minutes, and this
				# loop runs every task in line. One that outlasted the catch-up
				# window made the loop skip the minutes after it —logged as if
				# the bot had not been running— and their tasks with them.
				self._queue_prune(manager(schedule_host), prune_type, show_output)

			return True

		except host_registry.HostUnavailable as e:
			# Skipped for this run only, and said once per outage: a task that
			# fires every minute against a machine down for an hour would be
			# sixty messages, and saying nothing left a host down for days
			# with nobody knowing its tasks were not running.
			warning(f"Schedule {schedule.get('name') or action} skipped: {e}")
			if e.host_id not in self._down_hosts:
				self._down_hosts[e.host_id] = True
				self._notify(get_text("schedule_skipped_host_down",
										html.escape(schedule.get("name") or action), host_alias(e.host_id)))
			return False
		except Exception as e:
			error(f"Error executing schedule action [{action}]: [{str(e)}]")
			return False

	def _queue_prune(self, owner, prune_type, show_output):
		"""Leaves a scheduled prune to the prune worker, starting it if needed."""
		self._prunes.put((owner, prune_type, show_output))
		with self._prune_lock:
			if self._prune_worker is None or not self._prune_worker.is_alive():
				self._prune_worker = threading.Thread(target=self._prune_loop, daemon=True)
				self._prune_worker.start()

	def _prune_loop(self):
		"""
		Runs the queued prunes one after another.

		One at a time and in order, as the loop used to: a containers prune
		due with an images one frees images the second can then remove, and
		two at once on the same host only get Docker's "already running".
		"""
		while True:
			owner, prune_type, show_output = self._prunes.get()
			try:
				self._run_prune(owner, prune_type, show_output)
			except Exception as e:
				error(f"Error executing schedule action [prune]: [{str(e)}]")

	def _run_prune(self, owner, prune_type, show_output):
		result_message, data = getattr(owner, f"prune_{prune_type}")()
		# Show output if requested, otherwise just log
		if show_output and result_message and data is None:
			send_message(message=result_message)
		elif show_output and result_message:
			# Send the same format as manual /prune command
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
			fichero_temporal = get_temporal_file(data, get_text(SCHEDULED_PRUNE_LABELS[prune_type]))
			x = send_message(message=get_text("loading_file"))
			send_document(document=fichero_temporal, reply_markup=markup, caption=result_message)
			if x:
				delete_message(x.message_id)
		else:
			debug(f"Scheduled prune executed: {result_message}")

	def run(self):
		"""
		Main loop: evaluates every minute exactly once.

		It used to sleep 60 s after doing the work and then look at whatever
		minute it had landed in. The work pushed every lap later, so sooner or
		later a lap jumped from :00:55 to :02:05 and the tasks due at :01 never
		ran. Now it wakes at the start of each minute and makes up for any it
		missed.

		The @reboot tasks run here and not when the monitor is built: that is
		the main thread, and an exec that hangs there held up the start of
		polling with it.
		"""
		self._execute_reboot_tasks()
		while True:
			try:
				self._tick(datetime.now())
			except Exception as e:
				error(f"Error reading schedule file: [{e}]")
			time.sleep(60.5 - time.time() % 60)

	def _minutes_due(self, now):
		"""The minutes to evaluate now, oldest first, and marks them as done."""
		current = now.replace(second=0, microsecond=0)
		last = self._last_minute
		# The gap in real time, which a change of the clock's hour does not
		# move. In spring the wall clock goes from 01:59 to 03:00 in a minute:
		# taken for an hour the bot was not running, the tasks of 02:xx were
		# skipped that day. That is a gap of a minute, and those minutes are
		# replayed —each task once, like any catch-up— as cron does.
		long_gap = last is not None and current - last > timedelta(minutes=self.MAX_CATCH_UP_MINUTES) \
			and current.timestamp() - last.timestamp() > self.MAX_CATCH_UP_MINUTES * 60
		if last is None or long_gap or last - current > timedelta(hours=2):
			# First lap, a gap too long to replay, or the clock set well back:
			# start over from here.
			if last is not None and current > last:
				warning(f"Schedules: skipping {int((current - last).total_seconds() // 60) - 1} "
						f"minute(s) the bot was not running for")
			due = [current]
		elif current <= last:
			# The same minute again, or the clock went back a little —the
			# autumn change—: those minutes were already evaluated.
			due = []
		else:
			due = []
			minute = last + timedelta(minutes=1)
			while minute <= current:
				due.append(minute)
				minute += timedelta(minutes=1)
		if due:
			self._last_minute = due[-1]
		return due

	def _notify(self, message):
		"""One of the scheduler's own notices, kept quiet while muted."""
		if is_muted():
			debug(f"Message [{message}] omitted because muted")
			return
		send_message_to_notification_channel(message=message)

	def _check_down_hosts(self):
		"""
		Says when a host whose tasks were being skipped answers again.

		Asked every minute rather than left for its next task to find out: a
		daily task would otherwise announce the host back up to a day late.
		Only the hosts that are down are asked, so a healthy fleet costs
		nothing here.

		A remote host's return is told by HostAvailability, for every host and
		not only the ones with tasks, so here it is only noted: saying it twice
		would be one message too many. What is left to say here is the local
		host, which that one does not watch.
		"""
		if not self._down_hosts:
			return
		entries = [host_registry.host(host_id) for host_id in self._down_hosts]
		for host_id in [h for h, entry in zip(list(self._down_hosts), entries) if entry is None]:
			# Removed meanwhile: nothing will come back.
			self._down_hosts.pop(host_id, None)
		statuses = host_registry.status_snapshot(entries=[e for e in entries if e])
		watched = {e["id"] for e in entries if e and HostAvailability.watched(e)}
		for host_id, (ok, _reason) in statuses.items():
			if ok and self._down_hosts.pop(host_id, None) and host_id not in watched:
				self._notify(get_text("schedule_host_back", host_alias(host_id)))

	def _tick(self, now):
		"""Runs every task due in the minutes not yet evaluated."""
		try:
			self._check_down_hosts()
		except Exception as e:
			debug(f"Could not check the hosts whose schedules were skipped: {e}")
		due = self._minutes_due(now)
		if not due:
			return
		for schedule in self.schedule_manager.get_all_schedules():
			if not schedule.get("enabled", True):
				continue
			cron_expr = schedule.get("cron")
			# @reboot tasks run once, when the daemon starts
			if cron_expr == "@reboot":
				continue
			# Once even if it matches more than one of the minutes made up for:
			# catching up is about not losing a run, not about bunching them.
			if any(self.should_run(schedule.get("name"), cron_expr, minute) for minute in due):
				self._execute_schedule_action(schedule)

	def should_run(self, schedule_name, cron_expr, now):
		"""Whether a cron expression fires in the minute of `now`."""
		try:
			minute = now.replace(second=0, microsecond=0)
			return croniter.match(cron_expr, minute)
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

def schedule_button_value(schedule):
	"""What a list button carries to name a task: its id, never its place."""
	return f"i{schedule.get('id')}"

def _validate_schedule_index(index_str: str, schedules: list) -> int:
	"""
	Where in `schedules` the task a list button names is, or -1.

	Found by the task's id. The button used to carry the place the task had
	when the list was drawn, which named another task once the list changed
	underneath —one deleted from a newer list, by another admin, a double
	tap— and deleted that one, with no confirmation. A button from before
	carries only that place, so it is refused rather than guessed.
	"""
	value = str(index_str or "")
	if not (value.startswith("i") and value[1:].isdigit()):
		return -1
	for idx, sched in enumerate(schedules):
		if str(sched.get("id")) == value[1:]:
			return idx
	return -1

def _build_schedule_summary(state: dict) -> str:
	"""Build a consistent schedule summary message from state dict"""
	lines = []

	# Add schedule details
	if state.get("name"):
		lines.append(f"<b>{get_text('schedule_label_name')}:</b> {html.escape(str(state.get('name')))}")
	if state.get("cron"):
		lines.append(f"<b>{get_text('schedule_label_cron')}:</b> {state.get('cron')}")
	if state.get("action"):
		lines.append(f"<b>{get_text('schedule_label_action')}:</b> {state.get('action')}")
	if state.get("container"):
		# Typed by hand when a task is edited, so not necessarily a name Docker
		# would have allowed.
		lines.append(f"<b>{get_text('schedule_label_container')}:</b> {html.escape(str(state.get('container')))}")
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
		lines.append(f"<b>{get_text('schedule_label_command')}:</b> <code>{html.escape(str(state.get('command')))}</code>")

	return "\n".join(lines)

def _get_available_containers() -> list:
	"""
	The containers a scheduled task can act on, as (host_entry, container).

	Across every reachable host: a task pinned to one machine would otherwise
	only ever be offered the local one's containers.
	"""
	available = []
	for entry, _, containers in hosts_with_containers():
		for container in containers:
			if not is_own_container(entry["id"], container.id, container.name):
				available.append((entry, container))
	return available

def schedule_host_missing(schedule):
	"""
	Whether a task acts on a host the bot no longer has.

	Only a removed one: a paused or unreachable host is still registered and
	comes back. Re-adding the machine from /settings does not help either,
	since it comes in under a new id, so the task has to be moved to a host
	that exists. Only a host declared by hand in settings.json with the same id
	would match again.

	Never while settings.json cannot be read: the host list is then only the
	defaults, every task's host looks gone, and answering yes switched off
	every task, which stayed off once the file was fixed.
	"""
	if (schedule.get("action") or "").lower() not in HOST_SCOPED_SCHEDULE_ACTIONS:
		return False
	if store.settings_unreadable():
		return False
	return host_registry.host(schedule.get("host") or host_registry.local_host_id()) is None


def schedule_host_value(schedule):
	"""
	What a task's details say for its host, or None to leave the line out.

	A missing host is said even with a single host left, which is exactly when
	the line would otherwise be hidden and the task would look runnable.
	"""
	if schedule_host_missing(schedule):
		return get_text("schedule_host_missing")
	if host_registry.is_single_host() or schedule.get("action") not in HOST_SCOPED_SCHEDULE_ACTIONS:
		return None
	return host_alias(schedule.get("host") or host_registry.local_host_id())


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
			# Typed by hand, so escaped: a command with `<` in it —`mysql < dump.sql`—
			# made the whole listing fail to send, every time /schedule was opened.
			name = html.escape(str(sched['name']))
			action = sched.get('action', '')
			cron = sched.get('cron', '* * * * *')
			container = html.escape(str(sched.get('container') or ''))
			minutes = sched.get('minutes', '')
			command = html.escape(str(sched.get('command') or ''))
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
			host_value = schedule_host_value(sched)
			if host_value:
				host_line = f"  {get_text('schedule_label_host')}: <b>{host_value}</b>"

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

	# Split like any long listing: past 4096 characters, about twenty tasks,
	# Telegram refused the menu and, with it, the only way to manage them.
	send_split_message(message_text, reply_markup=markup)

def show_schedule_delete_list(user_id: int, chat_id: int):
	"""Show list of schedules to delete - Optimized"""
	schedules = schedule_manager.get_all_schedules()

	if not schedules:
		send_message(message=get_text("schedule_no_schedules"))
		return

	# Build message efficiently
	header = get_text("schedule_select_to_delete")
	schedule_lines = [f"{idx}. <code>{html.escape(str(sched['name']))}</code>" for idx, sched in enumerate(schedules, 1)]
	message_text = f"{header}\n\n" + "\n".join(schedule_lines)

	markup = InlineKeyboardMarkup(row_width=5)
	buttons = [InlineKeyboardButton(str(idx), callback_data=f"scheduleSelectDelete|{schedule_button_value(sched)}")
	           for idx, sched in enumerate(schedules, 1)]
	markup.add(*buttons)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))

	send_split_message(message_text, reply_markup=markup)

def show_schedule_edit_list(user_id: int, chat_id: int):
	"""Show list of schedules to edit - Optimized"""
	schedules = schedule_manager.get_all_schedules()

	if not schedules:
		send_message(message=get_text("schedule_no_schedules"))
		return

	# Build message efficiently
	header = get_text("schedule_select_to_edit")
	schedule_lines = [f"{idx}. <code>{html.escape(str(sched['name']))}</code>" for idx, sched in enumerate(schedules, 1)]
	message_text = f"{header}\n\n" + "\n".join(schedule_lines)

	markup = InlineKeyboardMarkup(row_width=5)
	buttons = [InlineKeyboardButton(str(idx), callback_data=f"scheduleSelectEdit|{schedule_button_value(sched)}")
	           for idx, sched in enumerate(schedules, 1)]
	markup.add(*buttons)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))

	send_split_message(message_text, reply_markup=markup)

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
	message_text = f"<b>{html.escape(schedule_name)}</b>\n\n"
	message_text += f"<b>{get_text('schedule_label_status')}:</b> {status_icon} {status_text}\n"
	message_text += f"<b>{get_text('schedule_label_cron')}:</b> <code>{cron}</code>\n"
	message_text += f"<b>{get_text('schedule_label_action')}:</b> <b>{action}</b>\n"
	# Which machine the task runs on, in the same position as the listing and
	# the creation summary: after what the task acts on. Same rule as the
	# listing, from schedule_host_value: only for actions that act on Docker,
	# with more than one host or when the task's host no longer exists.
	host_line = ""
	host_value = schedule_host_value(schedule)
	if host_value:
		host_line = f"<b>{get_text('schedule_label_host')}:</b> <b>{host_value}</b>\n"

	if action == 'mute':
		message_text += f"<b>{get_text('schedule_label_minutes')}:</b> <b>{minutes}</b>\n"
	elif action == 'exec':
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{html.escape(str(container or ''))}</b>\n"
		message_text += f"<b>{get_text('schedule_label_command')}:</b> <code>{html.escape(str(command or ''))}</code>\n"
		message_text += host_line
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> <b>{get_text('schedule_yes') if show_output else get_text('schedule_no')}</b>\n"
	elif action == 'prune':
		message_text += f"<b>{get_text('schedule_label_prune_type')}:</b> <b>{prune_type}</b>\n"
		message_text += host_line
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> <b>{get_text('schedule_yes') if show_output else get_text('schedule_no')}</b>\n"
	elif action in ('run', 'stop', 'restart'):
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{html.escape(str(container or ''))}</b>\n"
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
		# A container task moves host with "Container", since the name only
		# means something on one machine. A prune names none, so its host is
		# its own field: with one host left there is nowhere to move it,
		# unless the one it points at is gone.
		if not host_registry.is_single_host() or schedule_host_missing(schedule):
			markup.add(InlineKeyboardButton(get_text("schedule_edit_host"), callback_data=f"scheduleEditField|host|{schedule_id}"))

	if action in ('exec', 'prune'):
		markup.add(InlineKeyboardButton(get_text("schedule_edit_show_output"), callback_data=f"scheduleEditField|show_output|{schedule_id}"))

	# Add status toggle button
	status_button_text = get_text("schedule_button_disable") if enabled else get_text("schedule_button_enable")
	markup.add(InlineKeyboardButton(status_button_text, callback_data=f"scheduleEditStatus|{schedule_id}"))

	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))

	send_message(message=message_text, reply_markup=markup)

def ask_schedule_name(user_id: int):
	"""Ask user for schedule name"""
	state = init_add_schedule_state()
	message_text = typed_prompt("schedule_ask_name")
	msg = send_message(message=message_text, reply_markup=create_simple_keyboard("button_cancel", "scheduleCancel"))
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)

def note_schedule_containers(state, available_containers, choose):
	"""
	Notes in a schedule's state the containers it can be pointed at.

	The choice travels as an index, with the name and the host kept in the
	state: a reference plus a name would not fit in 64 bytes of callback_data
	for every container someone might have. `choose` is what the buttons
	carry before that index.
	"""
	for idx, (entry, container) in enumerate(available_containers):
		state[f"container_{idx}"] = container.name
		state[f"container_host_{idx}"] = entry["id"]
	state["container_count"] = len(available_containers)
	state["container_choice"] = choose
	state["container_page"] = 0


def schedule_container_markup(state):
	"""
	The keyboard to pick a schedule's container from those noted in `state`.

	A page at a time, like the other pickers: a button for every container
	on every host went past the hundred Telegram accepts on a big enough
	fleet, and the question never arrived, leaving the schedule half made.
	"""
	indexes = list(range(state.get("container_count") or 0))
	page, pages = update_keyboard_page(indexes, state.get("container_page"))
	single = host_registry.is_single_host()
	markup = InlineKeyboardMarkup(row_width=2)
	for idx in indexes[page * UPDATE_KEYBOARD_PAGE_SIZE:(page + 1) * UPDATE_KEYBOARD_PAGE_SIZE]:
		name = state.get(f"container_{idx}")
		label = name if single else f'{name} · {host_registry.alias(state.get(f"container_host_{idx}"))}'
		markup.add(InlineKeyboardButton(label, callback_data=f'{state.get("container_choice")}|{idx}'))
	if pages > 1:
		navigation = []
		if page > 0:
			navigation.append(InlineKeyboardButton("◀️", callback_data=f"scheduleContainerPage|{page - 1}"))
		navigation.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"scheduleContainerPage|{page}"))
		if page < pages - 1:
			navigation.append(InlineKeyboardButton("▶️", callback_data=f"scheduleContainerPage|{page + 1}"))
		markup.row(*navigation)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
	return markup


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

		note_schedule_containers(schedule_state, available_containers, "scheduleSelectContainer")
		markup = schedule_container_markup(schedule_state)
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
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))

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
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))

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
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))

	msg = send_message(message=message_text, reply_markup=markup)
	state["last_message_id"] = msg.message_id if msg else None
	save_schedule_state(user_id, state)


def confirm_schedule_creation(user_id: int, state: dict):
	"""Show confirmation of schedule creation"""
	# A task that acts on Docker always ends up with a host, so the executor
	# never has to guess: a prune on a single-host setup never got asked. A
	# mute is the bot's own notifications and gets none.
	if state.get("action") in HOST_SCOPED_SCHEDULE_ACTIONS and not state.get("host"):
		state["host"] = host_registry.local_host_id()
		save_schedule_state(user_id, state)
	elif state.get("action") not in HOST_SCOPED_SCHEDULE_ACTIONS:
		state["host"] = None
		save_schedule_state(user_id, state)
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
		InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel")
	)

	send_message(message=message_text, reply_markup=markup)

# What a schedule question accepts as a written answer. The container is not
# here: it is picked from a list, since its host goes with it.
SCHEDULE_TEXT_FIELDS = frozenset({"name", "cron", "command", "minutes"})
SCHEDULE_TEXT_STEPS = frozenset({"ask_name", "ask_cron", "ask_minutes", "ask_command"})

def handle_schedule_flow(user_id: int, user_input: str, state: dict, chat_id: int = None, user_message_id: int = None):
	"""Handle the schedule creation flow and editing"""
	step = state.get("step")
	chatId = chat_id  # Make chatId available in the function

	# Only some questions are answered by writing; the rest are buttons. Text
	# that arrives while one of those is open was not meant for the bot, so it
	# is left alone: deleting it, as before, swallowed every message the admin
	# wrote until the question was answered.
	if state.get("field"):
		if state.get("field") not in SCHEDULE_TEXT_FIELDS:
			return
	elif step not in SCHEDULE_TEXT_STEPS:
		return

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
				message_text += f"{typed_prompt('schedule_ask_name')}\n"
				message_text += f"<i>{get_text('current_value')}: {html.escape(schedule_name)}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, name=user_input)
			send_message(message=get_text("schedule_updated_success", html.escape(user_input)))
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
				message_text += f"{typed_prompt('schedule_ask_cron')}\n"
				message_text += f"<i>{get_text('current_value')}: {current_cron}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, cron=user_input)
			send_message(message=get_text("schedule_updated_success", html.escape(schedule_name)))
			clear_schedule_state(user_id)
			show_schedule_menu(user_id, chatId)
			return
		elif field == "command":
			schedule_manager.update_schedule(schedule_name, command=user_input)
			send_message(message=get_text("schedule_updated_success", html.escape(schedule_name)))
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
					message_text += f"{typed_prompt('schedule_ask_minutes')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
				message_text += f"{typed_prompt('schedule_ask_minutes')}\n"
				message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
				msg = send_message(message=message_text, reply_markup=markup)
				state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(user_id, state)
				return
			schedule_manager.update_schedule(schedule_name, minutes=minutes)
			send_message(message=get_text("schedule_updated_success", html.escape(schedule_name)))
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
			message_text += typed_prompt("schedule_ask_name")

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
		message_text = f"<b>{get_text('schedule_label_name')}:</b> {html.escape(user_input)}\n\n"
		message_text += typed_prompt("schedule_ask_cron")

		markup = InlineKeyboardMarkup(row_width=1)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
				message_text += f"<b>{get_text('schedule_label_name')}:</b> {html.escape(str(state.get('name')))}\n\n"
			message_text += typed_prompt("schedule_ask_cron")

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
		# What each action does, in the user's language. These strings were
		# translated into all eight locales and rendered nowhere: the screen
		# offered six buttons reading `run`, `stop`... in English and left the
		# user to guess. The token stays on the button because it is the
		# vocabulary of the feature — the summary, the listing and /schedule's
		# own usage text all show it — and the line explains it.
		message_text += "\n\n" + "\n".join(
			f'<code>{action}</code> · {get_text(f"schedule_action_{action}")}'
			for action in SCHEDULE_ACTIONS)

		markup = InlineKeyboardMarkup(row_width=2)
		markup.add(*[
			InlineKeyboardButton(action, callback_data=f"scheduleSelectAction|{action}")
			for action in SCHEDULE_ACTIONS
		])
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
				message_text += f"\n\n{typed_prompt('schedule_ask_minutes')}"

				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
			message_text += f"\n\n{typed_prompt('schedule_ask_minutes')}"

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="scheduleCancel"))
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
	"telemetry": "button_settings_telemetry",
}

# Which screen each toggle belongs to, so pressing one repaints the screen it
# was pressed on instead of bouncing back to the top level.
SETTINGS_TOGGLE_SCREEN = {
	"extended_messages": "main",
	"multi_selection": "main",
	"check_updates": "updates",
	"check_update_stopped_containers": "updates",
	"telemetry": "telemetry",
}

# --- ESTADÍSTICAS ANÓNIMAS ---------------------------------------------------
#
# Once a day, a snapshot of the settings and how many times each command and
# button was used, sent to telemetry.dgongut.com. Numbers, yes/no and values
# from closed lists only: never a name, an id or an address. What may be sent
# is declared in the manifest of the telemetry repo, which also drops anything
# that is not declared, and is listed on the project's privacy page.
#
# On by default, with no notice asking first. Nothing is counted or sent
# unless both hold:
#
#	allowed    the storage is a volume, and TELEMETRY=0 is not set
#	on         the setting, flipped from /settings
#
# Without a volume the install id would be new on every recreate, so one
# installation would count as many, and turning it off would not survive the
# next update.

TELEMETRY_PROJECT = "docker-controller-bot"
# Shown instead of the whole list when the preview would not fit a message.
TELEMETRY_PREVIEW_USAGE_KEYS = 25

def telemetry_forced_off():
	"""
	Why telemetry is off whatever the setting says, or None when it is not.

	Returns the variable that turned it off, or "volume" when the storage
	would not survive a recreate.
	"""
	if telemetry.disabled_by_environment():
		return "TELEMETRY"
	if not store.is_persistent():
		return "volume"
	return None

def telemetry_on():
	"""Whether the setting is on and nothing overrides it."""
	return telemetry_forced_off() is None and bool(store.get("bot.telemetry"))

def _containers_bucket(count):
	"""A container count as the range the manifest declares, never the number."""
	for limit, label in ((0, "0"), (5, "1-5"), (10, "6-10"), (25, "11-25"), (50, "26-50"), (100, "51-100")):
		if count <= limit:
			return label
	return "101+"

def collect_telemetry_metrics():
	"""
	The snapshot sent with each ping, on the telemetry thread.

	Every key here has to be declared in the manifest, or the server drops it.
	Counting containers is the slow part and the one that can fail: a host
	that does not answer is simply left out, and without any host the key is
	left out rather than sent as zero.
	"""
	configured = host_registry.hosts(include_paused=True)
	schemes = [str(entry.get("url", "")).split("://", 1)[0].lower() for entry in configured]
	interval = update_interval_hours()
	metrics = {
		"hosts": len(configured),
		"hosts_paused": sum(1 for entry in configured if entry.get("paused")),
		"hosts_ssh": schemes.count("ssh"),
		"hosts_tcp": schemes.count("tcp"),
		"schedules": sum(1 for schedule in schedule_manager.get_all_schedules() if schedule.get("enabled", True)),
		"admins": len(ADMIN_IDS),
		# The environment and not the global: that one falls back to the admin
		# when there is no group, which would make everyone look like a group.
		"telegram_group": bool(os.environ.get("TELEGRAM_GROUP")),
		"language": language(),
		"button_columns": button_columns(),
		"check_updates": bool(store.get("bot.check_updates")),
		"check_update_stopped_containers": bool(store.get("bot.check_update_stopped_containers")),
		"extended_messages": bool(store.get("bot.extended_messages")),
		"multi_selection": bool(store.get("bot.multi_selection")),
		"notification_channel": notification_channel() is not None,
		"legacy_volume": store.uses_legacy_root(),
	}
	metrics["check_update_every_hours"] = interval

	try:
		listed = hosts_with_containers()
		if listed:
			total = auto_update = ignore_updates = auto_update_bot = ignore_updates_bot = 0
			for entry, _owner, containers in listed:
				for container in containers:
					labels = container.labels or {}
					total += 1
					auto_update += label_enabled(labels, LABEL_AUTO_UPDATE)
					ignore_updates += label_enabled(labels, LABEL_IGNORE_CHECK_UPDATES)
					# Apart from the labels, rather than counted with them:
					# those two are declared as what carries the label, and
					# their history says so.
					mode, label = update_mode(entry["id"], container.name, labels)
					if label is None:
						auto_update_bot += mode == "auto"
						ignore_updates_bot += mode == "ignore"
			metrics["containers"] = _containers_bucket(total)
			metrics["containers_auto_update"] = auto_update
			metrics["containers_ignore_updates"] = ignore_updates
			metrics["containers_auto_update_bot"] = auto_update_bot
			metrics["containers_ignore_updates_bot"] = ignore_updates_bot
	except Exception as e:
		debug(f"Telemetry could not count containers: {e}")

	try:
		docker_version = str(host_registry.client(host_registry.local_host_id()).version().get("Version", ""))
		metrics["docker_major"] = int(docker_version.split(".", 1)[0])
	except Exception as e:
		debug(f"Telemetry could not read the Docker version: {e}")
	return metrics

telemetry_client = telemetry.Telemetry(
	project=TELEMETRY_PROJECT,
	version=VERSION,
	state_path=os.path.join(store.state_dir(), "telemetry.json"),
	metrics=collect_telemetry_metrics,
	enabled=telemetry_on,
	endpoint=TELEMETRY_ENDPOINT,
	log=debug,
	debug=TELEMETRY_DEBUG,
)
if TELEMETRY_DEBUG:
	warning(f"TELEMETRY_DEBUG is on: statistics go to {TELEMETRY_ENDPOINT} a minute after every start")

def count_usage(key):
	"""Counts one use of `key` for today's ping. Never raises into the caller."""
	try:
		telemetry_client.count(key)
	except Exception as e:
		debug(f"Telemetry could not count {key}: {e}")

def disable_telemetry():
	"""
	Turns telemetry off and forgets this installation.

	The id goes with it, so turning it back on later starts as a new
	installation rather than stitching the two periods together.
	"""
	store.set("bot.telemetry", False)
	telemetry_client.forget()

def build_telemetry_preview():
	"""
	Exactly what the next ping would carry, as a message.

	The usage counters are cut to the most used ones when the whole list
	would not fit in a Telegram message, and the message says so.
	"""
	payload = telemetry_client.preview()
	usage = payload.get("usage") or {}
	hidden = 0
	if len(usage) > TELEMETRY_PREVIEW_USAGE_KEYS:
		ranked = sorted(usage.items(), key=lambda item: (-item[1], item[0]))
		payload["usage"] = dict(ranked[:TELEMETRY_PREVIEW_USAGE_KEYS])
		hidden = len(usage) - TELEMETRY_PREVIEW_USAGE_KEYS
	body = html.escape(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True))
	text = f'{get_text("telemetry_preview")}\n\n<pre>{body}</pre>'
	if hidden:
		text += f'\n{get_text("telemetry_preview_truncated", hidden)}'
	markup = InlineKeyboardMarkup()
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	return text, markup

def build_settings_telemetry():
	"""
	The statistics screen: what they are, the switch, and what they send.

	When something outside the menu has turned them off there is no switch,
	only the reason, because pressing it would change nothing.
	"""
	forced = telemetry_forced_off()
	lines = [get_text("settings_telemetry_title"), "", get_text("settings_telemetry_help")]
	markup = InlineKeyboardMarkup(row_width=1)
	if forced == "volume":
		lines += ["", get_text("settings_telemetry_forced_volume")]
	elif forced:
		lines += ["", get_text("settings_telemetry_forced_env", forced)]
	else:
		markup.add(_toggle_button("telemetry"))
	markup.add(InlineKeyboardButton(get_text("button_telemetry_show"), callback_data="telemetryShow"))
	_add_navigation(markup, "settings")
	return "\n".join(lines), markup

def _on_off(value):
	return "✅" if value else "❌"

def _selected_prefix(is_selected):
	"""Marks the option currently in force inside a picker."""
	return "✅ " if is_selected else ""

def _format_interval(hours):
	"""Shows 4.0 as 4 and leaves 0.5 alone, so the menu reads like a number."""
	try:
		hours = as_interval_hours(hours)
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
	interval = _format_interval(update_interval_hours())

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
		# Todos, pausados incluidos: la fila dice cuántas máquinas tienes
		# configuradas, no cuántas están respondiendo ahora mismo.
		get_text("settings_row_hosts", len(host_registry.hosts(include_paused=True))),
		callback_data="settingsHosts"))
	if channel:
		markup.add(InlineKeyboardButton(get_text("button_settings_clear_channel"), callback_data="settingsClearChannel"))
	markup.add(InlineKeyboardButton(
		get_text("settings_row_telemetry", _on_off(telemetry_on())),
		callback_data="settingsTelemetry"))
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
		get_text("settings_row_interval", _format_interval(update_interval_hours())),
		callback_data="settingsAskInterval"))
	markup.add(_toggle_button("check_update_stopped_containers"))
	markup.add(InlineKeyboardButton(get_text("settings_row_containers"), callback_data="settingsContainers"))
	_add_navigation(markup, "settings")

	text = f'{get_text("settings_updates_title")}\n\n{get_text("settings_updates_help")}'
	return text, markup

def _checks_off_warning():
	"""What a per-container choice needs said while the checks are off."""
	return "" if store.get("bot.check_updates") else f'\n\n{get_text("settings_containers_checks_off")}'

# What a tap on a container moves it to: tell me, update on its own, do not
# check, and round again. "Update on its own" first, because it is what most
# taps are for. On the way to "do not check" it spends a moment there, which
# _auto_update covers: it asks again before updating anything.
NEXT_UPDATE_MODE = {None: "auto", "auto": "ignore", "ignore": None}

def next_update_mode(host_id, mode, image):
	"""
	The mode a tap moves a container on to: the next one round, past "update
	on its own" for what might be the bot, which would only be refused —and
	every tap would then propose it again, with "do not check" out of reach.
	"""
	following = NEXT_UPDATE_MODE[mode]
	if following == "auto" and might_be_the_bot(host_id, image):
		following = NEXT_UPDATE_MODE[following]
	return following

# How each mode shows in front of a container's name in the list: the same
# as on the /info button.
UPDATE_MODE_ICONS = {None: "🔔", "auto": "🔄", "ignore": "🚫"}

def _settings_containers_header():
	return f'{get_text("settings_containers_title")}\n\n{get_text("settings_containers_help")}'

def _away_lines(answered):
	"""
	The hosts that did not answer, or are paused, and have settings kept:
	they have no list to show, and what they have stays until they are back,
	or removed.
	"""
	stored = store.container_settings()
	return [get_text("settings_containers_host_away", host_alias(entry["id"]), len(stored[entry["id"]]))
			for entry in host_registry.hosts(include_paused=True)
			if entry["id"] not in answered and stored.get(entry["id"])]

def build_settings_containers_hosts(sections, answered):
	"""
	Which host's containers to list, when more than one has any.

	A list per host rather than one for the fleet: across machines every
	button has to carry the host's name, one to a row, and sixty containers
	are sixty rows. `sections` are the hosts with containers to list, and
	`answered` the ids of every host that answered, with them or without.
	"""
	markup = InlineKeyboardMarkup(row_width=1)
	for entry, _owner, _containers in sections:
		markup.add(InlineKeyboardButton(f'🖥️ - {entry.get("alias", entry["id"])}',
										callback_data=f'ctrHost|{entry["id"]}'))
	_add_navigation(markup, "settingsUpdates")

	lines = [_settings_containers_header(), "",
			 get_text("settings_containers_pick_host") if sections else get_text("error_no_containers_available")]
	lines += _away_lines(answered)
	return "\n".join(lines) + _checks_off_warning(), markup

def container_list_items(containers):
	"""
	A host's containers as a per-container list keeps them: name, its DCB-*
	labels and its image, which is all a tap needs to know without asking
	the daemon again.
	"""
	def dcb_labels(labels):
		labels = labels if isinstance(labels, dict) else {}
		return {key: value for key, value in labels.items()
				if key in (LABEL_AUTO_UPDATE, LABEL_IGNORE_CHECK_UPDATES)}
	return [[container.name, dcb_labels(container.labels), container_image(container)]
			for container in containers]

def _container_list_key(chat_id, message_id):
	return f"ctr_list_{chat_id}_{message_id}"

def save_container_list(chat_id, message_id, data):
	write_cache_item(_container_list_key(chat_id, message_id), data)

def load_container_list(chat_id, message_id, host_id):
	"""
	A per-container list's data, or None when the button pressed is not from
	the list the message shows: expired, or left for another host by a tap
	or an admin quicker than this one. Its buttons carry the host for that.
	"""
	data = read_cache_item(_container_list_key(chat_id, message_id))
	if not isinstance(data, dict) or not isinstance(data.get("items"), list) or data.get("host") != host_id:
		return None
	return data

def clear_container_list(chat_id, message_id):
	delete_cache_item(_container_list_key(chat_id, message_id))

# Serialises what reads a per-container list and writes it back: a tap that
# waited on the daemon must not save over a Back or a page change made
# meanwhile, nor they over it.
container_list_lock = threading.Lock()

def container_list_icon(current, label):
	"""How a container shows in the list: 🔔, 🔄 or 🚫, and 🏷️ in front when a label decides."""
	if not label:
		return UPDATE_MODE_ICONS[current]
	return "🏷️" if current is None else f"🏷️{UPDATE_MODE_ICONS[current]}"

def build_settings_containers_list(data):
	"""
	One host's containers, each a tap away from the next mode.

	One list for the three rather than one per mode: each button says how
	its container is set, and a tap moves it on. The buttons carry the host,
	the container's place in the list and the mode the tap sets, never
	"next": two taps, or two admins on the same list, then land on the same
	answer instead of going round twice, and a tap on a list the message no
	longer shows lands on nothing.

	Saved on the spot, like the rest of /settings: /updateall asks to confirm
	because updating is costly, and choosing is not.
	"""
	host_id = data["host"]
	items = data["items"]
	page, pages = update_keyboard_page(items, data.get("page", 0))
	start = page * UPDATE_KEYBOARD_PAGE_SIZE

	markup = InlineKeyboardMarkup(row_width=button_columns())
	buttons = []
	for index in range(start, min(start + UPDATE_KEYBOARD_PAGE_SIZE, len(items))):
		name, labels, image = items[index]
		current, label = update_mode(host_id, name, labels)
		buttons.append(InlineKeyboardButton(
			f"{container_list_icon(current, label)} {name}",
			callback_data=f"ctrToggle|{host_id}|{index}|{update_mode_code(next_update_mode(host_id, current, image))}"))
	markup.add(*buttons)
	if pages > 1:
		navigation = []
		if page > 0:
			navigation.append(InlineKeyboardButton("◀️", callback_data=f"ctrPage|{host_id}|{page - 1}"))
		navigation.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"ctrPage|{host_id}|{page}"))
		if page < pages - 1:
			navigation.append(InlineKeyboardButton("▶️", callback_data=f"ctrPage|{host_id}|{page + 1}"))
		markup.row(*navigation)
	# Back to the hosts when there was a choice of them.
	_add_navigation(markup, "settingsContainers" if data.get("several_hosts") else "settingsUpdates")

	lines = [_settings_containers_header()]
	if not host_registry.is_single_host():
		lines += ["", get_text("list_host_header", host_alias(host_id))]
	if not items:
		lines += ["", get_text("error_no_containers_available")]
	if data.get("answered") is not None:
		away = _away_lines(data["answered"])
		if away:
			lines += [""] + away
	return "\n".join(lines) + _checks_off_warning(), markup

def show_settings_containers(chat_id, message_id):
	"""
	The hosts to choose from, or straight to the list when only one has
	containers to show — as the /info picker does.
	"""
	sections = hosts_with_containers()
	answered = [entry["id"] for entry, _owner, _containers in sections]
	sections = [section for section in sections if section[2]]
	if len(sections) == 1:
		entry, _owner, containers = sections[0]
		show_settings_containers_list(chat_id, message_id, entry["id"], containers,
									  several_hosts=False, answered=answered)
		return
	text, markup = build_settings_containers_hosts(sections, answered)
	edit_message_text(text, chat_id, message_id, reply_markup=markup)

def show_settings_containers_list(chat_id, message_id, host_id, containers=None, several_hosts=True, answered=None):
	"""
	Lists one host's containers, in place of the menu.

	`answered` is given when there was no choice of host to make, and so no
	screen that named the hosts not answering: the list names them instead.
	"""
	if containers is None:
		try:
			containers = manager(host_id).list_containers()
		except Exception as e:
			reason = e.reason if isinstance(e, host_registry.HostUnavailable) else e
			send_message(message=get_text("host_unreachable", host_alias(host_id), html.escape(str(reason))))
			return
	data = {"host": host_id, "page": 0, "several_hosts": several_hosts,
			"answered": answered, "items": container_list_items(containers)}
	save_container_list(chat_id, message_id, data)
	text, markup = build_settings_containers_list(data)
	edit_message_text(text, chat_id, message_id, reply_markup=markup)

def toggle_in_container_list(data, index, mode):
	"""
	Sets the container at `index` of a list to `mode`, the one its button
	said a tap would set.

	The container is looked at again first, one request: the list may have
	been open a while. Removed since —from the bot, or with docker rm—, a
	tap stored a setting for nothing, and its cleanup announced a month
	later; recreated with a label since, it stored a value the label then
	overrode, while the button said otherwise. The item is refreshed with
	what was found, so the repaint shows it.

	Returns None, or what to say in an alert when it was not done.
	"""
	name = data["items"][index][0]
	host_id = data["host"]
	try:
		live = manager(host_id).container_named(name)
	except Exception as e:
		reason = e.reason if isinstance(e, host_registry.HostUnavailable) else e
		return alert_text(get_text("host_unreachable", host_alias(host_id), html.escape(str(reason))))
	if live is None:
		return alert_text(get_text("container_does_not_exist", html.escape(name)))
	data["items"][index] = container_list_items([live])[0]
	_name, labels, image = data["items"][index]
	current, label = update_mode(host_id, name, labels)
	if label:
		return get_text("container_updates_by_label", label)
	if current == mode:
		return None
	return set_update_mode(host_id, name, labels, image, mode)

def alert_text(text):
	"""A message's text for an alert, which is plain: its tags taken out."""
	return html.unescape(re.sub(r"<[^>]+>", "", text))

def expire_container_list(call_id):
	"""
	Says a per-container list can no longer be used.

	Without taking the buttons away, unlike an update list: the message may
	already show another screen, and a removal queued behind its repaint
	would leave that one without buttons.
	"""
	answer_callback_quietly(call_id, text=get_text("settings_containers_expired"), show_alert=True)

def build_settings_hosts():
	"""
	The list of Docker hosts, each with whether it answers right now.

	Every host is checked in parallel with a deadline, so opening this with a
	machine unplugged costs a few seconds once instead of hanging for the sum
	of every timeout.

	Paused ones are listed too — this is the screen they are resumed from —
	but nobody asks them anything: a pause is precisely the answer to a machine
	that does not answer, so probing it here would bring back the wait it was
	meant to end.
	"""
	configured = host_registry.hosts(include_paused=True)
	statuses = host_registry.status_snapshot(
		entries=[entry for entry in configured if not entry.get("paused")])
	markup = InlineKeyboardMarkup(row_width=1)
	for entry in configured:
		if entry.get("paused"):
			icon = "⏸️"
		else:
			icon = "🟢" if statuses.get(entry["id"], (False, ""))[0] else "🔴"
		markup.add(InlineKeyboardButton(
			f'{icon} - {entry.get("alias", entry["id"])}',
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

	paused = bool(entry.get("paused"))
	lines = [get_text("settings_host_title", host_alias(host_id))]
	lines.append(f'<code>{html.escape(entry.get("url", ""))}</code>')
	lines.append("")
	if paused:
		# Not asked whether it answers: that is the point of having paused it.
		lines.append(get_text("settings_host_paused"))
	else:
		# Only this host: sweeping the fleet to draw one screen means waiting
		# on every other machine as well.
		ok, reason = host_registry.host_status(host_id)
		lines.append(get_text("settings_host_ok") if ok else get_text("settings_host_failed"))
		if not ok and reason:
			lines.append(f'<i>{html.escape(str(reason))}</i>')
	if entry.get("local"):
		lines.append("")
		lines.append(get_text("settings_host_is_local"))

	markup = InlineKeyboardMarkup(row_width=1)
	if not paused:
		markup.add(InlineKeyboardButton(get_text("button_host_test"), callback_data=f"settingsHost|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_host_rename"), callback_data=f"settingsHostRename|{host_id}"))
	# The local host has none of these: the bot itself runs on it, so it can be
	# neither moved elsewhere, nor removed, nor left out of the sweeps.
	if not entry.get("local"):
		markup.add(InlineKeyboardButton(get_text("button_host_change_url"), callback_data=f"settingsHostUrl|{host_id}"))
		markup.add(InlineKeyboardButton(
			get_text("button_host_resume") if paused else get_text("button_host_pause"),
			callback_data=f"settingsHostPause|{host_id}"))
		markup.add(InlineKeyboardButton(get_text("button_host_remove"), callback_data=f"settingsHostRemove|{host_id}"))
	_add_navigation(markup, "settingsHosts")
	return "\n".join(lines), markup


def ask_host_url(user_id, host_id):
	"""Asks for a remote host's new URL, saying what it is now."""
	entry = host_registry.host(host_id)
	if entry is None or entry.get("local"):
		send_settings_menu(screen="hosts")
		return
	ask_text_input(user_id, f"host_url:{host_id}", "settings_host_ask_url", back_to="hosts",
				   prompt_args=(host_alias(host_id), html.escape(entry.get("url", ""))))

def _host_url_check(host_id, url):
	"""
	host_registry.probe_new_url, as (same_daemon, None), or (None, why not)
	with the text that says so.
	"""
	try:
		return host_registry.probe_new_url(host_id, url), None
	except host_registry.HostRejected as e:
		debug(f"Rejected new URL {url} for host {host_id}: {e.reason}")
		if e.reason == "same":
			return None, get_text("settings_host_url_same")
		if e.reason in ("local", "missing"):
			return None, get_text("error_can_not_do_that")
		key = "settings_host_duplicate" if e.reason == "duplicate" else "settings_host_bad_scheme"
		return None, get_text(key, html.escape(url))
	except host_registry.HostUnavailable as e:
		debug(f"New URL {url} for host {host_id} does not answer: {e.reason}")
		return None, get_text("settings_host_unreachable", html.escape(url), html.escape(str(e.reason)))

def _apply_host_url(host_id, url, same_daemon):
	"""Changes the URL, and returns what to say about it."""
	try:
		host_registry.change_url(host_id, url)
	except host_registry.HostRejected as e:
		debug(f"Could not change host {host_id} to {url}: {e.reason}")
		if e.reason == "duplicate":
			return get_text("settings_host_duplicate", html.escape(url))
		return get_text("error_can_not_do_that")
	# Its manager held a client on the old URL; the next use builds one on the
	# new. The event stream follows on the supervisor's next pass.
	forget_manager(host_id)
	if same_daemon is False:
		# What the cache knew was about another machine's containers, and a
		# name they happen to share would have carried it over.
		store.forget_host_updates(host_id)
	return get_text("settings_host_url_changed", host_alias(host_id), html.escape(url))

def _send_host_screen(host_id, prefix):
	"""The host's own screen, led by `prefix`, as a new message."""
	built = build_settings_host(host_id)
	if built is None:
		send_settings_menu(prefix=prefix, screen="hosts")
		return
	text, markup = built
	send_message(message=f"{prefix}\n\n{text}", reply_markup=markup)

def receive_host_url(user_id, host_id, raw):
	"""
	Takes the URL typed for a host, and changes it or asks first.

	Straight away when the new URL reaches the same Docker as the old one:
	the same machine by another way, nothing for anyone to weigh. Otherwise
	it asks, saying why: another Docker, or one it cannot compare because the
	old URL does not answer. Either way what the host keeps —the settings of
	its containers, its schedules— would then apply to the containers of
	whatever machine that is, by name.

	The URL waits for the confirmation in this admin's pending state, never in
	the button: it is not limited to 64 bytes there, and a button cannot be
	made to carry a URL nobody typed.
	"""
	_alias, url = parse_host_definition(raw)
	if not url:
		_send_host_screen(host_id, get_text("settings_host_invalid"))
		return
	same_daemon, refused = _host_url_check(host_id, url)
	if refused:
		_send_host_screen(host_id, refused)
		return
	if same_daemon:
		_send_host_screen(host_id, _apply_host_url(host_id, url, same_daemon))
		return
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_host_url_confirm"), callback_data=f"settingsHostUrlConfirm|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data=f"settingsHostUrlCancel|{host_id}"))
	key = "settings_host_url_other_daemon" if same_daemon is False else "settings_host_url_unknown_daemon"
	sent = send_message(message=get_text(key, host_alias(host_id), html.escape(url)), reply_markup=markup)
	if sent:
		# With the message that asks: a question asked twice leaves two, and
		# each one confirms its own URL —the one it shows—, not the latest.
		_save_cache("pending_host_url", user_id, asked_here({
			"host": host_id, "url": url, "message": sent.message_id}))

def _pending_host_url(user_id, chat_id, message_id, host_id):
	"""
	The URL a confirmation message holds for this admin, or None: asked by
	another admin, asked again since, or too long ago.
	"""
	pending = answerable(_load_cache("pending_host_url", user_id), chat_id)
	if not pending or pending.get("host") != host_id or pending.get("message") != message_id:
		return None
	return pending

def confirm_host_url(call_id, user_id, chat_id, message_id, host_id):
	"""
	Changes a host's URL once confirmed, checking it all again first: a
	minute later the URL may have stopped answering, or another admin may
	have registered it as a host of its own.

	A tap on a confirmation that is not this admin's pending one only says
	so, and leaves the message alone: in a group it may be another admin's
	question, still waiting for its own answer.
	"""
	pending = _pending_host_url(user_id, chat_id, message_id, host_id)
	if pending is None:
		answer_callback_quietly(call_id, text=alert_text(get_text("settings_host_url_expired")), show_alert=True)
		return
	answer_callback_quietly(call_id)
	_clear_cache("pending_host_url", user_id)
	same_daemon, refused = _host_url_check(host_id, pending.get("url", ""))
	result = refused or _apply_host_url(host_id, pending["url"], same_daemon)
	built = build_settings_host(host_id)
	if built is None:
		render_settings(chat_id, message_id, "hosts")
		return
	text, markup = built
	edit_message_text(f"{result}\n\n{text}", chat_id, message_id, reply_markup=markup)

def cancel_host_url(call_id, user_id, chat_id, message_id, host_id):
	"""Drops a URL waiting for confirmation, and goes back to the host."""
	if _pending_host_url(user_id, chat_id, message_id, host_id) is None:
		answer_callback_quietly(call_id, text=alert_text(get_text("settings_host_url_expired")), show_alert=True)
		return
	answer_callback_quietly(call_id)
	_clear_cache("pending_host_url", user_id)
	built = build_settings_host(host_id)
	if built is None:
		render_settings(chat_id, message_id, "hosts")
		return
	text, markup = built
	edit_message_text(text, chat_id, message_id, reply_markup=markup)

def schedules_on_host(host_id):
	"""
	The enabled schedules that would stop working without `host_id`.

	Only the host-scoped actions: a mute schedule is about the bot's own
	notifications and belongs to no machine. A task created before hosts
	existed carries none and means the local one, which cannot be removed
	anyway.
	"""
	local = host_registry.local_host_id()
	affected = []
	for schedule in schedule_manager.get_all_schedules():
		if not schedule.get("enabled", True):
			continue
		# Igual que en el ejecutor: get() con default no protege de un None
		# guardado, y una sola tarea así rompía la pantalla de quitar host.
		if (schedule.get("action") or "").lower() not in HOST_SCOPED_SCHEDULE_ACTIONS:
			continue
		if (schedule.get("host") or local) != host_id:
			continue
		affected.append(schedule)
	return affected


def disable_schedules(schedules):
	"""
	Turns off the given schedules, and returns the names it turned off.

	Disabled rather than deleted, which is what the executor already does with
	a task it cannot run: moving it to another host and switching it back on is
	a couple of taps, while a deleted cron expression is gone for good. Re-adding
	the machine from /settings is not enough, since it comes in under a new id.
	Left enabled, each would be switched off by its next firing anyway, with a
	notice per task at whatever hour its cron comes round.

	Takes the tasks already read rather than a host id, so the caller can look
	them up while the host still exists.
	"""
	names = []
	for schedule in schedules:
		name = schedule.get("name", "")
		if name and schedule_manager.update_schedule(name, enabled=False):
			names.append(name)
	return names


def build_settings_host_remove(host_id):
	"""Confirmation before dropping a host, since it takes its state with it."""
	entry = host_registry.host(host_id)
	if entry is None:
		return None
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_host_remove_confirm"),
									callback_data=f"settingsHostRemoveConfirm|{host_id}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data=f"settingsHost|{host_id}"))
	question = get_text("settings_host_remove_confirm", host_alias(host_id))
	# Whatever this takes down with it gets said before the tap, not after.
	# A schedule naming a host that no longer exists raises on every firing.
	affected = schedules_on_host(host_id)
	if affected:
		listed = "\n".join(f"· <b>{html.escape(s.get('name', ''))}</b>" for s in affected)
		question += get_text("settings_host_remove_schedules", listed)
	settings = len(store.container_settings().get(host_id) or {})
	if settings:
		question += get_text("settings_host_remove_containers", settings)
	return question, markup


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
	if screen == "telemetry":
		return build_settings_telemetry()
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

# How long a group's answer to "can the bot read loose text here" is trusted.
# Becoming admin is not something the bot hears about, so it has to expire.
_READS_GROUP_TEXT_TTL = 600
_reads_group_text = {}

def reads_loose_text(chat_id):
	"""
	Whether a message typed in `chat_id` without replying reaches the bot.

	Always in a private chat. In a group, only when BotFather's privacy mode is
	off or the bot is an administrator there: otherwise Telegram delivers it
	commands, buttons and replies to its own messages, and nothing else. A
	value typed as a plain message was lost without a trace, and the person
	was left waiting for an answer to something the bot never saw.

	Unknown counts as yes, so a failed lookup leaves the question as it was.
	"""
	try:
		chat_id = int(chat_id)
	except (TypeError, ValueError):
		return True
	if chat_id > 0:
		return True
	cached = _reads_group_text.get(chat_id)
	if cached and time.time() - cached[1] < _READS_GROUP_TEXT_TTL:
		return cached[0]
	try:
		me = bot_identity()
		reads = bool(getattr(me, "can_read_all_group_messages", False)) or \
			bot.get_chat_member(chat_id, me.id).status in ("administrator", "creator")
	except Exception as e:
		debug(f"Could not tell whether I read loose text in chat {chat_id}: {e}")
		return True
	if not reads:
		debug(f"Chat {chat_id}: I only see replies to my messages, so typed answers are asked for as replies")
	_reads_group_text[chat_id] = (reads, time.time())
	return reads

def typed_prompt(key, *args):
	"""
	A question answered by typing, with a note to reply to it when typing
	alone would not reach the bot.
	"""
	text = get_text(key, *args)
	if not reads_loose_text(get_reply_chat_id()):
		text += f"\n\n{get_text('reply_to_answer')}"
	return text

def ask_text_input(user_id, field, prompt_key, back_to="main", prompt_args=()):
	"""
	Asks for a value that has to be typed rather than picked.

	`back_to` names the settings screen to return to once answered, or is None
	for /mute, which is reached from the main menu and has nothing to go back
	to. `prompt_args` fill the prompt's placeholders.
	"""
	debug(f"Running command: ask_text_input({field}) for user {user_id}")
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelTextInput"))
	sent = send_message(message=typed_prompt(prompt_key, *prompt_args), reply_markup=markup)
	if sent:
		save_text_input_state(user_id, field, sent.message_id, back_to)

def save_text_input_state(user_id, field, delete_message_id, back_to="main"):
	_save_cache("pending_text_input", user_id, asked_here({
		"field": field,
		"deleteMessage": delete_message_id,
		"back_to": back_to,
	}))

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
			hours = as_interval_hours(raw)
		except ValueError:
			send_message(message=get_text("settings_invalid_interval"))
			return None
		store.set("bot.check_update_every_hours", hours)
		return get_text("settings_updated")

	if field == "host_add":
		alias_name, url = parse_host_definition(raw)
		# Each refusal is logged as well as answered: the answer goes to the
		# chat, and a /logfile sent to ask for help used to show nothing at all
		# about a host that would not add.
		if not url:
			debug(f"Rejected host definition [{raw}]: no URL in it")
			send_message(message=get_text("settings_host_invalid"))
			return None
		try:
			entry = host_registry.add_host(alias_name, url)
		except host_registry.HostRejected as e:
			# Nothing was tried: it is what was typed that cannot be registered.
			debug(f"Rejected host {url}: {e.reason}")
			key = ("settings_host_duplicate" if e.reason == "duplicate"
				   else "settings_host_bad_scheme")
			send_message(message=get_text(key, html.escape(url)))
			return None
		except host_registry.HostUnavailable as e:
			# Rejected at the point of adding, while the connection details are
			# still in front of the person typing them.
			debug(f"Could not add host {url}: {e.reason}")
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
		# Verify the bot can actually post there before saving it. Storing an
		# id it cannot post to would send every notification into the void,
		# with nothing in the interface to explain why. Reaching the chat was
		# not enough: a public channel answers to anyone, member or not, and
		# an administrator without the right to post can see it too. So a
		# message is posted, which also shows the channel it works.
		try:
			bot.send_message(raw, get_text("settings_channel_test"))
		except Exception as e:
			debug(f"Rejected notification channel {raw}: {e}")
			send_message(message=get_text("settings_channel_unreachable", html.escape(raw)))
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

def build_starting_message():
	"""
	What the bot says when it comes up.

	It used to report two of the eight settings — whether it checks for
	updates, and whether menus stay open for multi-selection — because those
	were the two environment variables 4.x had. With `/settings` in place that
	is just inheritance: a menu preference has no business in a boot report,
	and anyone can read the rest from the menu.

	What a boot report is for is the part you can only learn here, and the part
	most likely to be wrong after a change: whether it reached your machines.
	So the hosts are the body of it now.

	Reachability comes from the parallel snapshot with a deadline, not from
	listing each host in turn: a machine that is unplugged would otherwise hold
	the message — and with it the start of polling — for its whole timeout.
	"""
	lines = [f"🫡 <b>{html.escape(own_container_name() or 'docker-controller-bot')}</b>"
			f"  ·  <i>v{VERSION}</i>", ""]

	configured = host_registry.hosts()
	statuses = host_registry.status_snapshot()
	if len(configured) <= 1:
		# The golden rule: with one host there is no host level to speak of, so
		# this reads as it always did — what it can see, and nothing about
		# machines.
		try:
			containers = manager(host_registry.local_host_id()).list_containers()
			running = sum(1 for c in containers if c.status in ("running", "restarting"))
			lines.append(get_text("starting_containers", len(containers), running,
									len(containers) - running))
		except Exception as e:
			debug(f"Could not count containers at start-up: {e}")
			lines.append(get_text("starting_no_docker"))
	else:
		for entry in configured:
			ok, reason = statuses.get(entry["id"], (False, ""))
			if not ok:
				lines.append(get_text("starting_host_down", host_alias(entry["id"])))
				continue
			try:
				total = len(manager(entry["id"]).list_containers())
				lines.append(get_text("starting_host", host_alias(entry["id"]), total))
			except Exception as e:
				debug(f"Could not count containers on {entry['id']}: {e}")
				lines.append(get_text("starting_host_down", host_alias(entry["id"])))

	lines.append("")
	lines.extend(_migration_notices())

	# Said here because the bot has just come up with the user's hosts and
	# choices missing, and the log is not where anyone looks for why.
	unreadable = store.settings_unreadable()
	if unreadable:
		lines.append(get_text("starting_settings_unreadable", store.settings_path(), html.escape(unreadable)))
		lines.append("")
	if schedule_manager.set_aside:
		lines.append(get_text("starting_schedules_unreadable", schedule_manager.set_aside))
		lines.append("")

	# The one setting that belongs here: it is the only one that changes what
	# the bot does on its own while nobody is looking.
	if store.get("bot.check_updates"):
		lines.append(get_text("starting_updates_on",
								_format_interval(update_interval_hours())))
	else:
		lines.append(get_text("starting_updates_off"))
	lines.append(get_text("channel"))
	return "\n".join(lines)


def _migration_notices():
	"""
	What the user has to do to their docker-compose, as paragraphs of the
	start message, each followed by its blank line.

	Said here and not only in the log, because every one of them needs an edit
	only the compose's owner can make, and the log is not where anyone looks.
	The path comes first: it is the edit that matters, and cleaning up the
	variables can go in the same one.
	"""
	notices = []
	if not store.is_persistent():
		# Nothing survives a recreate, so this is the only thing worth saying:
		# the old path would not help either, and the settings file is seeded
		# again on every start, so "imported" would repeat on every one.
		notices.append(get_text("starting_not_persistent", store.CONFIG_ROOT))
	else:
		# With only /app/schedule mapped, /app/config is inside the image, so
		# copying the files there would put them somewhere the next recreate
		# deletes. The volume mapping is the migration.
		if store.uses_legacy_root():
			notices.append(get_text("starting_legacy_volume", store.LEGACY_ROOT, store.CONFIG_ROOT))
		if _migration.imported:
			notices.append(get_text("starting_env_imported"))
		ignored = [f"<code>{name}</code>" for name in _migration.ignored]
		if len(ignored) == 1:
			notices.append(get_text("starting_env_ignored", ignored[0]))
		elif ignored:
			notices.append(get_text("starting_env_ignored_many", ", ".join(ignored)))
	return [line for notice in notices for line in (notice, "")]


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


def bot_identity():
	"""
	The bot's own Telegram user, asked once.

	Every command compared against it, several times each, and every
	get_me() is a round trip to Telegram that cannot change the answer.
	"""
	global _bot_identity
	if _bot_identity is None:
		_bot_identity = bot.get_me()
	return _bot_identity

_bot_identity = None

def parse_command(text):
	"""
	Splits a command message into (command, addressee).

	By any whitespace, as telebot does when it matches the handler: split on
	spaces alone, "/logs" followed by a newline and a name came out as one
	word, which matched no command and was counted with the name inside.
	"""
	first = text.split(maxsplit=1)[0] if text and text.strip() else ""
	command, _, addressee = first.partition("@")
	return command, addressee

@bot.message_handler(commands=["start", "list", "run", "stop", "restart", "delete", "exec", "checkupdate", "updateall", "changetag", "logs", "logfile", "compose", "mute", "schedule", "settings", "info", "version", "donate", "donors", "prune", "ports"])
@with_reply_context
def command_controller(message):
	userId = message.from_user.id
	comando, addressee = parse_command(message.text)
	# Telebot matches "/stop@AnotherBot" too, since it drops the suffix. In a
	# group with several bots that one is for someone else, and 4.x ignored it.
	if addressee and addressee.lower() != (bot_identity().username or "").lower():
		debug(f"Command {comando}@{addressee} ignored: it is for another bot")
		return
	if not is_allowed_origin(message.chat, userId):
		debug(f"Command {comando} ignored: chat {message.chat.id} is neither an administrator private chat nor TELEGRAM_GROUP")
		return

	messageId = message.id
	message_thread_id = topic_of(message)
	debug(f"COMMAND: {comando} | USER: {userId} | CHAT: {message.chat.id} | THREAD: {message_thread_id}")

	# The topic filter only applies to groups: a private chat with the bot has no
	# topics, so commands sent there must always be accepted
	if message.chat.type != "private" and message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot_identity().id):
		return

	if not is_admin(userId):
		tell_not_admin(userId, message.from_user.username)
		return

	container_id = None
	container_name = None
	# Only the commands that act on a container look one up: the argument is
	# searched for on every host, which is wasted on /list or /prune. And only
	# once the sender is known to be an administrator: before, anyone in the
	# group typing a name two hosts share got the picker, hosts and all.
	if comando in COMMAND_PICKERS:
		argument = " ".join(message.text.split()[1:])
		if argument:
			# Searched across every host. Names rarely repeat between machines,
			# and when they do the user is asked rather than guessed at.
			container_id, container_name, candidates = resolve_container_argument(argument)
			if candidates:
				send_container_disambiguation(COMMAND_PICKERS[comando], container_name, candidates)
				return
			if container_id:
				debug(f"Argument {argument!r} resolved to {container_id}")

	# /start is the menu itself. Everything else goes through the table, so a
	# typed command and its button in that menu run the same function.
	if comando == "/start":
		count_usage("cmd_start")
		send_start_menu()
		return

	action = COMMAND_ACTIONS.get(comando)
	if action is None:
		debug(f"No action registered for {comando}")
		return
	# Counted by the table entry and never by what was typed: only a name the
	# table knows can reach the statistics.
	count_usage(f"cmd_{comando.lstrip('/')}")
	delete_message(messageId)

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


def _host_did_not_answer(call, spec, host_id, why):
	"""Says a button's host did not answer, the message kept to try again."""
	if not spec.answer_immediately:
		answer_callback_quietly(call.id)
	send_message(message=get_text("host_unreachable", host_alias(host_id), html.escape(why)))


def _container_is_gone(call, spec, chat_id, message_id, shown_name, host_id):
	"""
	Says a button's container no longer exists.

	A message that stays after its presses —a list of updates, an /info—
	stays here too, and the answer goes on the button: the list still holds
	the rest of its containers, and the one gone is left out when it is
	confirmed. Deleting it took them all away, and left the press without
	an answer. Any other message goes, as it always did.
	"""
	text = f'{get_text("container_does_not_exist", shown_name)}{host_suffix(host_id)}'
	if spec.keeps_message and not spec.answer_immediately:
		answer_callback_quietly(call.id, text=_fit_alert(alert_text(text)), show_alert=True)
		return
	if spec.keeps_message:
		send_message(message=text)
		return
	close_multi_action_menu(chat_id, message_id)
	send_message(message=text)


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
			tell_not_admin(userId, call.from_user.username)
			answer_callback_quietly(call.id, text="❌")
			return
		# Only where commands are taken too. No message with buttons is sent
		# anywhere else, but one left behind is enough: in a group TELEGRAM_GROUP
		# used to be, its buttons still worked, and answered there.
		if not is_allowed_origin(call.message.chat, userId):
			debug(f"Button ignored: chat {chatId} is neither an administrator private chat nor TELEGRAM_GROUP")
			answer_callback_quietly(call.id, text="❌")
			return

		spec, args = callback_registry.parse(call.data)
		# The registered name, never the arguments: those name containers.
		count_usage(f"btn_{spec.name}")
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
				ctx.containerName = load_container_name(chatId, messageId, ctx.containerId)
			if not ctx.containerName:
				# An update list outlives the names saved with it, which go
				# a week after they were written: the list still says them.
				ctx.containerName = listed_update_name(chatId, messageId, ctx.containerId)
			if not ctx.containerName:
				ctx.containerName, silent = name_by_reference(ctx.containerId)
				if silent is not None:
					# The host did not answer, which says nothing about the
					# container: said so, and the message kept to try again.
					# Taken for a container that does not exist, it got its
					# id in place of a name, and its message deleted.
					_host_did_not_answer(call, spec, ctx.hostId, silent)
					return
				if not ctx.containerName:
					_container_is_gone(call, spec, chatId, messageId, ref_id(ctx.containerId), ctx.hostId)
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
			current_id, silent = locate_container_on_host(ctx.hostId, ctx.containerName)
			if current_id:
				if current_id != ref_id(ctx.containerId):
					debug(f"Resolved stale id {ctx.containerId} to {current_id} via name {ctx.containerName} on {ctx.hostId}")
				ctx.containerId = make_ref(ctx.hostId, current_id)
			elif silent is not None:
				# The host did not answer, which says nothing about the container.
				# Saying it did not exist, and deleting the message —the list of
				# updates, the only way back into them— was wrong on both counts.
				_host_did_not_answer(call, spec, ctx.hostId, silent)
				return
			else:
				# Not looked for again by its id: the reference carries five
				# characters of it, which a container created since can share,
				# and the action —a stop, a delete— would land on that one.
				_container_is_gone(call, spec, chatId, messageId, ctx.containerName, ctx.hostId)
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
			delete_companions(chatId, messageId)

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
	message_thread_id = topic_of(message)

	if not is_allowed_origin(message.chat, userId):
		debug(f"Message ignored: chat {message.chat.id} is neither an administrator private chat nor TELEGRAM_GROUP")
		return

	# The topic filter only applies to groups: a private chat with the bot has no
	# topics, so messages sent there must always be accepted
	if message.chat.type != "private" and message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot_identity().id):
		return

	# Loose text gets no answer from the bot unless it comes from an
	# administrator: in the group that is every member's every message, and
	# from anyone at all in private, each answer queued with the notifications.
	if not is_admin(userId):
		debug(f"Message from {userId} ({username}) ignored: not an administrator")
		return

	chat_id = message.chat.id
	pending = answerable(load_command_request_state(userId), chat_id)
	pending_port_check = answerable(load_port_check_request_state(userId), chat_id)
	pending_input = answerable(load_text_input_state(userId), chat_id)
	schedule_state = answerable(load_schedule_state(userId), chat_id)

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
			# The host the question was asked about, not whichever one the
			# bot happens to run on.
			is_available, result_message = check_specific_port(
				port_number, pending_port_check.get("host"))
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
		elif field.startswith("host_url:"):
			receive_host_url(userId, field.split(":", 1)[1], raw_value)
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
	label = host_label(ref_host(containerId))
	x = send_message(message=f"{label}{get_text(config['message_key'], containerName)}")
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
		# The methods build their errors without a host, having only the short
		# id: this is where the host is known, so it is where it gets said.
		send_message(message=f"{label}{result}")
	return result

def run(containerId, containerName, from_schedule=False):
	return _execute_container_action('run', containerId, containerName, from_schedule)

def stop(containerId, containerName, from_schedule=False):
	return _execute_container_action('stop', containerId, containerName, from_schedule)

def restart(containerId, containerName, from_schedule=False):
	return _execute_container_action('restart', containerId, containerName, from_schedule)

def without_the_bot(host_id, containers):
	"""
	A project's containers split into the rest and the bot's own.

	Stopping, restarting or deleting a whole project went through every
	container in it, the bot included: a restart stopped the bot halfway and
	never came back to start the services it had already stopped, and a delete
	removed it. Picked alone, the bot is already refused; in a project it is
	left out and the admin is told.
	"""
	rest, own = [], []
	for container in containers:
		(own if is_own_container(host_id, container.id, container.name) else rest).append(container)
	return rest, own


def _tell_bot_skipped(host_id, own):
	for container in own:
		send_message(message=f'{get_text("project_bot_skipped", html.escape(container.name))}{host_suffix(host_id)}')


def _execute_compose_project_action(action, project_name, show_extended=True, host_id=None):
	"""
	Generic function to execute compose project actions (run, stop, restart).

	Args:
		action: Action name ('run', 'stop', 'restart')
		project_name: Project name
		show_extended: Whether to show extended messages
	"""
	debug(f"Running command: {action}_compose_project for project {project_name}")

	host_id = host_id or host_registry.local_host_id()
	# Every message this sends is about one machine's project, and a project
	# name says nothing about which: two hosts can run the same stack.
	label = host_label(host_id)

	# Get project information, on the host the project lives on. A broad except
	# because a host can build its client and fail on the next call.
	try:
		owner = manager(host_id)
		project_info = owner.get_project_info(project_name)
	except Exception as e:
		debug(f"Could not read project {project_name}: {e}")
		project_info = None
	if not project_info:
		send_message(message=f'{label}{get_text("error_project_not_found", html.escape(str(project_name)))}')
		return

	# Get containers sorted by dependencies
	containers = project_info.containers
	sorted_containers = owner.compose_manager.sort_containers_by_dependencies(containers)
	own = []
	if action in ('restart', 'stop'):
		sorted_containers, own = without_the_bot(host_id, sorted_containers)

	# Per-action configuration
	if action == 'restart':
		progress = send_message(message=f'{label}{get_text("restarting_project", html.escape(str(project_name)))}')
		# Stop containers in reverse order
		for container in reversed(sorted_containers):
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=f'{label}{get_text("stopping_service", html.escape(str(service_name)))}')
			try:
				stop_container(container)
			except Exception as e:
				debug(f"Error stopping {service_name}: {e}")
				if show_extended:
					send_message(message=f'{get_text("error_stopping_service", html.escape(str(service_name)))}{host_suffix(host_id)}')
		# Start containers in the correct order
		for container in sorted_containers:
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=f'{label}{get_text("starting_service", html.escape(str(service_name)))}')
			try:
				container.start()
			except Exception as e:
				debug(f"Error starting {service_name}: {e}")
				if show_extended:
					send_message(message=f'{get_text("error_starting_service", html.escape(str(service_name)))}{host_suffix(host_id)}')
		# The progress line goes when the work is done, as it does for a single container
		if progress:
			delete_message(progress.message_id)
		send_message(message=f'{get_text("project_restarted_success", html.escape(str(project_name)))}{host_suffix(host_id)}')
		_tell_bot_skipped(host_id, own)

	elif action == 'run':
		progress = send_message(message=f'{label}{get_text("starting_project", html.escape(str(project_name)))}')
		# Start containers in the correct order
		for container in sorted_containers:
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=f'{label}{get_text("starting_service", html.escape(str(service_name)))}')
			try:
				container.start()
			except Exception as e:
				debug(f"Error starting {service_name}: {e}")
				if show_extended:
					send_message(message=f'{get_text("error_starting_service", html.escape(str(service_name)))}{host_suffix(host_id)}')
		if progress:
			delete_message(progress.message_id)
		send_message(message=f'{get_text("project_started_success", html.escape(str(project_name)))}{host_suffix(host_id)}')

	elif action == 'stop':
		progress = send_message(message=f'{label}{get_text("stopping_project", html.escape(str(project_name)))}')
		# Stop containers in reverse order
		for container in reversed(sorted_containers):
			service_name = container.labels.get('com.docker.compose.service', container.name)
			if store.get("bot.extended_messages") and show_extended:
				send_message(message=f'{label}{get_text("stopping_service", html.escape(str(service_name)))}')
			try:
				stop_container(container)
			except Exception as e:
				debug(f"Error stopping {service_name}: {e}")
				if show_extended:
					send_message(message=f'{get_text("error_stopping_service", html.escape(str(service_name)))}{host_suffix(host_id)}')
		if progress:
			delete_message(progress.message_id)
		send_message(message=f'{get_text("project_stopped_success", html.escape(str(project_name)))}{host_suffix(host_id)}')
		_tell_bot_skipped(host_id, own)

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


def _compute_namespace_overrides(dep_container, old_parent_id, new_parent_id, start=True):
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
		overrides['is_running'] = start
	return overrides


# Dependents being stopped and started again after an update, which the
# shutdown waits for as it does for the update itself.
_dependents_restarting = 0
_dependents_lock = threading.Lock()


def dependents_in_progress():
	with _dependents_lock:
		return _dependents_restarting


def _counted_as_dependents_work(function):
	"""Marks a function whose work the shutdown has to wait for."""
	@functools.wraps(function)
	def wrapper(*args, **kwargs):
		global _dependents_restarting
		with _dependents_lock:
			_dependents_restarting += 1
		try:
			return function(*args, **kwargs)
		finally:
			with _dependents_lock:
				_dependents_restarting -= 1
	return wrapper


@_counted_as_dependents_work
def restart_dependents_after_update(project_name, updated_service_name, new_parent_container=None, old_parent_id=None, send_fn=None, host_id=None, hold_events=False, running_before=None):
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
		hold_events: When True, the dependents' start/stop events are not
			announced either — an update batch reports everything in one
			summary, and they would be most of the noise it is avoiding.
	"""
	if send_fn is None:
		send_fn = lambda msg: send_message(message=msg)

	debug(f"Restarting dependents of service {updated_service_name} in project {project_name}")

	# Get project information, on the host the updated container lives on
	owner = manager(host_id or host_registry.local_host_id())
	project_info = owner.get_project_info(project_name)
	if not project_info:
		send_fn(get_text("error_project_not_found", html.escape(str(project_name))))
		return

	# Get only the transitive dependents of the updated service
	dependents = owner.compose_manager.get_transitive_dependents(
		project_info.containers, updated_service_name
	)

	# Never the bot itself: stopping it to start it again is the bot ending
	# its own process halfway through, and nobody is left to do the start —
	# `unless-stopped` does not bring back a container stopped on purpose.
	# The docker-socket-proxy it depends on is exactly this case. It needs no
	# restart either way: it reconnects to the proxy on its own.
	dependents = [c for c in dependents if not is_own_container(owner.host_id, c.id, c.name)]

	if not dependents:
		debug(f"No dependents found for service {updated_service_name}, nothing to restart")
		return

	if not hold_events:
		_restart_dependents(owner, dependents, updated_service_name, new_parent_container, old_parent_id, send_fn, running_before)
		return
	# The ones recreated for sharing the parent's namespace go through the
	# same rename to _old as an update does.
	names = [c.name for c in dependents] + [f"{c.name}_old" for c in dependents]
	hold_container_events(owner.host_id, names)
	try:
		_restart_dependents(owner, dependents, updated_service_name, new_parent_container, old_parent_id, send_fn, running_before)
	finally:
		release_container_events(owner.host_id, names)


def _restart_dependents(owner, dependents, updated_service_name, new_parent_container, old_parent_id, send_fn, running_before=None):
	"""
	The body of restart_dependents_after_update, once the dependents are known.

	`running_before` is the set of ids that were running before the update;
	None when that could not be read, and then every dependent counts as
	running, as it always did. A dependent the user had stopped used to be
	started along with the rest: it is left as it was now, except that one
	sharing the parent's namespace is still recreated to point at the new
	parent — stopped — so that starting it later does not fail.
	"""
	def was_running(container):
		return running_before is None or container.id in running_before

	new_parent_id = new_parent_container.id if new_parent_container is not None else None
	namespace_overrides = {}
	for container in dependents:
		overrides = _compute_namespace_overrides(container, old_parent_id, new_parent_id,
												start=was_running(container))
		if overrides:
			namespace_overrides[container.id] = overrides
	dependents = [c for c in dependents if was_running(c) or c.id in namespace_overrides]
	if not dependents:
		return
	dependent_count = len(dependents)

	# Prefer the parent's container name (what shows up in `docker ps`) over
	# the compose service name when addressing the user; keep the service
	# name in debug logs for compose-level traceability.
	parent_display_name = new_parent_container.name if new_parent_container is not None else updated_service_name

	# Initial message
	send_fn(get_text("restarting_dependent_services", parent_display_name, dependent_count))

	# Which dependents need full recreation because they share a namespace
	# with the (now-replaced) parent container id was worked out above. Those
	# are NOT stopped here; recreate_with_overrides handles their lifecycle.

	# Stop dependents in reverse order (deepest dependents first), skipping
	# those that will be fully recreated. Per-service stop progress is logged
	# only in debug; the Telegram user gets a single summary at the end (and
	# explicit error messages if a stop fails).
	for container in reversed(dependents):
		if container.id in namespace_overrides:
			continue
		service_name = container.labels.get('com.docker.compose.service', container.name)
		try:
			stop_container(container)
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
			# A one-shot job — a migration, an init — has exited, so its update
			# leaves it created and not started, the way it was found. Waiting
			# for that to exit waited the full three minutes with the
			# dependents down, and the job never ran on its new image. Running
			# it is what `service_completed_successfully` asks for.
			try:
				new_parent_container.reload()
				if new_parent_container.status in ("created", "exited"):
					debug(f"Running {updated_service_name} so its dependents can wait for it to complete")
					new_parent_container.start()
			except Exception as e:
				debug(f"Could not start {updated_service_name} before its dependents: {e}")
			debug(f"Waiting for {updated_service_name} to exit successfully before starting dependents")
			if not _wait_for_container_exit_success(new_parent_container, timeout_seconds=180):
				debug(f"{updated_service_name} did not complete successfully; starting its dependents anyway")

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


def perform_container_update(container_id, container_name, tag=None, send_fn=None, hold_events=False):
	"""
	Single entry point for container updates. Wraps the full flow:
	  1. Capture Compose project/service info BEFORE the update (container is recreated).
	  2. Send the "updating" progress message via send_fn.
	  3. Delegate the actual update to the container's own manager.
	  4. Send the final result message via send_fn.
	  5. If the container belongs to a Compose project, restart only the services
	     that depend on it (directly or transitively).

	Every update ends up here, so the behaviour is identical regardless of
	origin. The triggers themselves (/update, /changetag, the auto-update
	daemon, /updateall) go through update_container or update_containers,
	which decide how the update is reported.

	Args:
		container_id: Container ID to update.
		container_name: Container name (used in user-facing messages).
		tag: Optional new image tag (used by the /changetag flow).
		send_fn: Function used to send user-facing messages. Receives the message
			text and returns the sent telegram Message (or None to suppress).
			If None, defaults to send_message (admin chat).
		hold_events: When True, the start/stop events of this container and
			its dependents are not announced: an update batch reports the
			outcome in one summary instead.

	Returns:
		UpdateOutcome: whether it worked, what it said, and the versions it
		went from and to. Unpacks as (ok, result).
	"""
	if send_fn is None:
		send_fn = lambda msg: send_message(message=msg)

	# Capture Compose info BEFORE the update, since the container object is recreated.
	project_name = None
	updated_service_name = None
	old_parent_id = None
	container_obj = None
	# The host comes from the reference and is used for everything below: the
	# container, its update and its project's dependents all live on the same
	# machine, and resolving it once keeps them from drifting apart.
	host_id = ref_host(container_id)
	owner = manager(host_id)
	try:
		try:
			container_obj = owner.client.containers.get(ref_id(container_id))
		except docker.errors.NotFound:
			# Recreated since the reference was taken, under the same name: an
			# update batch that updates `vpn` recreates `app` behind it, and
			# `app` is next in the batch with the id it had before. The name
			# is unique on its host and survives a recreation, so it finds the
			# container the reference meant.
			container_obj = owner.client.containers.get(container_name)
			container_id = container_ref(host_id, container_obj)
			debug(f"{container_name} was recreated since it was listed; updating it by name")
		project_name = ComposeDetector.get_project_name(container_obj)
		updated_service_name = ComposeDetector.get_service_name(container_obj)
		old_parent_id = container_obj.id
	except Exception as e:
		debug(f"Could not pre-fetch Compose info for {container_name}: {e}")

	# What was running before anything is touched: the dependents restarted
	# afterwards are those, and not the ones the user had stopped. Listed
	# sparse — the state is in the listing itself, without an inspect each.
	running_before = None
	try:
		running_before = {c.id for c in owner.client.containers.list(sparse=True)
							if c.status in ("running", "restarting")}
	except Exception as e:
		debug(f"Could not list what was running before updating {container_name}: {e}")

	old_version = running_version(host_id, container_obj) if container_obj is not None else None

	if container_obj is not None and bot_shares_namespace_with(owner, container_obj):
		warning(f"Not updating {container_name}: the bot shares its namespace")
		result = get_text("error_update_bot_shares_namespace", container_name)
		send_fn(f"{host_label(host_id)}{result}")
		return UpdateOutcome(False, result)

	old_started_at = None
	if container_obj is not None:
		old_started_at = ((container_obj.attrs or {}).get("State") or {}).get("StartedAt")
	plan = _UpdatePlan(owner=owner, host_id=host_id, reference=container_id, name=container_name, tag=tag,
						project=project_name, service=updated_service_name, old_id=old_parent_id,
						running_before=running_before, old_version=old_version, old_started_at=old_started_at)
	if not hold_events:
		return _perform_container_update(plan, send_fn, False)
	# The old container is renamed before the new one exists, and a rollback
	# can start it again under that name, so both are held.
	names = [container_name, f"{container_name}_old"]
	hold_container_events(host_id, names)
	try:
		return _perform_container_update(plan, send_fn, True)
	finally:
		release_container_events(host_id, names)


@dataclass
class UpdateOutcome:
	"""
	What an update did: whether it worked, the message saying so, and the
	versions it went from and to, None when the image does not say.

	Unpacks as (ok, result), the pair perform_container_update always
	returned, so callers that want only that keep reading it that way. A
	batch summary wants the versions too, and reads them off here.
	"""
	ok: bool
	result: str
	old_version: Optional[str] = None
	new_version: Optional[str] = None

	def __iter__(self):
		return iter((self.ok, self.result))


@dataclass
class _UpdatePlan:
	"""
	What is known about a container before its update touches it: where it
	lives, its Compose project and service, its id — the update replaces
	it —, what was running on its host, and the version it runs.
	"""
	owner: Any
	host_id: str
	reference: str
	name: str
	tag: Optional[str]
	project: Optional[str]
	service: Optional[str]
	old_id: Optional[str]
	running_before: Optional[set]
	old_version: Optional[str]
	# When the container last started, to tell afterwards whether the update
	# ever stopped it.
	old_started_at: Optional[str] = None


def _perform_container_update(plan, send_fn, hold_events):
	"""The body of perform_container_update, once the plan is drawn up."""
	owner, host_id, container_name = plan.owner, plan.host_id, plan.name
	# Send the initial "updating" progress message
	# Both the progress line and the result say which machine, the same as the
	# start/stop notifications do. Without it an update report on a multi-host
	# setup does not say where it happened.
	label = host_label(host_id)
	x = send_fn(f'{label}{get_text("updating", container_name)}')

	# Perform the actual update
	result = owner.update(container_id=ref_id(plan.reference), container_name=container_name, message=x, bot=bot, tag=plan.tag)

	# Remove the progress message and send the final result. The chat is taken
	# from the sent message itself: send_fn may target the notification channel
	# (auto-update) instead of the chat being used.
	if x is not None:
		delete_message(x.message_id, x.chat.id)
	# Un resultado que se lee como una frase lleva el host al final; uno que
	# acaba en otra frase ("consulta los logs...") lo lleva delante, porque
	# una cláusula pegada después de eso queda mal. La comparación es contra
	# el mismo get_text que produce el éxito, así que no se desincroniza.
	ok = result == get_text("updated_container", container_name)

	# Resolve the freshly recreated container by name (Docker enforces unique
	# container names, and perform_update keeps the original name): it says
	# which version the update landed on, and dependents can wait on its
	# healthcheck when their compose `depends_on` declared `service_healthy`.
	new_parent_container = None
	try:
		new_parent_container = owner.client.containers.get(container_name)
	except Exception as e:
		debug(f"Could not fetch new container after update for {container_name}: {e}")

	outcome = UpdateOutcome(ok, result)
	if ok:
		outcome.old_version = plan.old_version
		outcome.new_version = running_version(host_id, new_parent_container) if new_parent_container is not None else None
		send_fn(update_result_text(host_id, container_name, outcome.old_version, outcome.new_version))
	else:
		send_fn(f"{label}{result}")

	# Restart dependents if applicable: only when the update got as far as
	# stopping the container they hang on. A refused one —the same container
	# already being updated, a double tap— or one whose pull failed left it
	# running as it was, and restarting them anyway recreated them against
	# the old container in the middle of another update.
	if plan.project and plan.service and (ok or not _parent_untouched(plan, new_parent_container)):
		restart_dependents_after_update(
			plan.project,
			plan.service,
			new_parent_container=new_parent_container,
			old_parent_id=plan.old_id,
			send_fn=send_fn,
			host_id=host_id,
			hold_events=hold_events,
			running_before=plan.running_before,
		)
	if ok:
		_recreate_namespace_sharers(owner, plan.old_id, new_parent_container, send_fn, hold_events, plan.running_before)
	return outcome


def _parent_untouched(plan, new_parent_container):
	"""Whether a failed update left the container exactly as it was: never stopped."""
	if new_parent_container is None or new_parent_container.id != plan.old_id or not plan.old_started_at:
		return False
	state = (new_parent_container.attrs or {}).get("State") or {}
	return state.get("StartedAt") == plan.old_started_at and state.get("Running", True)


@_counted_as_dependents_work
def _recreate_namespace_sharers(owner, old_parent_id, new_parent_container, send_fn, hold_events, running_before=None):
	"""
	Recreates whatever still shares a namespace with the container just replaced.

	`--network container:vpn`, outside any Compose project — gluetun and the
	apps routed through it, as often as not. Only Compose dependents used to be
	looked after, so these kept running inside the network namespace of a
	container that no longer existed: alive, and cut off from the new one. And
	on their next restart they failed, pointing at an id that was gone.

	Runs after the Compose dependents, which by then point at the new id, so
	what is left matching the old one is exactly what nobody has handled.
	"""
	if not old_parent_id or new_parent_container is None or new_parent_container.id == old_parent_id:
		return
	old_refs = {f"container:{old_parent_id}", f"container:{old_parent_id[:12]}"}
	try:
		containers = owner.client.containers.list(all=True)
	except Exception as e:
		debug(f"Could not list containers to find namespace sharers: {e}")
		return
	sharers = [c for c in containers
				if any(((c.attrs.get('HostConfig') or {}).get(field) or '') in old_refs
						for field, _ in _NAMESPACE_HOSTCONFIG_FIELDS)]
	if not sharers:
		return
	names = [c.name for c in sharers] + [f"{c.name}_old" for c in sharers]
	if hold_events:
		hold_container_events(owner.host_id, names)
	try:
		for container in sharers:
			overrides = _compute_namespace_overrides(
				container, old_parent_id, new_parent_container.id,
				start=running_before is None or container.id in running_before)
			if not overrides:
				continue
			debug(f"Recreating {container.name} to follow {new_parent_container.name} into its new namespace")
			if store.get("bot.extended_messages"):
				send_fn(get_text("recreating_namespace_dependent", container.name))
			result = owner.recreate_with_overrides(container.id, container.name, overrides)
			if result != get_text("updated_container", container.name):
				send_fn(get_text("error_recreating_namespace_dependent", container.name))
	finally:
		if hold_events:
			release_container_events(owner.host_id, names)


# Where an update's own warnings go —the old container left behind, a
# namespace sharer that could not follow— when whoever runs the update says.
# They are sent from deep inside it, to the chat of a progress message that a
# summarised batch never has, so they went to the chat even from an automatic
# update and even while muted. A batch with somewhere of its own to report sets
# this for the length of each update; the update runs in that same thread.
_update_notify = contextvars.ContextVar("update_notify", default=None)


def update_notify(host_id, chat_id):
	"""What an update calls to warn about something on its own, on `host_id`."""
	route = _update_notify.get()
	if route is not None:
		return lambda text: route(f'{host_label(host_id)}{text}')
	return lambda text: send_message(chat_id=chat_id, message=f'{host_label(host_id)}{text}')


def _with_update_notify(send, update):
	"""Runs `update()` with its own warnings going to `send`, when there is one."""
	if send is None:
		return update()
	token = _update_notify.set(send)
	try:
		return update()
	finally:
		_update_notify.reset(token)


def auto_update_send(message):
	"""
	Where the automatic updates report: the notification channel, or nowhere
	while muted.

	Asked on every message rather than once per batch: a batch takes minutes,
	and a mute can start or end halfway through it.
	"""
	if is_muted():
		debug(f"Message [{message}] omitted because muted")
		return None
	return send_message_to_notification_channel(message=message)


def update_container(ref, name, tag=None, send=None):
	"""
	Updates one container, reporting it the way a batch does.

	One update without extended messages used to leave four messages behind:
	the result, and the "stopped", "created" and "started" the event monitor
	adds of its own. A batch already said all of that in one progress message
	and one summary, so a single update is now a batch of one.

	The bot's own container keeps its own messages, as it does in a batch: its
	update recreates the bot, and a summary would never arrive. So do extended
	messages, which are asked for precisely to see every step.

	`send` takes the text and returns the sent message, or None to stay
	quiet. By default, the chat being answered.
	"""
	if send is None:
		send = lambda msg: send_message(message=msg)
	# A new tag is a different image: what the old one runs says nothing.
	if tag is None and runs_latest_image(ref, name):
		send(update_result_text(ref_host(ref), name, up_to_date=True))
		return
	if store.get("bot.extended_messages") or is_own_container(ref_host(ref), ref_id(ref), name):
		perform_container_update(ref, name, tag=tag, send_fn=send)
		return
	_update_containers_quietly([(ref, name)], tag=tag, send=send)


def update_containers(targets, up_to_date=(), missing=(), send=None, report_up_to_date=True):
	"""
	Updates several containers, as (reference, name) pairs, in the order given.

	`up_to_date` and `missing`, as (host_id, name) pairs, are what was ticked
	but had nothing left to update, or could no longer be found. They are not
	updated, only reported with the rest, so the result accounts for every
	container that was asked for. A target already running its newest image
	joins `up_to_date` here, whichever way it got into the batch.

	With extended messages every step is reported as it happens, the same as
	updating one by one. Without them — the default — a batch across several
	hosts used to bury the chat: each update announces its progress and result,
	and the event monitor adds a "stopped", "created" and "started" of its own.
	So the batch keeps one progress message, holds those events back, and ends
	in a single summary naming what failed.

	The bot's own container always goes last, and on its own with its usual
	messages: updating it recreates the bot, which would cut the batch short
	and leave the summary unsent.

	`send` is where it all goes, as in update_container; None is the chat
	being answered. The automatic updates pass the notification channel, and
	no `report_up_to_date`: what turns out to be current already is left out
	of their summary rather than reported to someone who asked for nothing.

	Each update on its own guard, the way the summarised form has always had
	them: one host that stops answering halfway through a long batch must not
	take the updates after it, the bot's own included, down with it.
	"""
	reply = send or (lambda msg: send_message(message=msg))
	current = [t for t in targets if runs_latest_image(*t)]
	if report_up_to_date:
		up_to_date = list(up_to_date) + [(ref_host(ref), name) for ref, name in current]
	targets = [t for t in targets if t not in current]
	own = [t for t in targets if is_own_container(ref_host(t[0]), ref_id(t[0]), t[1])]
	others = [t for t in targets if t not in own]

	def guarded(ref, name):
		try:
			_with_update_notify(send, lambda: perform_container_update(ref, name, send_fn=send))
		except Exception as e:
			error(f"Could not update container {name}. Error: [{e}]")
			reply(update_result_text(ref_host(ref), name, ok=False))

	if store.get("bot.extended_messages"):
		for ref, name in others:
			guarded(ref, name)
		for host_id, name in up_to_date:
			reply(update_result_text(host_id, name, up_to_date=True))
		for host_id, name in missing:
			reply(update_result_text(host_id, name, ok=False))
	elif others or up_to_date or missing:
		_update_containers_quietly(others, up_to_date=up_to_date, missing=missing, send=send)

	for ref, name in own:
		guarded(ref, name)


def _batch_progress_text(index, total, host_id, name):
	"""
	The progress message of an update batch: where it is, on which machine,
	and what. One line each, so the machine does not have to be read off the
	front of the name — and with a single host that line is simply not there.
	"""
	# A batch of one has nothing to count: "(1/1)" only reads as noise.
	lines = [get_text("updating_batch", index, total) if total > 1 else get_text("updating_one")]
	if not host_registry.is_single_host():
		lines.append(f"🖥️ <b>{host_alias(host_id)}</b>")
	lines.append(f"🔄 <b>{html.escape(str(name))}</b>")
	return "\n".join(lines)


def update_result_text(host_id, name, old=None, new=None, ok=True, up_to_date=False):
	"""
	How the update of one container is reported, wherever it is reported
	from: the summary of an update, its result with extended messages on,
	and the bot announcing itself back from updating. Each had a text of its
	own, and changing how one looked left the others as they were.

	No counts — it worked or it did not —, laid out like every other list of
	updates: under its host, with its versions below.
	"""
	if up_to_date:
		head = get_text("updated_one_up_to_date")
	else:
		head = get_text("updated_one") if ok else get_text("updated_one_failed")
	return f"{head}\n{by_host([(host_id, container_line(name, old, new))])}"


def _update_containers_quietly(targets, tag=None, send=None, up_to_date=(), missing=()):
	"""
	The summarised form of update_containers, and of update_container.

	`tag` only makes sense with one target: it is the /changetag flow's.
	"""
	if send is None:
		send = lambda msg: send_message(message=msg)
	updated, failed = [], [(h, n, (None, None)) for h, n in missing]
	current = [(h, n, (None, None)) for h, n in up_to_date]
	# The count is of what was asked for: something already up to date is
	# not an update made, and leaving it out of the total hid it.
	total = len(targets) + len(failed) + len(current)
	progress = None
	for index, (ref, name) in enumerate(targets, start=1):
		host_id = ref_host(ref)
		text = _batch_progress_text(index, len(targets), host_id, name)
		if progress:
			edit_message_text(text, progress.chat.id, progress.message_id)
		else:
			progress = send(text)
		try:
			outcome = _with_update_notify(send, lambda: perform_container_update(
				ref, name, tag=tag, send_fn=lambda msg: None, hold_events=True))
		except Exception as e:
			error(f"Could not update container {name}. Error: [{e}]")
			outcome = UpdateOutcome(False, "")
		if outcome.ok:
			updated.append((host_id, name, (outcome.old_version, outcome.new_version)))
		else:
			failed.append((host_id, name, (None, None)))

	# Deleted and sent anew rather than edited into the summary: an edit makes
	# no sound, and the summary is the one message of the batch worth hearing.
	if progress:
		delete_message(progress.message_id, progress.chat.id)
	# The count alone left the chat asking which ones, so they are listed too.
	lines = lambda entries: by_host([(h, container_line(n, *v)) for h, n, v in entries])
	if total == 1:
		host_id, name, versions = (updated + failed + current)[0]
		summary = update_result_text(host_id, name, *versions, ok=bool(updated), up_to_date=bool(current))
	else:
		summary = get_text("updated_batch", len(updated), total)
		if updated:
			summary += "\n" + lines(updated)
		if failed:
			summary += get_text("updated_batch_failed") + "\n" + lines(failed)
		if current:
			summary += get_text("updated_batch_up_to_date") + "\n" + lines(current)
	# In pieces when long: a big fleet's batch, a line per container and its
	# versions, went past 4096 characters and the whole summary was lost.
	for piece in split_for_telegram(summary):
		send(piece)

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

	host_id = host_id or host_registry.local_host_id()
	# This deletes containers and there is no undo, so every message says which
	# machine it is deleting them from.
	label = host_label(host_id)

	# Get project information, on the host the project lives on
	try:
		project_info = manager(host_id).get_project_info(project_name)
	except Exception as e:
		debug(f"Could not read project {project_name}: {e}")
		project_info = None
	if not project_info:
		send_message(message=f'{label}{get_text("error_project_not_found", html.escape(str(project_name)))}')
		return

	# Get the project's containers, never the bot's own
	containers, own = without_the_bot(host_id, project_info.containers)
	container_count = len(containers)

	# Initial message
	send_message(message=f'{label}{get_text("deleting_project", html.escape(str(project_name)), container_count)}')

	# Delete each container
	for container in containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if store.get("bot.extended_messages"):
			send_message(message=f'{label}{get_text("deleting_service", html.escape(str(service_name)))}')
		try:
			container.remove(force=True)
			forget_container(host_id, container.name)
		except Exception as e:
			debug(f"Error deleting {service_name}: {e}")
			send_message(message=f'{label}{get_text("error_deleting_service", html.escape(str(service_name)))}')

	# Final message
	send_message(message=f'{get_text("project_deleted_success", html.escape(str(project_name)), container_count)}{host_suffix(host_id)}')
	_tell_bot_skipped(host_id, own)

def logs(containerId, containerName):
	debug(f"Running command: logs for container {containerName}")
	result = manager_for(containerId).show_logs(container_id=ref_id(containerId), container_name=containerName)
	send_message(message=result, reply_markup=create_simple_keyboard("button_close"))

def log_file(containerId, containerName):
	debug(f"Running command: log_file for container {containerName}")
	markup = create_simple_keyboard("button_delete")
	result = manager_for(containerId).show_logs_raw(container_id=ref_id(containerId), container_name=containerName)
	if result is None:
		send_message(message=get_text("error_showing_logs_container", containerName), reply_markup=markup)
		return
	text, cut = result
	fichero_temporal = get_temporal_file(text, f'logs_{containerName}')
	x = send_message(message=get_text("loading_file"))
	# The file stays in the chat long after the menu that asked for it, so
	# its caption is the only thing left saying which machine it came from.
	caption = f'{get_text("logs", containerName)}{host_suffix(ref_host(containerId))}'
	if cut:
		caption += f'\n{get_text("logs_truncated", LOGFILE_MAX_BYTES // (1024 * 1024))}'
	sent = send_document(document=fichero_temporal, reply_markup=markup, caption=caption)
	if x:
		delete_message(x.message_id)
	if not sent:
		# Said, rather than the loading message just going away.
		send_message(message=get_text("error_showing_logs_container", containerName), reply_markup=markup)

def get_temporal_file(data, fileName):
	fichero_temporal = io.BytesIO(data.encode('utf-8'))
	fecha_hora_actual = datetime.now()
	formato = "%Y.%m.%d_%H.%M.%S"
	fecha_hora_formateada = fecha_hora_actual.strftime(formato)
	fichero_temporal.name = f"{fileName}_{fecha_hora_formateada}.txt"
	return fichero_temporal

def mute(minutes, asked=False, extend_only=False):
	"""
	Mute the bot with thread-safe lock to prevent race conditions.

	`asked` is for a /mute someone typed or pressed: that one is always
	confirmed. Without extended messages it used to answer nothing at all,
	which reads as the command not having worked. A scheduled mute stays
	quiet unless extended messages are on, as it always has.

	`extend_only` is for a scheduled one, which only ever lengthens a
	silence: a half-hour task at 23:00 cut a /mute until 06:00 short at
	23:30.
	"""
	global _unmute_timer

	if minutes == 0:
		unmute(asked=asked)
		return

	# Use lock to prevent race conditions with unmute timer
	# Acotado aquí, que es por donde pasan tanto /mute como una programación
	# de mute: los minutos de una tarea guardada pueden ser cualquier cosa, y
	# un número absurdo mata el temporizador y deja al bot mudo sin retorno.
	try:
		minutes = max(0, min(int(minutes), MUTE_MAX_MINUTES))
	except (TypeError, ValueError):
		warning(f"Ignoring a mute of {minutes!r}: not a number of minutes")
		return

	with _mute_lock:
		if extend_only and _mute_until() >= time.time() + minutes * 60:
			debug(f"Mute of {minutes} minutes not applied: a longer one is running")
			return
		# Cancel any existing unmute timer
		if _unmute_timer is not None:
			_unmute_timer.cancel()
			_unmute_timer = None

		store.state_set("mute_until", time.time() + minutes * 60)
		debug(f"Bot muted for {minutes} minutes")
		if asked or store.get("bot.extended_messages"):
			if minutes == 1:
				send_message(message=get_text("muted_singular"))
			else:
				send_message(message=get_text("muted", minutes))
		# Daemon como todo lo demás que el bot arranca: sin eso el intérprete
		# espera a unirlo al salir, así que un /mute de una hora dejaba al
		# contenedor sin apagarse hasta que Docker lo mataba a SIGKILL.
		_unmute_timer = threading.Timer(minutes * 60, unmute)
		_unmute_timer.daemon = True
		_unmute_timer.start()

def unmute(asked=False):
	"""
	Unmute the bot with thread-safe lock to prevent race conditions.

	`asked` as in mute(): a /mute 0 is confirmed; the timer running out is
	only said with extended messages on.
	"""
	global _unmute_timer

	# Use lock to prevent race conditions with mute timer
	with _mute_lock:
		# Cancel any existing unmute timer
		if _unmute_timer is not None:
			_unmute_timer.cancel()
			_unmute_timer = None

		store.state_set("mute_until", 0)
		debug("Bot unmuted")
		if asked or store.get("bot.extended_messages"):
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
		_unmute_timer.daemon = True
		_unmute_timer.start()

def compose(containerId, containerName):
	debug(f"Running command: compose for container {containerName}")
	markup = create_simple_keyboard("button_delete")
	result = manager_for(containerId).get_docker_compose(container_id=ref_id(containerId), container_name=containerName)
	if result is not None:
		fichero_temporal = io.BytesIO(result.encode('utf-8'))
		fichero_temporal.name = "docker-compose.txt"
		x = send_message(message=get_text("loading_file"))
		send_document(document=fichero_temporal, reply_markup=markup,
					caption=f'{host_label(ref_host(containerId))}{get_text("compose", containerName)}')
		if x:
			delete_message(x.message_id)
	else:
		send_message(message=get_text("error_showing_compose_container", containerName), reply_markup=markup)

def _parse_docker_time(value):
	"""A timestamp from the Docker API as an aware datetime, or None for its zero."""
	if not value or str(value).startswith("0001-"):
		return None
	text = str(value).replace("Z", "+00:00")
	# Docker writes nanoseconds; Python reads up to microseconds.
	text = re.sub(r"(\.\d{6})\d+", r"\1", text)
	try:
		return datetime.fromisoformat(text)
	except ValueError:
		return None


def _local_time(moment):
	return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else None


def container_stats(container):
	"""
	One reading of a running container's usage, the way `docker stats` takes
	it — memory without the page cache it could give back —, or None.
	"""
	try:
		return _read_stats(container.stats(stream=False))
	except Exception as e:
		# A missing figure, or one in a shape this does not expect, costs the
		# resources line and not the whole of /info.
		debug(f"Statistics of {container.name} not available: {e}")
		return None


def _read_stats(stats):
	"""The figures container_stats reports, out of one stats sample."""
	result = {}
	cpu, precpu = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
	cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (precpu.get("cpu_usage") or {}).get("total_usage", 0)
	system_delta = (cpu.get("system_cpu_usage") or 0) - (precpu.get("system_cpu_usage") or 0)
	cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
	result["cpu"] = (cpu_delta / system_delta) * cpus * 100 if system_delta > 0 and cpu_delta > 0 else 0.0
	memory = stats.get("memory_stats") or {}
	if memory.get("usage") is not None:
		breakdown = memory.get("stats") or {}
		cache = breakdown.get("inactive_file", breakdown.get("total_inactive_file", 0)) or 0
		result["memory"] = max(memory["usage"] - cache, 0)
		result["memory_limit"] = memory.get("limit")
	networks = stats.get("networks")
	if isinstance(networks, dict):
		result["rx"] = sum(n.get("rx_bytes", 0) for n in networks.values())
		result["tx"] = sum(n.get("tx_bytes", 0) for n in networks.values())
	io = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive")
	if isinstance(io, list):
		result["read"] = sum(e.get("value", 0) for e in io if str(e.get("op", "")).lower() == "read")
		result["write"] = sum(e.get("value", 0) for e in io if str(e.get("op", "")).lower() == "write")
	result["pids"] = (stats.get("pids_stats") or {}).get("current")
	return result


def gather_container_info(owner, container):
	"""
	Everything /info shows about a container, as plain values.

	From what is already at hand: the inspect output, its image, one reading
	of its stats when it runs, the update cache, the schedules, and a sparse
	listing of the host to know who shares its network or depends on it.
	The one thing that leaves the host is checking that a release page
	exists, so the link never leads to a 404.
	"""
	host_id = owner.host_id
	attrs = container.attrs or {}
	config = attrs.get("Config") or {}
	host_config = attrs.get("HostConfig") or {}
	state = attrs.get("State") or {}
	labels = config.get("Labels") or {}
	now = datetime.now(timezone.utc)
	status = container.status
	info = {
		"name": container.name,
		"host": None if host_registry.is_single_host() else host_registry.alias(host_id),
		"own": is_own_container(host_id, container.id, container.name),
		"status": status,
		"health": (state.get("Health") or {}).get("Status"),
		"restarts": attrs.get("RestartCount", 0),
		# Twelve, as `docker ps` shows them: the five the buttons carry are an
		# internal shorthand, not something to read.
		"short_id": container.id[:12],
		"created": _local_time(_parse_docker_time(attrs.get("Created"))),
		"image": config.get("Image", ""),
	}

	# Since when it is in the state it is in.
	moment = _parse_docker_time(state.get("StartedAt") if status in ("running", "paused", "restarting") else state.get("FinishedAt"))
	if moment:
		info["since"] = _local_time(moment)
		info["since_seconds"] = (now - moment).total_seconds()
	if status in ("exited", "dead"):
		info["oom"] = bool(state.get("OOMKilled"))
		if state.get("ExitCode") is not None:
			info["exit_code_text"] = describe_exit_code(state.get("ExitCode"))
	policy = host_config.get("RestartPolicy") or {}
	if policy.get("Name") and policy["Name"] != "no":
		info["restart_policy"] = policy["Name"] + (f":{policy['MaximumRetryCount']}" if policy.get("MaximumRetryCount") else "")

	# The image: what it is, which version, and where to read about it.
	try:
		image = container.image
		image_attrs = image.attrs or {}
		info["image_digest"] = image.id.replace("sha256:", "")[:12]
		info["image_size"] = sizeof_fmt(image_attrs.get("Size", 0)) if image_attrs.get("Size") else None
		created = _parse_docker_time(image_attrs.get("Created"))
		info["image_created"] = created.astimezone().strftime("%Y-%m-%d") if created else None
		image_labels = (image_attrs.get("Config") or {}).get("Labels") or {}
	except Exception as e:
		debug(f"Could not read the image of {container.name}: {e}")
		image_labels = {}
	info["version"] = running_version(host_id, container)
	info["release_notes_url"] = release_notes_url(image_labels.get("org.opencontainers.image.source"), info["version"])
	info["registry_url"], info["registry_name"] = build_registry_url(info["image"])
	platform = container_platform(container)
	host_architecture = get_my_architecture(host_id)
	if platform and host_architecture:
		architecture = platform.split("/", 1)[1] if "/" in platform else platform
		if docker_architectures.get(architecture.split("/")[0], architecture.split("/")[0]) != host_architecture:
			info["foreign_platform"] = architecture
			info["host_architecture"] = host_architecture

	# Updates, and how the bot treats it.
	info["update_mode"], info["update_mode_label"] = update_mode(host_id, container.name, labels)
	# Ignored is never pending, here as in /updateall: see update_available.
	info["has_update"] = (False if info["update_mode"] == "ignore"
						  else read_container_update_status(info["image"], container.name, host_id))
	info["update_versions"] = store.update_versions(host_id, container.name)
	checked = store.update_checked_at(host_id, container.name)
	if checked:
		try:
			info["last_check_seconds"] = (datetime.now() - datetime.fromisoformat(checked)).total_seconds()
		except ValueError:
			pass
	info["schedules"] = [
		(task.get("name", ""), task.get("cron", ""))
		for task in schedule_manager.get_all_schedules()
		if task.get("enabled", True) and task.get("container") == container.name
		and (task.get("host") or host_registry.local_host_id()) == host_id]

	# What it uses, and what it is allowed to.
	if status == "running":
		info["stats"] = container_stats(container)
	if host_config.get("NanoCpus"):
		info["cpu_limit"] = host_config["NanoCpus"] / 1e9
	elif host_config.get("CpuQuota") and host_config.get("CpuPeriod"):
		info["cpu_limit"] = host_config["CpuQuota"] / host_config["CpuPeriod"]
	info["memory_limit"] = host_config.get("Memory") or None
	gpus = [r for r in host_config.get("DeviceRequests") or []
			if "gpu" in [cap for group in (r.get("Capabilities") or []) for cap in group]]
	if gpus:
		request = gpus[0]
		count = get_text("info_all") if request.get("Count") == -1 else (request.get("Count") or len(request.get("DeviceIDs") or []))
		info["gpu"] = ", ".join(str(part) for part in (request.get("Driver"), count) if part)
	info["privileged"] = bool(host_config.get("Privileged"))
	info["all_capabilities"] = "ALL" in [str(c).upper() for c in host_config.get("CapAdd") or []]

	# Where it is reachable, and who it shares that with.
	try:
		neighbours = owner.client.containers.list(all=True, sparse=True)
	except Exception as e:
		debug(f"Could not list the neighbours of {container.name}: {e}")
		neighbours = []
	names = {c.id: (c.attrs.get("Names") or [c.id[:12]])[0].lstrip("/") for c in neighbours}
	mode = host_config.get("NetworkMode") or ""
	if mode.startswith("container:"):
		target = mode.split(":", 1)[1]
		info["network_of"] = next((name for cid, name in names.items() if cid.startswith(target) or target.startswith(cid)), target[:12])
	elif mode in ("host", "none"):
		info["network_mode"] = mode
	else:
		info["networks"] = [(name, settings.get("IPAddress") or settings.get("GlobalIPv6Address") or "")
							for name, settings in ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).items()]
	refs = {f"container:{container.id}", f"container:{container.id[:12]}"}
	info["shared_by"] = sorted(names[c.id] for c in neighbours
								if ((c.attrs.get("HostConfig") or {}).get("NetworkMode") or "") in refs)
	ports = []
	bindings = ((attrs.get("NetworkSettings") or {}).get("Ports") if status == "running" else None) or host_config.get("PortBindings") or {}
	for container_port, host_bindings in bindings.items():
		for binding in host_bindings or []:
			host_ip = binding.get("HostIp") or ""
			published = binding.get("HostPort") or ""
			entry = f"{published} → {container_port}" if host_ip in ("", "0.0.0.0", "::") else f"{host_ip}:{published} → {container_port}"
			if entry not in ports:
				ports.append(entry)
	info["ports"] = ports

	# What it keeps.
	info["mounts"] = [(mount.get("Name") if mount.get("Type") == "volume" and mount.get("Name") and len(mount.get("Name")) != 64
						else ("tmpfs" if mount.get("Type") == "tmpfs" else mount.get("Source") or mount.get("Name") or "?"),
						mount.get("Destination", "?"), not mount.get("RW", True))
						for mount in attrs.get("Mounts") or []]
	# compose's short `tmpfs:` is not among the mounts.
	listed = {target for _, target, _ in info["mounts"]}
	info["mounts"] += [("tmpfs", target, False) for target in host_config.get("Tmpfs") or {} if target not in listed]

	# What it is part of.
	project = labels.get("com.docker.compose.project")
	if project:
		service = labels.get("com.docker.compose.service")
		info["compose_project"], info["compose_service"] = project, service
		info["depends_on"] = owner.compose_manager.get_service_dependencies(container)
		# A sparse listing has no `.labels`: they are in its raw attributes.
		def sparse_labels(c):
			return c.attrs.get("Labels") or {}
		info["dependents"] = sorted(
			sparse_labels(c).get("com.docker.compose.service") or names.get(c.id, "")
			for c in neighbours
			if sparse_labels(c).get("com.docker.compose.project") == project and c.id != container.id
			and service in [d.split(":")[0].strip() for d in
							sparse_labels(c).get("com.docker.compose.depends_on", "").split(",") if d.strip()])
	return info


def info(containerId, containerName):
	debug(f"Running command: info for container {containerName}")
	x = send_message(message=get_text("obtaining_info", containerName))
	result, possible_update, can_start, gathered = manager_for(containerId).get_info(container_id=ref_id(containerId), container_name=containerName)
	if x:
		delete_message(x.message_id)
	updates = None
	if gathered is not None:
		mode, label = gathered["update_mode"], gathered["update_mode_label"]
		updates = (mode, label, next_update_mode(ref_host(containerId), mode, gathered.get("image") or ""))
	sent = send_message(message=result, reply_markup=info_markup(containerId, possible_update, can_start, updates))
	if sent and gathered is not None:
		# Its buttons repaint this message, and each press would otherwise
		# ask the daemon for the name that is already known here.
		save_container_refs(sent.chat.id, sent.message_id, [(containerId, containerName)])
		# And what it said, to say it again with the new mode when its button
		# is pressed, without gathering it all a second time.
		write_cache_item(_info_key(sent.chat.id, sent.message_id), gathered)

def _info_key(chat_id, message_id):
	return f"info_{chat_id}_{message_id}"

def clear_info_cache(chat_id, message_id):
	delete_cache_item(_info_key(chat_id, message_id))

# The button of each update mode, by mode and by the letter a callback
# carries for it: "n" is None, the default, which announces what it finds.
UPDATE_MODE_BUTTONS = {None: "button_updates_notify", "auto": "button_updates_auto", "ignore": "button_updates_ignore"}
UPDATE_MODE_CODES = {"n": None, "a": "auto", "i": "ignore"}

def update_mode_code(mode):
	return next(code for code, value in UPDATE_MODE_CODES.items() if value == mode)

def info_markup(ref, has_update, can_start, updates):
	"""
	The buttons under a container's /info.

	What the update check does with it is a button rather than a line of the
	text, the way /settings shows its values: it says how it is set, and a
	tap moves it on to the next, as in the list in /settings. The button
	carries the mode it sets rather than "next", so a double tap lands once.

	`updates` is (mode, label, the mode a tap sets), or None for an /info that
	could not be gathered.
	"""
	markup = InlineKeyboardMarkup(row_width=1)
	if has_update:
		markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{ref}"))
	if can_start:
		markup.add(InlineKeyboardButton(get_text("button_start"), callback_data=f"run|{ref}"))
	if updates is not None:
		mode, label, following = updates
		markup.add(InlineKeyboardButton(
			get_text("button_info_updates", "🏷️" if label else "🤖", get_text(UPDATE_MODE_BUTTONS[mode])),
			callback_data=f"infoSetUpdates|{update_mode_code(following)}|{ref}"))
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	return markup

def live_info(ref, found=None):
	"""
	A container's /info buttons as it is now: (container, markup, mode,
	label, pending), or None when it is gone.

	From one inspect and the cache, without gathering /info again: that is a
	reading of its stats, a second or two, for buttons that only need to know
	whether it is stopped and has an update waiting. `found` is the
	(owner, container) a caller has just looked up, which spares the inspect.
	"""
	owner, container = found or find_container(ref)
	if container is None:
		return None
	host_id = owner.host_id
	mode, label = update_mode(host_id, container.name, container.labels)
	image = container_image(container)
	# Read as gather_container_info reads it, or the button could go while the
	# text above it still offers the update.
	pending = mode != "ignore" and read_container_update_status(image, container.name, host_id) is True
	markup = info_markup(ref, pending, container.status in ("exited", "dead", "created"),
						 (mode, label, next_update_mode(host_id, mode, image)))
	return container, markup, mode, label, pending

def live_info_markup(ref):
	"""The /info buttons of a container as it is now, or None when it is gone."""
	state = live_info(ref)
	return state[1] if state else None

def repaint_info(call_id, chat_id, message_id, ref, alert=None, found=None):
	"""
	Repaints an /info once its update button was pressed: its buttons, and
	its text too, from what /info gathered, so both agree on whether an
	update is waiting —choosing "do not check" takes it away from both—, on
	the versions it would bring and on when it was last checked.
	"""
	state = live_info(ref, found)
	if state is None:
		answer_callback_quietly(call_id, text=_fit_alert(alert), show_alert=bool(alert))
		return
	container, markup, mode, label, pending = state
	# Under the reference the buttons now carry: pressed after the container
	# is recreated again, the name is still known, and found on its host as
	# it was this time. Without it, that press said it did not exist.
	save_container_refs(chat_id, message_id, [(ref, container.name)])
	gathered = read_cache_item(_info_key(chat_id, message_id))
	if not isinstance(gathered, dict):
		repaint_markup(call_id, chat_id, message_id, markup, alert)
		return
	host_id = ref_host(ref)
	gathered.update(update_mode=mode, update_mode_label=label, has_update=pending,
					update_versions=store.update_versions(host_id, container.name))
	gathered.pop("last_check_seconds", None)
	checked = store.update_checked_at(host_id, container.name)
	if checked:
		try:
			gathered["last_check_seconds"] = (datetime.now() - datetime.fromisoformat(checked)).total_seconds()
		except ValueError:
			pass
	write_cache_item(_info_key(chat_id, message_id), gathered)
	edit_message_text(render_container_info(gathered), chat_id, message_id, reply_markup=markup)
	answer_callback_quietly(call_id, text=_fit_alert(alert), show_alert=bool(alert))

ALERT_MAX_LENGTH = 200

def _fit_alert(alert):
	"""
	Telegram refuses an alert over 200 characters, and the tap then shows
	nothing at all. A container name is the part that can run long.
	"""
	if alert and len(alert) > ALERT_MAX_LENGTH:
		return alert[:ALERT_MAX_LENGTH - 1] + "…"
	return alert

def repaint_markup(call_id, chat_id, message_id, markup, alert=None):
	"""
	Puts `markup` on a message straight away and answers the press, with an
	alert when there is something to say.

	Synchronous, like the update toggles, so the tap shows its effect before
	the next one. Telegram refuses an identical keyboard ("message is not
	modified"), which a second tap on the same choice asks for: nothing to
	report.
	"""
	try:
		edit_message_reply_markup_sync(chat_id, message_id, reply_markup=markup)
	except Exception as e:
		debug(f"Could not repaint the buttons of message {message_id}: {e}")
	answer_callback_quietly(call_id, text=_fit_alert(alert), show_alert=bool(alert))

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
	question = get_text("confirm_delete", containerName)
	if store.container_update_mode(ref_host(containerId), containerName):
		question += get_text("confirm_delete_settings")
	send_message(message=f'{host_label(ref_host(containerId))}{question}', reply_markup=markup)

def forget_container(host_id, name):
	"""
	Forgets what the bot keeps about a container deleted from the bot.

	Straight away, without the wait the cleanup after each check gives the
	ones that disappear on their own: removing it from here says it is not
	coming back. Its update state too, which nothing else ever cleared and
	would have been taken up by the next container given its name.
	"""
	store.forget_container_settings(host_id, name)
	store.forget_update_status(host_id, name)

def ask_command(userId, containerId, containerName):
	debug(f"Running command: ask_command for container {containerName}")
	markup = create_simple_keyboard("button_cancel", "cancelAskCommand")
	x = send_message(message=f'{host_label(ref_host(containerId))}{typed_prompt("prompt_enter_command", containerName)}',
					reply_markup=markup)
	if x:
		save_command_request_state(userId, containerId, containerName, x.message_id)

def confirm_execute_command(containerId, containerName, command):
	debug(f"Running command: confirm_exec for container {containerName} with command [{command}]")
	commandId = save_command_cache(command)
	markup = create_confirm_cancel_keyboard(f"exec|{containerId}|{commandId}", "button_confirm", f"cancelExec|{commandId}")
	send_message(message=f'{host_label(ref_host(containerId))}{get_text("confirm_exec", containerName, html.escape(command))}',
				reply_markup=markup)

# How long a typed /exec waits for its result before saying it is still
# running. The SDK reads an exec's output with no time limit —the client's
# timeout does not reach it— so a `ping` without -c held the button's worker
# for good, and eight of them left the bot deaf.
EXEC_WAIT_SECONDS = 60
# How much output goes in the message. Past it the whole output goes as a
# file: in messages, 5 MB of log was some 1500 of them, rate-limited, with
# every other notification queued behind.
EXEC_OUTPUT_INLINE = 3500
# The command is shown too, and shares the message's 4096 characters with the
# output: a long one pushed the first message over and it was lost.
EXEC_COMMAND_SHOWN = 300
# The end of a log /logs shows: the message only has room for its last 3500
# characters anyway.
LOGS_TAIL_LINES = 200
# The most of a log /logfile sends, under the 50 MB Telegram accepts.
LOGFILE_MAX_BYTES = 45 * 1024 * 1024


def execute_command(containerId, containerName, command, sendMessage=True, wait=True):
	"""
	Runs a command in a container, on a thread of its own.

	With `wait`, which is how a typed /exec behaves, the caller waits up to
	EXEC_WAIT_SECONDS and then says the command is still running; the result
	follows whenever it finishes. A scheduled exec does not wait at all: run in
	line, a long one made the scheduler skip the minutes after it, and one that
	never ended stopped every other task for good.
	"""
	debug(f"Running command: exec for container {containerName} with command [{command}]")
	# The thread answers where the command was asked from, not in the group.
	context_chat = getattr(_reply_context, "chat_id", None)
	context_thread = getattr(_reply_context, "thread_id", None)
	lock = threading.Lock()
	answered = threading.Event()

	def run():
		if context_chat is not None:
			set_reply_context(context_chat, context_thread)
		try:
			try:
				output = manager_for(containerId).execute_command(
					container_id=ref_id(containerId), container_name=containerName, command=command)
			except Exception as e:
				error(f"Error executing command [{command}] in container {containerName}. Error: [{e}]")
				output = None
			with lock:
				answered.set()
				if sendMessage:
					_send_exec_result(containerId, containerName, command, output)
		finally:
			clear_reply_context()

	threading.Thread(target=run, name=f"exec-{containerName}", daemon=True).start()
	if not wait or not sendMessage:
		return
	if answered.wait(EXEC_WAIT_SECONDS):
		return
	# Under the lock, so the notice cannot land after a result that arrived
	# just as the wait ran out.
	with lock:
		if not answered.is_set():
			send_message(message=f'{host_label(ref_host(containerId))}{get_text("exec_still_running", containerName)}')


def _send_exec_result(containerId, containerName, command, output):
	label = host_label(ref_host(containerId))
	if output is None:
		send_message(message=f'{label}{get_text("error_executing_command_container", html.escape(command), containerName)}')
		return
	shown = command if len(command) <= EXEC_COMMAND_SHOWN else f"{command[:EXEC_COMMAND_SHOWN]}…"
	send_message(message=f'{label}{get_text("executed_command", containerName, html.escape(shown), html.escape(output[:EXEC_OUTPUT_INLINE]))}')
	if len(output) > EXEC_OUTPUT_INLINE:
		send_document(document=get_temporal_file(output, f"exec_{containerName}"),
					caption=f'{label}{get_text("exec_output_attached", containerName)}')

def comparison_message(containerId, containerName, comparison):
	"""
	The comparison shown before an update or a tag change is confirmed: the
	image running and the one it would become, what changes between them,
	and where to read more.

	One builder for both: they used to be two copies of the same text, and a
	change had to be made twice — the versions were. When both are the same
	image, which only a tag change can find, it says that instead.
	"""
	message = f"{host_label(ref_host(containerId))}📦 <b>{containerName}</b>\n\n"
	if comparison['current_digest'] == comparison['new_digest']:
		message += (f"ℹ️ <b>{get_text('update_information')}:</b>\n"
					f"   {get_text('update_tag')}: <code>{comparison['current_tag']}</code> → <code>{comparison['new_tag']}</code>"
					f"{comparison_version_line(comparison['current_version'])}\n"
					f"   {get_text('update_created')}: {comparison['current_date']}\n"
					f"   {get_text('update_size')}: {comparison['current_size']}\n"
					f"   {get_text('update_digest')}: <code>{comparison['current_digest']}</code>\n\n"
					f"{get_text('update_same_image')}")
	else:
		changes = [f"{get_text('update_size_change')}: {comparison['size_diff']}"]
		if comparison['days_diff'] > 0:
			changes.append(f"{comparison['days_diff']} {get_text('update_days_newer')}")
		elif comparison['days_diff'] < 0:
			changes.append(f"{abs(comparison['days_diff'])} {get_text('update_days_older')}")
		version_change = comparison_version_change(comparison)
		if version_change:
			changes.append(version_change)
		# Bullets only when there is more than one change.
		changes_text = ("\n   • " + "\n   • ".join(changes)) if len(changes) > 1 else f"\n   {changes[0]}"
		for side, emoji, title in (("current", "📌", "update_current_image"), ("new", "🆕", "update_new_image")):
			message += (f"{emoji} <b>{get_text(title)}:</b>\n"
						f"   {get_text('update_tag')}: <code>{comparison[side + '_tag']}</code>"
						f"{comparison_version_line(comparison[side + '_version'])}\n"
						f"   {get_text('update_created')}: {comparison[side + '_date']}\n"
						f"   {get_text('update_size')}: {comparison[side + '_size']}\n"
						f"   {get_text('update_digest')}: <code>{comparison[side + '_digest']}</code>\n\n")
		message += f"📊 <b>{get_text('update_changes')}:</b>{changes_text}"

	message += comparison_version_notes(comparison)

	if comparison['description']:
		message += f"\n\n📝 <b>{get_text('update_description')}:</b>\n{comparison['description']}"

	if comparison['registry_url']:
		if comparison['registry_name']:
			link_text = get_text('update_more_info_registry', comparison['registry_name'])
		else:
			link_text = get_text('update_more_info')
		message += f"\n\n🔗 <a href=\"{comparison['registry_url']}\">{link_text}</a>"
	return message


def confirm_change_tag(containerId, containerName, tag):
	debug(f"Running command: confirm_change_tag for container {containerName} to tag {tag}")

	# Show loading message
	loading_msg = send_message(message=get_text("fetching_image_data"))

	# Get detailed comparison
	comparison = get_image_comparison(containerId, containerName, new_tag=tag)

	# Delete loading message
	if loading_msg:
		delete_message(loading_msg.message_id)

	# The same buttons with or without the comparison. The fallback built
	# its own and showed "Yes, change to $1", the tag never filled in.
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_change_tag", tag), callback_data=f"changeTag|{containerId}|{tag}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	if not comparison:
		# Fallback to simple confirmation if comparison fails. The tag is
		# escaped in the text only: it comes from whatever registry the image
		# names, and the callback needs it exactly as it is.
		send_message(message=f'{host_label(ref_host(containerId))}{get_text("confirm_change_tag", containerName, html.escape(tag))}',
					reply_markup=markup)
		return

	message = comparison_message(containerId, containerName, comparison)
	send_message(message=message, reply_markup=markup)

def change_tag_container(containerId, containerName):
	try:
		markup = InlineKeyboardMarkup(row_width=button_columns())
		container = manager_for(containerId).client.containers.get(ref_id(containerId))
		repo = image_repository(container.attrs['Config']['Image'])
		tags = get_docker_tags(repo, ref_host(containerId))

		if not tags:
			error(f"Could not get tags for image {repo}")
			send_message(message=f'{host_label(ref_host(containerId))}{get_text("error_getting_tags", repo)}')
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
		send_message(message=f'{host_label(ref_host(containerId))}{get_text("change_tag", containerName)}',
					reply_markup=markup)
	except Exception as e:
		error(f"Error changing tag for container {containerName}. Error: [{e}]")
		send_message(message=f'{host_label(ref_host(containerId))}{get_text("error_changing_tag", containerName)}')

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
			tag_to_pull = f"{image_repository(current_tag)}:{new_tag}"
		else:
			# Checking for update: pull the same tag
			tag_to_pull = current_tag

		# Pull new image (without applying to container)
		debug(f"Pulling image {tag_to_pull} for comparison")
		new_image = pull_image(owner.client, tag_to_pull, container)

		# New image info
		new_digest = new_image.id.replace('sha256:', '')[:12]
		new_size = new_image.attrs.get('Size', 0)
		new_created = new_image.attrs.get('Created', '')

		# Calculate differences
		has_update = current_digest != new_digest \
			and not same_running_manifest(owner.client, container, current_image.id, new_image)
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

		# What each one is, by the number its image declares.
		current_version = running_version(ref_host(containerId), container)
		new_version = available_version(new_image, tag_to_pull)
		new_labels = ((new_image.attrs or {}).get('Config') or {}).get('Labels') or {}

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
			'registry_name': registry_name,
			'current_version': current_version,
			'new_version': new_version,
			'release_notes_url': release_notes_url(new_labels.get('org.opencontainers.image.source'), new_version),
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
		# Docker Hub decided the way Docker decides it. Two components without
		# a dot used to mean Docker Hub here, so `nas:5000/app` linked to a
		# Docker Hub page that does not exist; and `docker.io/library/nginx`,
		# three components, linked nowhere.
		registry, repository = split_registry(image_repository(image_tag))
		if registry is None:
			if repository.startswith("library/"):
				repository = repository[len("library/"):]
			image_tag = repository
		elif registry == "localhost" or ":" in registry or "." not in registry:
			# A registry of the user's own: there is no public page to link.
			return (None, None)
		parts = image_tag.split('/')

		# Check if it's a Docker Hub image
		if registry is None and len(parts) <= 2:
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
		send_message(message=f'{host_label(ref_host(containerId))}{get_text("confirm_update", containerName)}',
					reply_markup=markup)
		return

	# Check if images are identical (no update available)
	if comparison['current_digest'] == comparison['new_digest']:
		# Same image, no update needed - save to cache as updated
		image_with_tag = comparison['current_tag']
		save_container_update_status(image_with_tag, containerName, False, ref_host(containerId))
		send_message(message=f'{get_text("already_updated", containerName)}{host_suffix(ref_host(containerId))}')
		return

	message = comparison_message(containerId, containerName, comparison)

	markup = create_confirm_cancel_keyboard(f"update|{containerId}", "button_confirm_update")
	send_message(message=message, reply_markup=markup)

def confirm_update_selected(chatId, messageId):
	containers, selected = load_update_data(chatId, messageId)
	if not containers or not selected:
		send_message(message=get_text("update_list_expired"))
		return
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
	for cid in selected_in_order(containers, selected):
		# The list can span hosts, and the same name on two of them is two
		# different updates: the confirmation has to say which is which.
		containersToUpdate += f"· <b>{id_to_name.get(cid, cid)}</b>{host_suffix(ref_host(cid))}\n"
	markup = create_confirm_cancel_keyboard(f"updateSelected|{messageId}", "button_confirm_update")
	# A selection across a dozen hosts can name more than fits one message.
	send_split_message(get_text("confirm_update_all", containersToUpdate), reply_markup=markup)

# Telegram refuses a keyboard of more than about a hundred buttons, and with it
# the whole message: a big release day across a dozen hosts lost the
# announcement. A longer list is shown a page at a time, with room to spare for
# the navigation and the fixed rows.
UPDATE_KEYBOARD_PAGE_SIZE = 80


def update_keyboard_page(container_available, page):
	"""`page` held inside the pages the list has, so a stale one still shows something."""
	pages = max(1, -(-len(container_available) // UPDATE_KEYBOARD_PAGE_SIZE))
	try:
		page = int(page)
	except (TypeError, ValueError):
		page = 0
	return min(max(page, 0), pages - 1), pages


def page_of(container_available, reference):
	"""The page a reference is on, for repainting the one that was just tapped."""
	for index, (cid, _cname) in enumerate(container_available):
		if cid == reference:
			return index // UPDATE_KEYBOARD_PAGE_SIZE
	return 0


def build_generic_keyboard(container_available, selected_containers, originalMessageId, action_type, button_text, button_text_all=None, page=0):
	"""Generic keyboard builder for the multi-select update flow.

	container_available: list of [id, name] pairs.
	selected_containers: set of container IDs.
	page: which page to show when the list does not fit one keyboard. The
	selection and the fixed buttons always cover the whole list.
	"""
	# The host earns a place on the button only when the list spans machines:
	# /updateall gathers the whole fleet and two hosts can hold the same
	# container name, while a per-host message already says which host it is
	# about in its own text. With a single host the level does not exist.
	spans_hosts = len({ref_host(cid) for cid, _cname in container_available}) > 1

	# With the host on it a button no longer fits two to a row on a phone, and
	# a cut-off "nas · homea…" does not say what it would update. So a list
	# across hosts gets one column, whatever the setting says; with one host
	# the names are short again and the setting applies.
	markup = InlineKeyboardMarkup(row_width=1 if spans_hosts else button_columns())
	page, pages = update_keyboard_page(container_available, page)
	start = page * UPDATE_KEYBOARD_PAGE_SIZE
	botones = []
	for cid, cname in container_available[start:start + UPDATE_KEYBOARD_PAGE_SIZE]:
		icono = ICON_CONTAINER_MARKED_FOR_UPDATE if cid in selected_containers else ICON_CONTAINER_MARK_FOR_UPDATE
		# A button caption is plain text, so the alias goes in unescaped: the
		# bold markup host_label() uses would show as literal tags here.
		etiqueta = f"{host_registry.alias(ref_host(cid))} · {cname}" if spans_hosts else cname
		botones.append(
			InlineKeyboardButton(f"{icono} {etiqueta}", callback_data=f"toggle{action_type}|{cid}")
		)
	markup.add(*botones)

	# Only when there is more than one page, so an ordinary list reads as it
	# always has. The counter repaints the page it is on, which is harmless and
	# spares a callback that does nothing.
	if pages > 1:
		navigation = []
		if page > 0:
			navigation.append(InlineKeyboardButton("◀️", callback_data=f"page{action_type}|{page - 1}"))
		navigation.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"page{action_type}|{page}"))
		if page < pages - 1:
			navigation.append(InlineKeyboardButton("▶️", callback_data=f"page{action_type}|{page + 1}"))
		markup.row(*navigation)

	fixed_buttons = []
	if selected_containers:
		fixed_buttons.append(InlineKeyboardButton(button_text, callback_data=f"confirm{action_type}Selected|{originalMessageId}"))
	else:
		# Use button_text_all if provided, otherwise use button_text
		text_to_use = button_text_all if button_text_all else button_text
		fixed_buttons.append(InlineKeyboardButton(text_to_use, callback_data=f"toggle{action_type}All"))

	fixed_buttons.append(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	# Always side by side, one row: with a single column add() would stack
	# them, and the last row is where the fixed buttons are expected to be.
	markup.row(*fixed_buttons)
	return markup

def count_actionable_buttons(markup):
	"""
	Number of container/project buttons in a menu keyboard, ignoring the last
	row (which always holds the fixed cancel/close/back buttons). Zero means the
	menu has nothing left to act on.
	"""
	rows = markup.keyboard or []
	# The page buttons act on nothing: counted, an emptied list never closed.
	return sum(1 for row in rows[:-1] for button in row
			if not str(getattr(button, "callback_data", "") or "").startswith("pickPage|"))


def picker_page_key(chat_id, message_id):
	"""Cache key for the page a container picker is showing."""
	return f"picker_page_{chat_id}_{message_id}"

def build_hierarchical_keyboard(containers, action_type, exclude_own=False, filter_standalone_status=None, filter_projects_with_all_status=None, marked_names=None, host_id=None, back_to_hosts=False, page=0):
	"""
	Build hierarchical keyboard with Compose projects and standalone containers.
	Level 1: Shows projects (📦) and standalone containers (🐳)

	Args:
		containers: List of container objects
		action_type: Type of action (Restart, Stop, Run)
		exclude_own: Whether to leave the bot's own container out
		filter_standalone_status: Optional list of statuses to filter standalone containers (e.g., ['running', 'restarting'])
		filter_projects_with_all_status: Optional list of statuses - hide projects where ALL containers have these statuses
		marked_names: Optional set of container names already acted upon in a
			multi-action session; they show a check instead of their status emoji
		back_to_hosts: Whether the list was reached by choosing a host, and
			so offers going back to that choice

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
		if exclude_own and is_own_container(host_id, container.id, container.name):
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
			# The bot is not offered, so it is not counted either: the number
			# is what the project's buttons will act on.
			counted = project_info.containers
			if exclude_own:
				counted = without_the_bot(host_id, counted)[0]
			if filter_standalone_status:
				# Count only containers matching the filter
				container_count = sum(1 for c in counted if c.status in filter_standalone_status)
			else:
				# Show total count
				container_count = len(counted)
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
		'ports': 'ports',
		# Whatever its state: a stopped container fell through to "run", and
		# tapping it to get its compose started it instead.
		'compose': 'compose',
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
			status_emoji = get_status_emoji(container.status, container.name, container, host_id)

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

	# Telegram refuses a keyboard of more than about a hundred buttons, and the
	# message with it: a host with more containers than that answered /logs or
	# /restart with nothing at all. Shown a page at a time, as the update list.
	page, pages = update_keyboard_page(botones, page)
	start = page * UPDATE_KEYBOARD_PAGE_SIZE
	markup.add(*botones[start:start + UPDATE_KEYBOARD_PAGE_SIZE])
	if pages > 1:
		navigation = []
		if page > 0:
			navigation.append(InlineKeyboardButton("◀️", callback_data=f"pickPage|{action_type}|{host_id}|{page - 1}"))
		navigation.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"pickPage|{action_type}|{host_id}|{page}"))
		if page < pages - 1:
			navigation.append(InlineKeyboardButton("▶️", callback_data=f"pickPage|{action_type}|{host_id}|{page + 1}"))
		markup.row(*navigation)

	# Add cancel button. Once something has been acted upon there is nothing
	# left to cancel, only to close
	close = InlineKeyboardButton(get_text("button_close" if marked_names else "button_cancel"), callback_data="cerrar")
	# A way back to choosing the host: tapping the wrong one meant cancelling
	# and typing the command again. In the same row as the cancel button,
	# which is where the fixed buttons are expected to be.
	if back_to_hosts and action_type in PICKER_ACTIONS:
		markup.row(InlineKeyboardButton(get_text("button_back"), callback_data=f"pickHosts|{action_type}"), close)
	else:
		markup.add(close)

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
		"exclude_own": True,
		"filter_standalone_status": ["exited", "stopped", "paused", "created"],
		"filter_projects_with_all_status": ["running", "restarting"],
		"multi_action": "Run",
	},
	"Stop": {
		"container_callback": "stop",
		"prompt_key": "stop_a_container",
		"empty_key": "no_containers_to_stop",
		"exclude_own": True,
		"filter_standalone_status": ["running", "restarting"],
		"filter_projects_with_all_status": ["exited", "stopped", "paused", "created"],
		"multi_action": "Stop",
	},
	"Restart": {
		"container_callback": "restart",
		"prompt_key": "restart_a_container",
		"empty_key": "no_containers_to_restart",
		"exclude_own": True,
		"multi_action": "Restart",
	},
	"Delete": {
		"container_callback": "confirmDelete",
		"prompt_key": "delete_container",
		"empty_key": "no_containers_to_delete",
		"exclude_own": True,
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
		exclude_own=spec.get("exclude_own", False),
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

	Across hosts it goes through the same parallel sweep as the menus, with
	its deadline: asking each machine in turn left a typed "/stop plex"
	waiting on a hung host for as long as that host cared to take.
	"""
	if not host_id:
		return [(entry, container)
				for entry, _, containers in hosts_with_containers()
				for container in containers if container.name == container_name]

	matches = []
	for entry in host_registry.hosts():
		if entry["id"] != host_id:
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

	missing = []
	if host_id:
		matches = find_containers_by_name(text, host_id)
	else:
		sections = hosts_with_containers()
		matches = [(entry, container) for entry, _, containers in sections
				for container in containers if container.name == text]
		missing = unreachable_hosts(sections)
	if not matches:
		return None, text, []
	# One match is only the one meant if every host answered. With one silent,
	# a "/stop plex" meant for its plex stopped the namesake on another
	# machine without asking; now the one found has to be picked.
	if len(matches) == 1 and not missing:
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
			f'🖥️ {entry.get("alias", entry["id"])}  ·  {get_status_emoji(container.status, container.name, container, entry["id"])}',
			callback_data=f'{spec["container_callback"]}|{container_ref(entry["id"], container)}'))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	text = get_text("container_on_several_hosts", container_name)
	if len(candidates) == 1:
		# Asked because a host did not answer, which may have the name too.
		silent = unreachable_hosts(hosts_with_containers())
		if silent:
			text += "\n\n" + "\n".join(get_text("list_host_unreachable", host_alias(e["id"]), "") for e in silent)
	sent = send_message(message=text, reply_markup=markup)
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


def _picker_has_anything(containers, exclude_own, filter_standalone_status,
						filter_projects_with_all_status, owner, host_id=None):
	"""
	Whether an action has anything to offer on one host.

	Mirrors the filtering the keyboard applies, so a host button never leads to
	an empty list.
	"""
	for container in containers:
		if exclude_own and is_own_container(host_id, container.id, container.name):
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
						exclude_own=False, filter_standalone_status=None,
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
		if _picker_has_anything(containers, exclude_own, filter_standalone_status,
								filter_projects_with_all_status, owner, entry["id"]):
			sections.append((entry, owner, containers))

	if not sections:
		send_message(message=get_text(empty_key))
		return None

	if len(sections) == 1:
		entry, _, containers = sections[0]
		return _send_picker_for_host(
			entry, containers, action_type, prompt_key, exclude_own,
			filter_standalone_status, filter_projects_with_all_status, multi_action,
			name_host=not host_registry.is_single_host())

	# More than one host has something: offer the hosts first.
	# No session yet: which host the menu is showing is only decided once one
	# is picked, and that is where the session is opened.
	return send_message(message=host_question(action_type),
						reply_markup=_host_picker_markup(action_type, sections))


def _picker_sections(action_type):
	"""The hosts that have something to offer for a picker action, as (entry, owner, containers)."""
	spec = PICKER_ACTIONS[action_type]
	return [(entry, owner, containers)
			for entry, owner, containers in hosts_with_containers(spec.get("comando", ""))
			if _picker_has_anything(containers, spec.get("exclude_own", False),
									spec.get("filter_standalone_status"),
									spec.get("filter_projects_with_all_status"), owner, entry["id"])]


def _host_picker_markup(action_type, sections):
	"""One button per host, then cancel."""
	markup = InlineKeyboardMarkup(row_width=1)
	for entry, _, containers in sections:
		markup.add(InlineKeyboardButton(
			f'🖥️ {entry.get("alias", entry["id"])}',
			callback_data=f'pickHost|{action_type}|{entry["id"]}'))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	return markup


def render_host_picker(chat_id, message_id, action_type):
	"""
	Repaints a host's container list as the choice of host it came from.

	Asked again rather than remembered: what each host has to offer may have
	changed since, and a host with nothing left would be a button to nowhere.
	The session the host's list opened is closed, since no host is chosen now.
	"""
	if action_type not in PICKER_ACTIONS:
		warning(f"Unknown picker action: {action_type}")
		return
	clear_multi_action(chat_id, message_id)
	sections = _picker_sections(action_type)
	if not sections:
		edit_message_text(get_text(PICKER_ACTIONS[action_type]["empty_key"]), chat_id, message_id)
		return
	edit_message_text(host_question(action_type), chat_id, message_id,
					reply_markup=_host_picker_markup(action_type, sections))


def _send_picker_for_host(entry, containers, action_type, prompt_key, exclude_own,
						filter_standalone_status, filter_projects_with_all_status,
						multi_action, name_host=False):
	"""Sends one host's level-1 keyboard, naming the host when it is not the only one."""
	markup, standalone = build_hierarchical_keyboard(
		containers, action_type, exclude_own,
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


def host_level_key(chat_id, message_id):
	"""Cache key marking a picker that was reached by choosing a host."""
	return f"host_level_{chat_id}_{message_id}"


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
		disconnect_host(host_id)
		edit_message_text(get_text("host_unreachable", host_alias(host_id), html.escape(str(e))),
						chat_id, message_id)
		return

	markup, standalone = build_hierarchical_keyboard(
		containers, action_type, spec.get("exclude_own", False),
		filter_standalone_status=spec.get("filter_standalone_status"),
		filter_projects_with_all_status=spec.get("filter_projects_with_all_status"),
		host_id=host_id, back_to_hosts=True)
	# Remembered on the message, so the list rebuilt after entering a project
	# or acting on a container keeps its way back. A picker that skipped the
	# choice of host, because only one had anything, has no choice to go back to.
	write_cache_item(host_level_key(chat_id, message_id), True)

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
	project_info = project_info_or_none(host_id, project_name)

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

	return markup, get_text(message_key, html.escape(str(project_name)))

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
		send_message(message=f'{get_text("error_project_not_found", html.escape(str(project_name)))}'
							f'{host_suffix(host_id or host_registry.local_host_id())}')
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
	# The session is the message's own record of its host, so it wins: what
	# the press carries is only the dispatcher's default when nothing in the
	# button names a machine.
	host_id = (session["host"] if session and session.get("host") else None) or host_id
	if session:
		save_multi_action(chatId, messageId, session["action"], 2, project_name, done, host_id)
	handle_enter_project_level2(action_type, project_name, chatId, messageId,
								marked_names=done, host_id=host_id)

def back_to_level1_multi_aware(action_type, chatId, messageId, host_id=None):
	"""Returns to the top-level list, keeping any multi-action session in sync."""
	session = load_multi_action(chatId, messageId)
	done = session["done"] if session else None
	# The session is the message's own record of its host, so it wins: what
	# the press carries is only the dispatcher's default when nothing in the
	# button names a machine.
	host_id = (session["host"] if session and session.get("host") else None) or host_id
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

def build_back_to_level1_keyboard(action_type, chatId, messageId, exclude_own=True, marked_names=None, host_id=None):
	"""
	Generic function to build "backTo...Level1" keyboards.

	Args:
		action_type: Type of action ('Restart', 'Run', 'Stop', 'Delete', 'Exec', 'Logs', 'Logfile', 'Compose', 'CheckUpdate', 'Info', 'ChangeTag')
		chatId: Chat ID for saving cache
		messageId: Message ID for saving cache
		exclude_own: Whether to leave the bot's own container out
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
	except Exception as e:
		# Same as above: the client builds and the call fails. Every "back to
		# the list" button in the project menus comes through here, so leaving
		# this uncaught broke twenty-five of them at once.
		warning(f"Could not rebuild the menu for {host_alias(host_id)}: {e}")
		disconnect_host(host_id)
		return None

	# Check if no containers or only bot container
	label = host_label(host_id)
	if not containers:
		send_message(message=f'{label}{get_text(config["no_containers_key"])}')
		return None

	if config['check_only_bot'] and all(
			is_own_container(host_id, c.id, c.name) for c in containers):
		send_message(message=f'{label}{get_text(config["no_containers_key"])}')
		return None

	# Build hierarchical keyboard
	exclude_own_here = exclude_own and config['exclude_bot']
	markup, standalone_containers = build_hierarchical_keyboard(
		containers,
		action_type,
		exclude_own_here,
		filter_standalone_status=config['filter_standalone_status'],
		filter_projects_with_all_status=config['filter_projects_with_all_status'],
		marked_names=marked_names,
		host_id=host_id,
		back_to_hosts=bool(read_cache_item(host_level_key(chatId, messageId))),
		# The page the list was on: acting on a container on page 3 does not
		# send the list back to page 1.
		page=read_cache_item(picker_page_key(chatId, messageId)) or 0,
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
		back_to_hosts: Whether the list was reached by choosing a host, and
			so offers going back to that choice

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
			status_indicator = get_status_emoji(container.status, container.name, container, host_id)
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

	# One row, whatever the columns: with one, add() stacked them in two, the
	# whole-project button outside the last row, counted as one still to act on,
	# and an emptied project never went back to the list.
	markup.row(*bottom_buttons)

	return markup

def is_admin(userId):
	return str(userId) in ADMIN_IDS


# When each user who is not an administrator was last told so. Answering
# every command and button let anyone who found the bot fill the one queue the
# notifications go through, so it is said once an hour at most, and not waited
# for.
NOT_ADMIN_REPLY_EVERY = 3600
_not_admin_told = {}
_not_admin_lock = threading.Lock()

def tell_not_admin(user_id, username):
	now = time.monotonic()
	with _not_admin_lock:
		last = _not_admin_told.get(user_id)
		if last is not None and now - last < NOT_ADMIN_REPLY_EVERY:
			debug(f"User {user_id} ({username}) is not an administrator, already told")
			return
		if len(_not_admin_told) > 1000:
			for stale in [u for u, t in _not_admin_told.items() if now - t >= NOT_ADMIN_REPLY_EVERY]:
				del _not_admin_told[stale]
		_not_admin_told[user_id] = now
	warning(f"User {user_id} ({username}) tried to use admin command without permission")
	message_queue.add_message(_send_message_direct, user_id, get_text("user_not_admin"), None, "html", True, None, idempotent=False)

def update_mode(host_id, name, labels):
	"""
	What the update check does with a container, and what decided it.

	Returns (mode, label): mode is "auto", "ignore", or None to announce what
	it finds; label is the DCB-* label that decided it, or None when it was
	the setting chosen from /info or /settings.

	A label in the container wins whatever it says, `=false` included: it is
	in the compose, the bot cannot change it, and a setting overriding it
	would leave the compose saying one thing and the bot doing another.
	Ignoring wins over updating, as it always has.
	"""
	labels = labels if isinstance(labels, dict) else {}
	if LABEL_IGNORE_CHECK_UPDATES in labels or LABEL_AUTO_UPDATE in labels:
		if label_enabled(labels, LABEL_IGNORE_CHECK_UPDATES):
			return "ignore", LABEL_IGNORE_CHECK_UPDATES
		if label_enabled(labels, LABEL_AUTO_UPDATE):
			return "auto", LABEL_AUTO_UPDATE
		return None, LABEL_IGNORE_CHECK_UPDATES if LABEL_IGNORE_CHECK_UPDATES in labels else LABEL_AUTO_UPDATE
	return store.container_update_mode(host_id or host_registry.local_host_id(), name), None

def container_image(container):
	"""The image reference a container was created from, as its config says it."""
	return str(((getattr(container, "attrs", None) or {}).get("Config") or {}).get("Image") or "")

def might_be_the_bot(host_id, image):
	"""
	Whether a container could be the bot without the bot being able to tell.

	When it cannot identify itself —nothing found in /proc— an automatic
	update could take it for any other container: stop it, and with it the
	update halfway through. With a label that took editing the compose; from
	a button it is one tap. Only the identification counts here, never a
	name: one that does not match is exactly how the bot fails to recognise
	itself. Going by the image is a guess, but it is only used to refuse.
	"""
	if own_container() is not None:
		return False
	if host_id != host_registry.local_host_id():
		return False
	return "docker-controller-bot" in image

def set_update_mode(host_id, name, labels, image, mode):
	"""
	Changes how the update check treats a container, from /info or /settings.

	`labels` and `image` are the container's, as last seen: whether a label
	decides instead, and whether it might be the bot.

	Returns None when it was changed, or the text that says why not: a
	DCB-* label decides it, settings.json cannot be written, or it might be
	the bot itself and the bot cannot tell.

	Going into "ignore" or out of it, what the cache knew goes. Left in, an
	update found by a /checkupdate while ignored stayed in /updateall for good,
	and one announced before ignoring it was taken, back out, for one
	already announced — and never announced again.
	"""
	if host_registry.host(host_id) is None:
		return get_text("error_can_not_do_that")
	current, label = update_mode(host_id, name, labels)
	if label:
		return get_text("container_updates_by_label", label)
	if mode == "auto" and might_be_the_bot(host_id, image):
		return get_text("container_updates_maybe_the_bot")
	if not store.set_container_update_mode(host_id, name, mode):
		return get_text("container_updates_not_saved")
	if (current == "ignore") != (mode == "ignore"):
		store.forget_update_status(host_id, name)
	debug(f"Update mode of {name} on {host_registry.alias(host_id)}: {mode or 'notify'}")
	return None

# How long a container's settings are kept after it last showed up on a host
# that answered. Recreating a container is removing it and creating it again,
# and a `docker compose down` can last a weekend.
CONTAINER_SETTINGS_GRACE_DAYS = 30

def forget_gone_container_settings(sections, now=None):
	"""
	Tidies the per-container settings against what the hosts just listed.

	Two things go. What a DCB-* label now decides: the label wins, and a
	value kept underneath would come back the day the label is removed,
	months later, when nobody remembers having set it. And the settings of
	containers missing from a host that answered for
	CONTAINER_SETTINGS_GRACE_DAYS, which are said once, where notifications
	go. A host that is down or paused is not in `sections`, so nothing of
	its is touched; a removed host takes its settings with it when removed.

	`sections` is what hosts_with_containers returned. Returns the
	(host_id, name) pairs forgotten for being gone.
	"""
	if not store.container_settings() or store.settings_unreadable():
		return []
	present = {}
	for entry, _owner, containers in sections:
		host_id = entry["id"]
		present[host_id] = [container.name for container in containers]
		for container in containers:
			_mode, label = update_mode(host_id, container.name, container.labels)
			if label and store.container_update_mode(host_id, container.name):
				debug(f"{container.name} has the {label} label now: its own setting goes")
				store.set_container_update_mode(host_id, container.name, None)
	forgotten = store.reconcile_containers(
		present, time.time() if now is None else now, CONTAINER_SETTINGS_GRACE_DAYS * 24 * 60 * 60)
	if forgotten:
		debug(f"Forgot the settings of {len(forgotten)} containers gone for {CONTAINER_SETTINGS_GRACE_DAYS} days")
		for host_id, name in forgotten:
			# Left, it would be taken up by the next container given that name,
			# as forget_container explains for a delete from the bot.
			store.forget_update_status(host_id, name)
		if not is_muted():
			# In pieces: a rebuilt host can leave a list too long for one.
			for piece in split_for_telegram(get_text(
					"container_settings_forgotten", CONTAINER_SETTINGS_GRACE_DAYS,
					by_host([(host_id, container_line(name)) for host_id, name in forgotten]))):
				send_message_to_notification_channel(message=piece)
	return forgotten

def update_available(container, host_id=None):
	"""
	Whether a container has a pending update, according to the cache.

	`host_id` is which host it lives on. It defaults to the local one so the
	single-host callers read as they always did, but anything iterating over
	hosts has to pass it or it would read another machine's cache entry.

	Never for a container the check ignores: a /checkupdate on it still
	finds and records what is there, and it ended up in /updateall and its
	button, with nothing that would ever take it out again.
	"""
	image_with_tag = container.attrs['Config']['Image']
	update = False
	if store.get("bot.check_updates") and update_mode(host_id, container.name, getattr(container, "labels", None))[0] != "ignore":
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
			if is_own_container(host_id, container.id, container.name):
				bot_container = container
			else:
				other_standalone.append(container)

	# Show bot container first if present
	if bot_container:
		result += f"🐳 {get_status_emoji(bot_container.status, bot_container.name, bot_container, host_id)} {bot_container.name}"
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
		result += f"📦 {html.escape(project_name)} ({get_text('compose_project_containers', container_count)})\n"

		# Sort containers within project: running first, then stopped (all alphabetically)
		sorted_project_conts = sort_containers_by_priority(project_conts)
		for container in sorted_project_conts:
			# Use cached info, fall back to container name when the compose
			# service label is missing (e.g. Nextcloud AIO spawns siblings
			# tagged only with the project label, no service label).
			_, service_name = container_info_cache[container.id]
			display_name = service_name or container.name
			result += f"  {get_status_emoji(container.status, container.name, container, host_id)} {html.escape(display_name)}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"
		result += "\n"  # Empty line between projects

	# Show other standalone containers last (running first, then stopped - all alphabetically)
	if other_standalone:
		sorted_standalone = sort_containers_by_priority(other_standalone)
		for container in sorted_standalone:
			result += f"🐳 {get_status_emoji(container.status, container.name, container, host_id)} {container.name}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"

	result += "</pre>"
	return result

def pending_updates():
	"""
	Every container with an update waiting, across the fleet, as [reference,
	name] pairs: host by host, each one sorted the way its lists are.

	One list for /updateall and for its button, which had a copy each.
	"""
	pending = []
	for entry, owner, containers in hosts_with_containers():
		# Sorted within each host: bot first, then running, then stopped (all
		# alphabetically). Sorting the fleet as one list would interleave
		# machines, and the host is the coarser grouping. Parallel listing,
		# so a hung host degrades this instead of holding it for its timeout.
		for container in sort_containers_by_priority(containers, entry["id"]):
			if update_available(container, entry["id"]):
				pending.append([container_ref(entry["id"], container), container.name])
	return pending

def sort_containers_by_priority(containers, host_id=None):
	"""
	Sort containers with consistent priority:
	1. The bot's own container first
	2. Running/restarting containers (alphabetically)
	3. Stopped/paused/exited containers (alphabetically)

	Args:
		containers: List of container objects

	Returns:
		List of sorted containers
	"""
	def sort_key(container):
		# Priority 1: Bot container first
		is_bot = 0 if is_own_container(host_id, container.id, container.name) else 1

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

def get_status_emoji(statusStr, containerName, container=None, host_id=None):
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

	if is_own_container(host_id, getattr(container, "id", None), containerName):
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

	try:
		# By name from the list, which says the image too: one request
		# instead of a name lookup and an inspect for every service shown.
		container = manager(host_id).container_named(containerName)
		if container is None:
			return status
		if update_available(container, host_id):
			status = "⬆️"
	except Exception as e:
		error(f"Could not read the update status of {containerName} on {host_registry.alias(host_id)}: [{e}]")

	return status

def get_random_available_port(host_id=None):
	"""
	A free port on one host, or None when none was found.

	Same rule as check_specific_port: a port free on the bot's machine says
	nothing about the machine the user is looking at, so handing that number
	back would be a wrong answer rather than a rough one.
	"""
	host_id = host_id or host_registry.local_host_id()
	# Un botón vive en un mensaje y el mensaje sobrevive al host: pulsar uno
	# viejo de una máquina ya quitada no puede reventar.
	try:
		owner = manager(host_id)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot generate a port for {host_id}: {e.reason}")
		return None
	try:
		return port_manager.get_random_available_port(manager=owner)
	except Exception as e:
		warning(f"Could not generate a port on {host_id}: {e}")
		return None

def _port_sort_key(port):
	"""Sorts "8000-8010/tcp" by 8000, and anything unreadable last."""
	number, _, protocol = str(port).partition("/")
	try:
		first = int(number.split("-", 1)[0])
	except ValueError:
		first = 65536
	return first, protocol, str(port)


def show_container_ports(host_id=None):
	"""
	Show all ports used by containers on one host.

	Per host rather than merged: a port is only in use or free on a given
	machine, and a single list would suggest a conflict where there is none.
	"""
	host_id = host_id or host_registry.local_host_id()
	try:
		# Inspected, not just listed: the bindings come from
		# HostConfig.PortBindings, which only the inspect carries, and a
		# stopped container's ports are nowhere else.
		containers = manager(host_id).list_containers(inspect=True)
	except host_registry.HostUnavailable as e:
		send_message(message=get_text("host_unreachable", host_alias(host_id), html.escape(str(e.reason))))
		return
	except Exception as e:
		warning(f"Could not list ports on {host_alias(host_id)}: {e}")
		disconnect_host(host_id)
		send_message(message=get_text("host_unreachable", host_alias(host_id), html.escape(str(e))))
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
				# By the first port of each: a range such as 8000-8010 made int()
				# raise, and the container was left out of the list altogether.
				unique_ports = sorted(set(ports), key=_port_sort_key)
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
	# Con su host: generar y comprobar son preguntas sobre una máquina, y sin
	# él contestaban siempre por la local aunque estuvieras viendo otra.
	markup.add(
		InlineKeyboardButton(get_text("ports_button_generate"), callback_data=f"generatePort|{host_id}"),
		InlineKeyboardButton(get_text("ports_button_check"), callback_data=f"checkPort|{host_id}")
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

	# Send each chunk: header only on the first, keyboard only on the last,
	# which takes the others with it when closed.
	earlier = []
	for i, chunk_lines in enumerate(chunks):
		prefix = header if i == 0 else ""
		message = f"{prefix}{pre_open}" + "\n".join(chunk_lines) + pre_close
		is_last = (i == len(chunks) - 1)
		sent = send_message(message=message, reply_markup=markup if is_last else None)
		if not is_last and sent:
			earlier.append(sent.message_id)
	if sent and earlier:
		write_cache_item(f"companions_{sent.chat.id}_{sent.message_id}", earlier)

def ask_port_to_check(userId, host_id=None):
	"""
	Asks for a port number, remembering which host the answer is about.

	The host is carried in the state rather than looked up when the number
	arrives: by then the screen it came from is gone, and defaulting to the
	local machine is how this used to answer about the wrong one.
	"""
	host_id = host_id or host_registry.local_host_id()
	debug(f"Running command: ask_port_to_check for user {userId} on {host_id}")
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelCheckPort"))
	x = send_message(message=f'{host_label(host_id)}{typed_prompt("ports_ask_port")}', reply_markup=markup)
	if x:
		save_port_check_request_state(userId, x.message_id, host_id)

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


def check_specific_port(port_number, host_id=None):
	"""
	Whether a port is free on one host, as (is_available, message).

	The host is not optional in meaning, only in signature: a port is taken or
	free on a single machine. Answering about the local one while the user is
	looking at another is a wrong answer, not an approximation.
	"""
	host_id = host_id or host_registry.local_host_id()
	try:
		owner = manager(host_id)
	except host_registry.HostUnavailable as e:
		return False, get_text("host_unreachable", host_alias(host_id), html.escape(str(e.reason)))
	try:
		is_available, message_key, container_name = port_manager.check_port_availability(
			port_number, manager=owner)
	except Exception as e:
		warning(f"Could not check port {port_number} on {host_id}: {e}")
		return False, get_text("host_unreachable", host_alias(host_id), html.escape(str(e)))

	if container_name:
		return is_available, get_text(message_key, port_number, container_name)
	else:
		return is_available, get_text(message_key, port_number)

def print_donors():
	donors = get_array_donors_online()
	if donors:
		result = ""
		for donor in donors:
			result += f"· {html.escape(str(donor))}\n"
		send_message(message=get_text("donors_list", result))
	else:
		send_message(message=get_text("error_getting_donors"))

def get_array_donors_online():
	headers = {
		'Cache-Control': 'no-cache',
		'Pragma': 'no-cache'
	}

	# With a deadline and its failures caught, like every other request the
	# bot makes: a server that accepted the connection and never answered
	# held the worker for good, and a network error reached nobody.
	try:
		response = requests.get(DONORS_URL, headers=headers, timeout=10)
	except requests.RequestException as e:
		error(f"Error getting donors: {e}")
		return []
	if response.status_code == 200:
		try:
			data = response.json()
			if isinstance(data, list):
				return sorted(data, key=str)
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


def find_live_container(ref, name):
	"""
	find_container, for a reference that may be older than the container.

	Updated from outside the bot, a container comes back with another ID and
	the same name. A list of updates can be days old, so what it holds is
	looked up by name when the ID is gone — on its own host only, as the
	buttons are.
	"""
	owner, container = find_container(ref)
	if container is None:
		current_id = find_container_id_on_host(ref_host(ref), name)
		if current_id:
			owner, container = find_container(make_ref(ref_host(ref), current_id))
	return owner, container


def runs_latest_image(ref, name):
	"""
	Whether a container already runs the newest image its tag has here.

	The pending-update cache is only rewritten by the next check, hours away.
	A container updated from outside the bot in between still read as
	pending, and updating it pulled nothing new and recreated it for nothing.
	The check pulls the image when it finds an update, so locally the tag
	already points at it: a container running that image is up to date, and
	the cache is told so.
	"""
	owner, container = find_live_container(ref, name)
	if container is None:
		return False
	try:
		image_with_tag = container.attrs['Config']['Image']
		local_id = container.image.id
		latest = owner.client.images.get(image_with_tag)
		if local_id != latest.id and not same_running_manifest(owner.client, container, local_id, latest):
			return False
	except Exception as e:
		debug(f"Could not compare the image of {name}: {e}")
		return False
	save_container_update_status(image_with_tag, container.name, False, owner.host_id)
	return True


def project_info_or_none(host_id, project_name):
	"""
	A host's project, or None when that host cannot answer right now.

	Reading a project means listing containers, and a host has two ways of
	failing at that: not giving a client at all, and giving one that fails on
	the very next call — which is what a Docker mid-restart does. Both mean the
	same thing to a caller, and neither should reach the person who pressed the
	button as a stack trace.
	"""
	try:
		return manager(host_id).get_project_info(project_name)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach {host_id} for project {project_name}: {e.reason}")
		return None
	except Exception as e:
		warning(f"Could not read project {project_name} on {host_alias(host_id)}: {e}")
		disconnect_host(host_id)
		return None


# Telegram refuses a message over 4096 characters, counted after the markup is
# parsed. The raw text is held well under that, tags included.
MESSAGE_SPLIT_BUDGET = 3500

# The tags split_for_telegram keeps balanced across a cut, outermost first.
SPLIT_KEPT_TAGS = ("pre", "b", "i", "code")


def split_for_telegram(text, budget=MESSAGE_SPLIT_BUDGET):
	"""
	Splits an HTML message into pieces Telegram will take, in order.

	Cuts fall on a blank line where there is one, so a group is not torn
	across two messages: first one outside a <pre> — between hosts —, then one
	inside — between Compose projects —, and on any line otherwise. Never on
	the blank line just before a <pre> opens, which would leave a host's
	heading and counts in one message and its containers in the next.

	A tag cut in two — the <pre> of a listing, the <b> around a list of names —
	is closed at the end of one piece and opened again at the start of the
	next, or the second half would lose its format and the first be refused
	for an unclosed tag.

	A text that fits comes back whole, so a short message is unchanged.
	"""
	if len(text) <= budget:
		return [text]

	pieces = []
	carried = dict.fromkeys(SPLIT_KEPT_TAGS, 0)
	current = []

	def emit(lines):
		while lines and not lines[-1].strip():
			lines = lines[:-1]
		if not lines:
			return
		body = "\n".join(lines)
		reopen = "".join(f"<{tag}>" * carried[tag] for tag in SPLIT_KEPT_TAGS)
		for tag in SPLIT_KEPT_TAGS:
			carried[tag] = max(0, carried[tag] + body.count(f"<{tag}>") - body.count(f"</{tag}>"))
		close = "".join(f"</{tag}>" * carried[tag] for tag in reversed(SPLIT_KEPT_TAGS))
		pieces.append(reopen + body + close)

	# Room kept for the tags reopened at the start and closed at the end.
	room = budget - 100
	for line in text.split("\n"):
		# A line that only closes tags stays with what it closes, in the
		# room kept above. Cut before it, a listing's final "</pre>" became a
		# last piece with no text in it, which Telegram refuses, and that
		# last piece is the one carrying the keyboard.
		if current and re.fullmatch(r"(?:</\w+>)+", line.strip()):
			current.append(line)
			continue
		# A loop, not an if: what is left after cutting at a blank line can
		# still be too long with this line on it.
		while current and len("\n".join(current + [line])) > room:
			cut = _split_point(current, line, carried["pre"] > 0)
			emit(current[:cut])
			current = current[cut:]
			while current and not current[0].strip():
				current = current[1:]
		current.append(line)
	emit(current)
	return pieces


def _split_point(lines, following, in_pre):
	"""
	Where split_for_telegram cuts `lines`: the index of the blank line it
	starts the next piece at, or len(lines) to cut after all of them.
	`following` is the line about to be added, and `in_pre` whether the first
	of `lines` is inside a <pre>.
	"""
	outside, inside = [], []
	depth = 1 if in_pre else 0
	for index, existing in enumerate(lines):
		after = lines[index + 1] if index + 1 < len(lines) else following
		if index > 0 and not existing.strip() and not after.lstrip().startswith("<pre>"):
			(inside if depth > 0 else outside).append(index)
		depth += existing.count("<pre>") - existing.count("</pre>")
	candidates = outside or inside
	return candidates[-1] if candidates else len(lines)


def send_split_message(text, reply_markup=None):
	"""
	Sends `text` in as many messages as it takes, the keyboard on the last.

	The earlier pieces are noted against the last one, so that its buttons —
	close, cancel, confirm — take the whole of it away and not only the piece
	they are on. Returns the last message sent.
	"""
	pieces = split_for_telegram(text)
	earlier = []
	sent = None
	for index, piece in enumerate(pieces):
		last = index == len(pieces) - 1
		sent = send_message(message=piece, reply_markup=reply_markup if last else None)
		if not last and sent:
			earlier.append(sent.message_id)
	if sent and earlier:
		write_cache_item(f"companions_{sent.chat.id}_{sent.message_id}", earlier)
	return sent


def delete_companions(chat_id, message_id):
	"""Deletes the earlier pieces of a message send_split_message sent in several."""
	key = f"companions_{chat_id}_{message_id}"
	earlier = read_cache_item(key)
	if not earlier:
		return
	delete_cache_item(key)
	for companion in earlier:
		delete_message(companion, chat_id)


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
		except Exception as e:
			# A daemon can build a client and then fail the very next call —
			# a Docker that is restarting does exactly that. Catching only
			# HostUnavailable left /list raising on the commonest setup there
			# is: a single host, mid-restart.
			warning(f"Could not list containers on {host_alias(host_id)}: {e}")
			disconnect_host(host_id)
			return get_text("list_host_unreachable", host_alias(host_id), html.escape(str(e)))
		return display_containers(containers, host_id)

	sections = hosts_with_containers(comando)
	rendered = []
	for entry, _, containers in sections:
		body = display_containers(containers, entry["id"]) if containers else get_text("list_host_empty")
		rendered.append(f'{get_text("list_host_header", host_alias(entry["id"]))}\n{body}')

	for entry in unreachable_hosts(sections):
		rendered.append(get_text("list_host_unreachable", host_alias(entry["id"]), ""))

	return "\n\n".join(rendered) if rendered else get_text("error_no_containers_available")


# The listing threads in flight, one per host and filter at most, with when
# each was started; and the last answer each gave, stamped with when. Module-
# level because the point of them is what another call started or left behind:
# see hosts_with_containers.
_listings = {}
_listing_results = {}
_listings_lock = threading.Lock()


def hosts_with_containers(comando="",
						snapshot_deadline_seconds=host_registry.PROBE_TIMEOUT_SECONDS,
						list_deadline_seconds=20):
	"""
	Every reachable host and its containers, in the order the hosts are
	configured.

	Returns [(host_entry, manager, containers)]. Hosts that do not answer are
	left out: one machine being down has to degrade what it can and nothing
	else.

	Reachability is pre-filtered with the parallel snapshot, and the listings
	then run one thread per host under a single deadline. Listing one host
	after another meant a hung machine held every menu for its own timeout
	before the next host was even tried, so with one machine unplugged the
	whole bot looked frozen.

	The deadline for the listings is shorter than the working client's own
	timeout on purpose: a menu that waits the full 30s has already failed the
	person looking at it. Waiting for the probe is bounded by the probe's
	timeout, which is what its deadline is taken from.

	A listing another sweep started moments ago is shared rather than repeated.
	One still running from longer ago than the deadline is not asked again: it
	is not answering anyway, and starting a second one would leave a thread
	behind on every refresh of a menu. It counts as down for this sweep, and
	giving up on it closes its client, which is what lets the thread that was
	left behind come back.
	"""
	configured = host_registry.hosts()
	if not configured:
		return []
	statuses = host_registry.status_snapshot(
		deadline_seconds=snapshot_deadline_seconds, entries=configured)

	def list_one(entry, key):
		host_id = entry["id"]
		try:
			owner = manager(host_id)
			outcome = (entry, owner, owner.list_containers(comando=comando))
		except host_registry.HostUnavailable as e:
			debug(f"Skipping host {entry.get('alias', host_id)}: {e.reason}")
			outcome = e
		except Exception as e:
			warning(f"Could not list containers on {entry.get('alias', host_id)}: {e}")
			outcome = e
		with _listings_lock:
			# Stamped with when it finished, so every sweep sharing this
			# listing can tell an answer given during it from a stale one.
			_listing_results[key] = (time.monotonic(), outcome)
			# Only if it is still this thread: a later sweep may have given
			# up on this one and started its own.
			if _listings.get(key, (None,))[0] is threading.current_thread():
				_listings.pop(key, None)

	started = time.monotonic()
	asked = []
	for entry in configured:
		host_id = entry["id"]
		if not statuses.get(host_id, (False, ""))[0]:
			continue
		# Per host and per filter: a /run menu and a /stop menu open at once
		# want different lists, and neither can stand in for the other.
		key = (host_id, comando)
		with _listings_lock:
			previous, since = _listings.get(key, (None, 0))
			if previous is not None and previous.is_alive():
				if started - since > list_deadline_seconds:
					debug(f"Host {entry.get('alias', host_id)} is still listing from an earlier sweep: leaving it out")
					continue
				# A sweep running alongside this one —two menus, or the update
				# pass and the start-up message— asked a moment ago. Sharing
				# its answer, rather than leaving the host out, is what keeps a
				# healthy machine from being reported down.
				asked.append((entry, key, previous))
				continue
			thread = threading.Thread(target=list_one, args=(entry, key), daemon=True)
			_listings[key] = (thread, started)
		thread.start()
		asked.append((entry, key, thread))

	deadline = started + list_deadline_seconds
	for _, _, thread in asked:
		thread.join(max(0, deadline - time.monotonic()))

	sections = []
	for entry, key, _ in asked:
		with _listings_lock:
			stamped = _listing_results.get(key)
		outcome = stamped[1] if stamped and stamped[0] >= started else None
		if isinstance(outcome, tuple):
			sections.append(outcome)
		else:
			# It raised, or it is still going past the deadline: a blocking
			# socket read cannot be cancelled, so it is left behind and the
			# host counts as down for this sweep rather than holding the menu.
			disconnect_host(entry["id"])
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
	containers = local_manager().list_containers()
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

def save_container_update_status(image_with_tag, container_name, has_update, host_id=None, versions=None):
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
		versions=versions if has_update else None,
	)

def read_container_update_status(image_with_tag, container_name, host_id=None):
	"""
	Whether `container_name` has a pending update: True, False, or None when
	nothing usable is cached.

	None also covers an entry recorded against a different image, since a tag
	change makes what was cached say nothing about what runs now.
	"""
	return store.update_status(host_id or LOCAL_HOST_ID, container_name, image_with_tag)

# Telegram refuses a message over 4096 characters. A long list of updates
# keeps its header and as many lines as fit, and says how many it left out.
AVAILABLE_UPDATES_TEXT_BUDGET = 3500


def available_updates_text(pairs):
	"""
	The announcement of several updates: how many, and what each one goes
	from and to — the buttons below only have room for the names.
	"""
	header = get_text("available_updates", len(pairs))
	entries = []
	left_out = 0
	used = len(header)
	for index, (ref, name) in enumerate(pairs):
		host_id = ref_host(ref)
		line = container_line(name, *store.update_versions(host_id, name))
		# A host's heading costs room too; counted generously, once per line.
		if used + len(line) + 40 > AVAILABLE_UPDATES_TEXT_BUDGET:
			left_out = len(pairs) - index
			break
		entries.append((host_id, line))
		used += len(line) + 1
	if not entries:
		return header
	text = f"{header}\n\n{by_host(entries)}"
	return f"{text}\n\n… (+{left_out})" if left_out else text


def running_version(host_id, container):
	"""
	The version a container runs, or None when its image does not say.

	The bot knows its own without asking: the images published before it
	carried the label do not have it, and it would otherwise announce its own
	update without the number it is updating from.
	"""
	if is_own_container(host_id, container.id, container.name):
		return VERSION
	config = (container.attrs or {}).get('Config') or {}
	reference = config.get('Image', '')
	# From the image, not from the container's own labels: 4.x copied every
	# label of the image onto the containers it recreated, the version among
	# them, and those containers kept announcing the version they had then.
	try:
		image_config = (container.image.attrs or {}).get('Config') or {}
	except Exception as e:
		debug(f"Could not read the image of {container.name}: {e}")
		image_config = None
	if isinstance(image_config, dict):
		return image_version(image_config, image_repository(reference))
	return image_version(config, image_repository(reference))


def available_version(image, reference):
	"""The version a pulled image carries, or None."""
	try:
		config = (image.attrs or {}).get('Config') or {}
	except Exception:
		return None
	return image_version(config, image_repository(reference))


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

def save_update_data(chat_id, message_id, containers, selected=None, page=0):
	if selected is None:
		selected = set()
	data = {
		"containers": containers,
		"selected": selected,
		"page": page,
	}
	write_cache_item(f"update_data_{chat_id}_{message_id}", data)

def load_update_page(chat_id, message_id):
	"""The page an update list is showing, 0 for one saved before there were pages."""
	data = read_cache_item(f"update_data_{chat_id}_{message_id}")
	return data.get("page", 0) if isinstance(data, dict) else 0

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

def listed_update_name(chat_id, message_id, reference):
	"""The name an update list shows for a reference, or None when it is not one of its."""
	containers, _selected = load_update_data(chat_id, message_id)
	for entry in containers:
		if entry[0] == reference:
			return entry[1]
	return None

def selected_in_order(containers, selected):
	"""
	The selected references, in the order the list shows them.

	The selection is a set, so it has no order of its own. The list does —
	host by host, the way it was gathered — and both the confirmation and the
	updates themselves follow it, so neither jumps between machines.
	"""
	return [cid for cid, _cname in containers if cid in selected]

def clear_update_data(chat_id, message_id):
	delete_cache_item(f"update_data_{chat_id}_{message_id}")

# Serialises every read-modify-write of an update list. Two quick taps on the
# toggles each read the selection before the other wrote it, and one was lost;
# two on "confirm" each found the list still there and ran the batch twice.
update_data_lock = threading.Lock()

def take_update_data(chat_id, message_id):
	"""
	An update list's containers and selection, forgotten in the same step.

	For what acts on the list rather than repaints it: whoever takes it is the
	only one who gets it, so a second tap finds nothing left to run.
	"""
	with update_data_lock:
		containers, selected = load_update_data(chat_id, message_id)
		clear_update_data(chat_id, message_id)
	return containers, selected

def expire_update_list(call_id, chat_id, message_id):
	"""
	Says an update list can no longer be used, and takes its buttons away.

	Its data goes after a week untouched, or with an upgrade from 4.x. Until
	now a tap then repainted a keyboard with no containers on it, and
	confirming updated nothing without a word. The daemon marks what it
	announced as notified, so that message was the only way in, and silence
	looked like the updates having been applied.
	"""
	answer_callback_quietly(call_id, text=get_text("update_list_expired"), show_alert=True)
	edit_message_reply_markup(chat_id, message_id, reply_markup=None)

# Generic cache helpers
def save_schedule_state(user_id, state):
	"""
	Remembers where a user is in the /schedule flow.

	On the session cache like every other half-finished interaction, rather
	than the separate `./cache/` directory this used to use: that path was
	relative to the working directory, so it landed inside the image instead
	of the mapped volume, nothing ever swept it, and its errors went to stdout
	with print() instead of the log.
	"""
	_save_cache("schedule_state", user_id, asked_here(state))

def load_schedule_state(user_id):
	"""The user's in-progress schedule, or None once it has expired."""
	return _load_cache("schedule_state", user_id)

def clear_schedule_state(user_id):
	"""Forgets an in-progress schedule, finished or abandoned."""
	_clear_cache("schedule_state", user_id)

def init_add_schedule_state():
	"""
	A blank schedule, at the first question.

	Every field starts as None on purpose, so the flow can tell "not asked
	yet" from "answered". Which is also why nothing may persist one of these
	without checking it is finished: add_schedule refuses a task with no name,
	cron or action for exactly that reason.
	"""
	return {
		"step": "ask_name",
		"name": None,
		"cron": None,
		"action": None,
		"container": None,
		"minutes": None,
		"show_output": None,
		"command": None,
	}

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

_command_take_lock = threading.Lock()

def take_command_cache(command_id):
	"""
	The command waiting to be confirmed, handed out once.

	Read and cleared in one step: done apart, a double tap on Confirm let both
	presses read it before either cleared it, and the command ran twice.
	"""
	with _command_take_lock:
		command = load_command_cache(command_id)
		clear_command_cache(command_id)
	return command

# Command request state functions
def save_command_request_state(user_id, containerId, containerName, deleteMessage):
	value = {"containerId": containerId, "containerName": containerName, "deleteMessage": deleteMessage}
	_save_cache("pending_command", user_id, asked_here(value))

def load_command_request_state(user_id):
	return _load_cache("pending_command", user_id)

def clear_command_request_state(user_id):
	_clear_cache("pending_command", user_id)

# Port check request state functions
def save_port_check_request_state(user_id, deleteMessage, host_id=None):
	value = {"deleteMessage": deleteMessage,
			"host": host_id or host_registry.local_host_id()}
	_save_cache("pending_port_check", user_id, asked_here(value))

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
		# Which machine the message is about, for the buttons that carry no
		# reference of their own: "Back" from a project has to return to the
		# list of the host it came from, not to the local one.
		"host": host_id,
		"containers": {}
	}
	for container in containers:
		cache_data["containers"][container_ref(host_id, container)] = container.name

	write_cache_item(f"containers_{chat_id}_{message_id}", cache_data)

def message_host(chat_id, message_id):
	"""
	The host a menu message is about, or None when the bot no longer knows.

	Messages cached before the host was stored still say it through their
	references, which all point at the same machine.
	"""
	cache_data = read_cache_item(f"containers_{chat_id}_{message_id}")
	if not cache_data:
		return None
	if cache_data.get("host"):
		return cache_data["host"]
	for reference in cache_data.get("containers", {}):
		if ":" in reference:
			return ref_host(reference)
	return None

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

def find_container_id_on_host(host_id, container_name):
	"""
	The short id of a container by name, on one host only.

	Deliberately not a search across hosts: this resolves the id of a button
	whose container was recreated, and the button already says which machine
	it meant. Searching everywhere could land the action on a container of the
	same name on a different host, which is the one mistake in all of this
	that would be both silent and destructive.
	"""
	return locate_container_on_host(host_id, container_name)[0]


def locate_container_on_host(host_id, container_name):
	"""
	As find_container_id_on_host, telling apart a host that did not answer.

	Returns (short id or None, why the host could not be asked or None). A
	host that is down says nothing about whether the container is there.
	"""
	try:
		owner = manager(host_id)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach host {host_id} to look up {container_name}: {e.reason}")
		return None, str(e.reason)
	try:
		container = owner.container_named(container_name)
	except Exception as e:
		debug(f"Could not look up {container_name} on {host_id}: {e}")
		return None, str(e)
	return (container.id[:CONTAINER_ID_LENGTH] if container is not None else None), None

def name_by_reference(ref):
	"""
	The name of the container a reference points at, telling apart a host
	that did not answer: (name or None, why the host could not be asked or
	None). As locate_container_on_host does going the other way.
	"""
	try:
		owner = manager_for(ref)
	except host_registry.HostUnavailable as e:
		debug(f"Cannot reach the host for {ref}: {e.reason}")
		return None, str(e.reason)
	try:
		return owner.client.containers.get(ref_id(ref)).name, None
	except docker.errors.NotFound:
		return None, None
	except docker.errors.APIError as e:
		if e.status_code == 400:
			# "multiple IDs found with provided prefix": the host answered,
			# and which container the button meant cannot be told.
			debug(f"Could not tell which container {ref} is: {e}")
			return None, None
		debug(f"Could not look up {ref}: {e}")
		return None, str(e)
	except Exception as e:
		debug(f"Could not look up {ref}: {e}")
		return None, str(e)

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

def pull_image(client, image, container):
	"""
	Pulls an image for the platform the container runs, not the host's.

	Without it a container forced to `linux/amd64` on an ARM machine was
	compared against the ARM image: on the classic image store, an update
	announced on every check that, once applied, switched its architecture.
	"""
	platform = container_platform(container) if container is not None else None
	# Checked: the SDK's own pull ignores an error the daemon reports midway
	# —a full disk, a dropped layer— and hands back the image already here,
	# which read as "up to date".
	return pull_checked(client, image, platform=platform)


def same_running_manifest(client, container, local_id, image):
	"""
	Whether `container`, running the image `local_id`, already runs `image`.

	By id first, which is all the classic image store needs: there an id is
	the platform's own. With containerd's store, the default on new installs,
	an id is the digest of the multi-platform index, which changes when its
	author republishes any one platform: compared alone, it announced an
	update "X → X" that changed nothing here, and applying it recreated the
	container for nothing. The manifest the container runs is in its
	ImageManifestDescriptor (API 1.48); the index's manifests only come in
	an inspect that asks for them, which docker-py's inspect_image cannot.
	"""
	if local_id == getattr(image, "id", None):
		return True
	digest = ((container.attrs or {}).get("ImageManifestDescriptor") or {}).get("digest")
	if not isinstance(digest, str) or not digest:
		return False
	try:
		api = client.api
		attrs = api._result(api._get(api._url("/images/{0}/json", image.id), params={"manifests": "1"}), True)
	except Exception as e:
		debug(f"Could not read the manifests of {image.id}: {e}")
		return False
	for manifest in (attrs.get("Manifests") if isinstance(attrs, dict) else None) or []:
		if not isinstance(manifest, dict) or manifest.get("Kind", "image") != "image":
			continue
		if digest in (manifest.get("ID"), (manifest.get("Descriptor") or {}).get("digest")):
			return True
	return False


def generate_docker_compose(container):
	"""
	Builds the docker-compose of a container.

	Reuses `extract_container_config`, the same extractor every update relies
	on, so the compose reflects what the user actually configured instead of
	re-reading a handful of `attrs` fields: values inherited from the image
	(PATH, the image ENTRYPOINT, its LABELs...) are already filtered out there.
	"""
	config = extract_container_config(container)
	compose_text = ComposeGenerator(container.name, config).to_yaml()
	# The bot's own token is the one secret the file does not need to carry:
	# whoever recreates the bot from it has it in their .env already, and
	# compose fills it in. Sent in the clear, it stayed in the chat history,
	# readable by anyone in the group. Replaced after the dump, which escapes
	# every `$` it is given.
	if TELEGRAM_TOKEN and TELEGRAM_TOKEN in compose_text:
		compose_text = compose_text.replace(TELEGRAM_TOKEN, "${TELEGRAM_TOKEN}")
	return compose_text

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
		# La cola reintenta con el mismo objeto: sin rebobinarlo, el segundo
		# intento leería un fichero ya consumido y mandaría uno vacío.
		if hasattr(document, "seek"):
			document.seek(0)
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
		# Without previews, as every message is sent: an edit that does not
		# say so gets one, and an /info repainted from its button grew a
		# GitHub card under it.
		return bot.edit_message_text(text, chat_id, message_id, parse_mode=parse_mode, reply_markup=reply_markup,
									 link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=True))
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

def delete_message_later(sent, seconds):
	"""
	Removes a message after a delay, without holding a thread meanwhile.

	The self-destructing messages used to sleep in line, which parks one of
	telebot's workers for as long as the message is meant to last — 45 seconds
	for /donate, and /start now puts both of them one tap away.

	The chat is captured now rather than looked up when the timer fires: the
	reply context is thread-local and a timer thread has none, so it would fall
	back to TELEGRAM_GROUP and delete in the wrong chat.
	"""
	if not sent:
		return None
	chat_id = sent.chat.id
	message_id = sent.message_id
	timer = threading.Timer(seconds, lambda: delete_message(message_id, chat_id))
	timer.daemon = True
	timer.start()
	return timer


def send_message(chat_id=None, message=None, reply_markup=None, parse_mode="html", disable_web_page_preview=True):
	"""Sends a message using the queue (waits for result to get the message_id)"""
	if chat_id is None:
		chat_id = get_reply_chat_id()
	message_thread_id = get_reply_thread_id(chat_id)
	return message_queue.add_message(_send_message_direct, chat_id, message, reply_markup, parse_mode, disable_web_page_preview, message_thread_id, wait_for_result=True, idempotent=False)

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
	return message_queue.add_message(_send_document_direct, chat_id, document, reply_markup, caption, parse_mode, message_thread_id, wait_for_result=True, idempotent=False)

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
		container = local_manager().client.containers.get(container_id)
		try:
			updater_image = container.image.id
			stop_container(container)
			container.remove()
			local_manager().client.images.remove(updater_image)
			previous = store.state_get("self_update_from")
			store.state_set("self_update_from", None)
			send_message(message=update_result_text(host_registry.local_host_id(), own_container_name(),
													previous, VERSION if previous else None))
		except Exception as e:
			error(f"Could not delete container {UPDATER_CONTAINER_NAME}. Error: [{e}]")

def check_own_container():
	"""
	Works out which container the bot is, and says so in the log.

	It used to be told, in CONTAINER_NAME, and refused to start without it.
	Now it finds out: own_container() reads the ids out of /proc and asks the
	local daemon which one is real. See own_container.py for why that works
	where the hostname does not.

	Three outcomes, and none of them stops the bot:

	- Found it. If CONTAINER_NAME is still in the docker-compose, the log says
	  it can go.
	- Could not, but CONTAINER_NAME is set. The safeguards fall back to
	  matching that name on the local host, which is what 4.x did.
	- Could not, and there is no name either. Everything works except knowing
	  which container is the bot, so the bot can be stopped or deleted from
	  its own menus. That is worth a warning, and not worth refusing to start
	  over: outside a container there is nothing to protect, and inside one
	  this has never been observed to fail.
	"""
	own = own_container()
	if own:
		_, container_id, name = own
		debug(f"I am {name} ({container_id[:CONTAINER_ID_LENGTH]}), worked out on my own")
		if CONTAINER_NAME:
			warning("CONTAINER_NAME is no longer needed: the bot identifies its own "
					"container. You can remove it from the docker-compose.")
		return
	if CONTAINER_NAME:
		warning(f"Could not identify my own container, falling back to the name "
				f"{CONTAINER_NAME} on the local host")
		return
	warning("Could not identify my own container, and CONTAINER_NAME is not set: "
			"I will not stop myself from being stopped or deleted from my own menus")

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

		# Parsing is not enough: `0 0 30 2 *` parses, and fires on the 30th of
		# February. It was saved, and never ran, and nothing said so. Asking
		# for the next run is what tells a date that exists from one that
		# does not.
		croniter(cron_expression).get_next(datetime)
		return True
	except Exception:
		return False

def get_my_architecture(host_id=None):
	"""
	The architecture of a host, in Docker Hub's naming.

	Of the host the container lives on, not of the bot's: with a Raspberry Pi
	managed from an x86 machine, filtering by the bot's own architecture
	offered the Pi tags it cannot run.
	"""
	try:
		info = manager(host_id or host_registry.local_host_id()).client.info()
		architecture_docker = info['Architecture']
		return docker_architectures.get(architecture_docker, architecture_docker)
	except Exception as e:
		error(f"Error getting Docker architecture: [{e}]")
		return None

# Docker Hub under the names it also answers to. `docker.io/library/nginx` is
# how a lot of compose files spell `nginx`, and asking Docker Hub's API for a
# repository called `docker.io/library/nginx` found nothing.
DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")

# linuxserver.io publishes the same images on Docker Hub, whose API says when
# each tag was pushed and for which architectures; lscr.io itself is a plain
# registry and says neither.
DOCKER_HUB_MIRRORS = ("lscr.io/",)

# A registry lists its tags in alphabetical order, in pages: everything has to
# be read to find the newest. Capped, so a repository with an absurd number of
# tags costs a bounded wait.
REGISTRY_TAG_PAGE_SIZE = 1000
REGISTRY_TAG_MAX_PAGES = 20
REGISTRY_TAGS_SHOWN = 20


def split_registry(repo_name):
	"""
	(registry, repository) for an image repository; registry None for Docker Hub.

	The first component is a registry when Docker would read it as one: it has
	a dot or a port, or it is localhost.
	"""
	for prefix in DOCKER_HUB_PREFIXES + DOCKER_HUB_MIRRORS:
		if repo_name.startswith(prefix):
			return None, repo_name[len(prefix):]
	first, _, rest = repo_name.partition("/")
	if rest and ("." in first or ":" in first or first == "localhost"):
		return first, rest
	return None, repo_name


def get_docker_tags(repo_name, host_id=None):
	"""Tags to offer for an image repository, newest first."""
	try:
		registry, repository = split_registry(repo_name)
		if registry is None:
			debug(f"Getting tags from DockerHub for {repo_name}")
			return get_docker_tags_from_DockerHub(repository, host_id)
		debug(f"Getting tags from {registry} for {repository}")
		return get_docker_tags_from_registry(registry, repository)
	except Exception as e:
		error(f"Failed to get tags for {repo_name}: {str(e)}")
		return []

def get_docker_tags_from_DockerHub(repo_name, host_id=None):
	architecture = get_my_architecture(host_id)
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


_STABLE_VERSION = re.compile(r"v?\d+(?:\.\d+)*")


def sort_tags_newest_first(tags):
	"""
	Version tags newest first, by their numbers rather than alphabetically.

	A registry hands tags over in alphabetical order, and taking the first
	twenty of those offered Home Assistant's 2021 releases and Plex's from
	2020 — a /changetag that read as a list of upgrades and was a list of
	downgrades. Released versions (`2025.10.1`, `v1.2`) come first; then the
	named ones (`latest`, `stable`), which mean something without numbers;
	then everything else with a version in it (`2025.11.0b1`,
	`version-1.43.4-ls326`).
	"""
	def numbers(tag):
		return tuple(int(n) for n in re.findall(r"\d+", tag))

	stable = sorted((t for t in tags if _STABLE_VERSION.fullmatch(t)), key=numbers, reverse=True)
	named = sorted(t for t in tags if not re.search(r"\d", t))
	rest = sorted((t for t in tags if t not in stable and t not in named), key=numbers, reverse=True)
	return stable + named + rest


# The most a registry answer is read to. The registry is whatever an image
# name says, so it may be anyone's, and a tag list of hundreds of megabytes
# was read whole, up to twenty pages of it.
REGISTRY_MAX_BYTES = 5 * 1024 * 1024


def _is_private_host(host):
	"""Whether a host is written as a loopback, private or link-local address."""
	if (host or "").lower() == "localhost":
		return True
	try:
		address = ipaddress.ip_address(host)
	except ValueError:
		return False
	return address.is_private or address.is_loopback or address.is_link_local


def _realm_allowed(realm, url):
	"""
	Whether the bot may ask `realm` for the token `url` wants.

	The realm comes in the registry's own answer and was asked whatever it
	was: an http address, or one inside the user's network handed out by a
	registry on the internet, had the bot make that request from in there.
	So https, and a private address only for a registry that is private too;
	plain http only for a registry on localhost, as Docker itself allows.
	"""
	realm_parts, url_parts = urlsplit(realm), urlsplit(url)
	if not realm_parts.hostname:
		return False
	if realm_parts.scheme == "http":
		return realm_parts.hostname in ("localhost", "127.0.0.1") and url_parts.hostname in ("localhost", "127.0.0.1")
	if realm_parts.scheme != "https":
		return False
	return not _is_private_host(realm_parts.hostname) or _is_private_host(url_parts.hostname)


def _registry_json(response):
	"""A registry answer's JSON, read only up to REGISTRY_MAX_BYTES."""
	if not hasattr(response, "iter_content"):
		return response.json()
	body = bytearray()
	for chunk in response.iter_content(65536):
		body += chunk
		if len(body) > REGISTRY_MAX_BYTES:
			response.close()
			raise ValueError(f"registry answer larger than {REGISTRY_MAX_BYTES} bytes")
	return json.loads(body.decode("utf-8") or "null")


def _registry_get(url, session):
	"""
	A registry request, with the anonymous token most of them want first.

	Redirects are not followed: the tag list and the token are not answers a
	registry redirects, and following them let any registry send the bot to
	any address.
	"""
	response = session.get(url, timeout=10, stream=True, allow_redirects=False)
	if response.status_code == 401:
		challenge = response.headers.get("WWW-Authenticate", "")
		if challenge.lower().startswith("bearer"):
			fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
			realm = fields.get("realm", "")
			if not _realm_allowed(realm, url):
				warning(f"Not asking {realm!r} for a token: {url} is not allowed to send the bot there")
				return response
			token_response = session.get(realm, timeout=10, stream=True, allow_redirects=False, params={
				key: fields[key] for key in ("service", "scope") if key in fields})
			body = _registry_json(token_response) if token_response.ok else {}
			body = body if isinstance(body, dict) else {}
			token = body.get("token") or body.get("access_token")
			if token:
				session.headers["Authorization"] = f"Bearer {token}"
				response = session.get(url, timeout=10, stream=True, allow_redirects=False)
	return response


def get_docker_tags_from_registry(registry, repo_name):
	"""
	Tags from any registry speaking the Docker Registry API: ghcr.io, quay.io,
	a private one on the user's NAS.

	HTTPS first, and plain HTTP after it only for the addresses Docker itself
	allows it for without configuration: localhost.
	"""
	schemes = ["https"]
	host = registry.split(":")[0]
	if host in ("localhost", "127.0.0.1"):
		schemes.append("http")
	for scheme in schemes:
		session = requests.Session()
		tags = []
		path = f"/v2/{repo_name}/tags/list?n={REGISTRY_TAG_PAGE_SIZE}"
		try:
			for _ in range(REGISTRY_TAG_MAX_PAGES):
				response = _registry_get(f"{scheme}://{registry}{path}", session)
				if response.status_code != 200:
					error(f"{registry} answered {response.status_code} listing the tags of {repo_name}")
					return []
				listed = _registry_json(response)
				tags.extend(t for t in ((listed.get("tags") if isinstance(listed, dict) else None) or []) if isinstance(t, str) and t)
				# The next page, if any, is announced in a Link header.
				match = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", response.headers.get("Link", ""))
				if not match:
					break
				path = match.group(1)
			else:
				debug(f"Stopped reading the tags of {registry}/{repo_name} after {REGISTRY_TAG_MAX_PAGES} pages")
		except requests.exceptions.RequestException as e:
			debug(f"Could not reach {scheme}://{registry}: {e}")
			continue
		except ValueError as e:
			error(f"{registry} gave an answer that could not be read listing the tags of {repo_name}: {e}")
			return []
		if not tags:
			debug(f"No tags returned by {registry} for {repo_name}")
		return sort_tags_newest_first(list(dict.fromkeys(tags)))[:REGISTRY_TAGS_SHOWN]
	error(f"Could not reach the registry {registry} to list the tags of {repo_name}")
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


# How long a stop waits for the messages still queued, so the last thing the
# bot said reaches Telegram. Docker's default grace period is 10 s.
STOP_MESSAGES_SECONDS = 5

_stopping = False


def _stop_on_signal(signum, frame):
	"""
	What `docker stop` sends. Python running as a container's first process
	ignores SIGTERM unless something handles it, so the bot kept running until
	Docker killed it at the end of the grace period: every restart, every
	recreation and every self-update waited ten seconds for nothing.

	An update of another container that is halfway through is let finish: cut
	short, it leaves `<name>_old` behind. Docker still kills the bot if it
	takes longer than the grace period, which is what happened before anyway.
	Then whatever is queued for Telegram goes out, and the bot exits.

	Raises instead of asking the poller to stop: that would wait for the
	current long poll, up to a minute.
	"""
	global _stopping
	if _stopping:
		return
	_stopping = True
	debug(f"Received {signal.Signals(signum).name}, stopping")
	# The dependents an update stops and starts again count too: stopping
	# while they wait for their parent left them stopped, and a manual stop
	# is one their restart policy does not undo.
	if updates_in_progress() or dependents_in_progress():
		debug(f"Waiting for {updates_in_progress()} update(s) and {dependents_in_progress()} dependent restart(s) to finish before stopping")
		while updates_in_progress() or dependents_in_progress():
			time.sleep(0.5)
	if not message_queue.drain(STOP_MESSAGES_SECONDS):
		warning(f"Stopping with messages still queued after {STOP_MESSAGES_SECONDS}s")
	debug("Stopped")
	raise SystemExit(0)


# What the image's HEALTHCHECK reads. Touched through the same workers that
# answer commands and buttons, so it goes stale when every one of them is
# stuck: alive, but deaf, which is what a health check is for.
HEARTBEAT_PATH = "/tmp/dcb-heartbeat"
HEARTBEAT_SECONDS = 30


def _beat():
	try:
		with open(HEARTBEAT_PATH, "a"):
			pass
		os.utime(HEARTBEAT_PATH, None)
	except OSError as e:
		debug(f"Could not write the heartbeat: {e}")


def _heartbeat_loop():
	while True:
		pool = getattr(bot, "worker_pool", None)
		if pool is not None:
			pool.put(_beat)
		else:
			_beat()
		time.sleep(HEARTBEAT_SECONDS)


def main():
	"""
	Starts the daemons, publishes the command menu and begins polling.

	Called by the entry point once every module that registers commands
	and callbacks has been imported, so nothing is missing by the time
	the first message arrives.
	"""
	debug(f"Starting bot version {VERSION}")
	signal.signal(signal.SIGTERM, _stop_on_signal)
	_beat()
	threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()

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

	# Neither is a reason not to start. Raised from here, a Telegram not yet
	# reachable after a reboot, or a local Docker not answering, put the
	# container in a restart loop with nothing said anywhere.
	try:
		register_bot_commands()
	except Exception as e:
		warning(f"Could not publish the command menu: {e}")
	try:
		delete_updater()
	except Exception as e:
		warning(f"Could not look for a leftover updater container: {e}")
	check_own_container()
	check_mute()
	send_message(message=build_starting_message())
	if _migration.ask_for_language:
		ask_initial_language()
	telemetry_client.start()
	bot.infinity_polling(timeout=60)
