import docker
import hashlib
import io
import json
import os
import pickle
import re
import requests
import sys
import telebot
import threading
import time
import uuid
import yaml
from config import *
from croniter import croniter
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from docker_update import extract_container_config, perform_update
from docker_compose_manager import (
    ComposeDetector,
    ComposeProjectManager,
    ComposeProjectInfo
)
from schedule_manager import ScheduleManager
from schedule_flow import (
    save_schedule_state, load_schedule_state, clear_schedule_state,
    init_add_schedule_state
)
from migrate_schedules import migrate_schedules

VERSION = "4.0.0"

_unmute_timer = None
_mute_lock = threading.Lock()  # Lock for thread-safe mute timer operations
_cache_lock = threading.Lock()  # Lock for thread-safe cache operations

def debug(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - DEBUG: {message}')

def error(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - ERROR: {message}')

def warning(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - WARNING: {message}')

def sizeof_fmt(num, suffix="B"):
	for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
		if abs(num) < 1024.0:
			return f"{num:3.1f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f}Yi{suffix}"

if LANGUAGE.lower() not in ("es", "en", "nl", "de", "ru", "gl", "it", "cat"):
	error("LANGUAGE only can be ES/EN/NL/DE/RU/GL/IT/CAT")
	sys.exit(1)

# MODULO DE TRADUCCIONES
# Cache for locale files to avoid repeated file I/O
_locale_cache = {}

def load_locale(locale):
	"""Load locale with caching to avoid repeated file I/O"""
	if locale not in _locale_cache:
		with open(f"/app/locale/{locale}.json", "r", encoding="utf-8") as file:
			_locale_cache[locale] = json.load(file)
	return _locale_cache[locale]

def get_text(key, *args):
	"""Get translated text with caching"""
	messages = load_locale(LANGUAGE.lower())
	if key in messages:
		translated_text = messages[key]
	else:
		messages_en = load_locale("en")
		if key in messages_en:
			warning(f"key ['{key}'] is not in locale {LANGUAGE}")
			translated_text = messages_en[key]
		else:
			error(f"key ['{key}'] is not in locale {LANGUAGE} or EN")
			return f"key ['{key}'] is not in locale {LANGUAGE} or EN"

	# Replace placeholders efficiently
	if args:
		for i, arg in enumerate(args, start=1):
			translated_text = translated_text.replace(f"${i}", str(arg))

	return translated_text


# Comprobación inicial de variables
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

DIR = {"cache": "./cache/"}
for key in DIR:
	try:
		os.mkdir(DIR[key])
	except:
		pass

if not os.path.exists(SCHEDULE_PATH):
	os.makedirs(SCHEDULE_PATH)

if not os.path.exists(FULL_MUTE_FILE_PATH):
	with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
		mute_file.write("0")

# Instanciamos el bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Instanciamos el ScheduleManager
schedule_manager = ScheduleManager(SCHEDULE_PATH, SCHEDULE_JSON_FILE)

# Ejecutar migración de schedules si es necesario
try:
	migrate_schedules()
	# Refresh the cache after migration to ensure schedules are loaded
	schedule_manager._load_cache()
except Exception as e:
	error(f"Error during schedule migration: {e}")

# ============================================================================
# SISTEMA DE COLA DE MENSAJES CON RATE LIMITING
# ============================================================================
import queue
from threading import Thread, Lock

class MessageQueue:
	"""
	Sistema de cola de mensajes con rate limiting para evitar saturar Telegram.
	Implementa:
	- Cola de mensajes con delays configurables
	- Reintentos con backoff exponencial
	- Manejo de errores de rate limiting
	"""
	def __init__(self, delay_between_messages=0.5, max_retries=3):
		self.queue = queue.Queue()
		self.delay_between_messages = delay_between_messages
		self.max_retries = max_retries
		self.lock = Lock()
		self.running = True
		self.worker_thread = Thread(target=self._process_queue, daemon=True)
		self.worker_thread.start()
		debug("Message queue started")

	def _process_queue(self):
		"""Procesa la cola de mensajes de forma continua"""
		while self.running:
			try:
				# Obtener el siguiente mensaje de la cola (timeout para permitir shutdown)
				message_data = self.queue.get(timeout=1)
				if message_data is None:  # Señal de parada
					break

				self._execute_message(message_data)
				time.sleep(self.delay_between_messages)
			except queue.Empty:
				continue
			except Exception as e:
				error(f"Error processing message queue: {str(e)}")

	def _execute_message(self, message_data):
		"""Ejecuta un mensaje con reintentos y backoff exponencial"""
		func = message_data['func']
		args = message_data['args']
		kwargs = message_data['kwargs']
		result_queue = message_data.get('result_queue')

		try:
			for attempt in range(self.max_retries):
				try:
					result = func(*args, **kwargs)
					if result_queue:
						result_queue.put(result)
					return result
				except Exception as e:
					error_msg = str(e)
					# Detectar rate limiting de Telegram
					if "Too Many Requests" in error_msg or "429" in error_msg:
						if attempt < self.max_retries - 1:
							wait_time = (2 ** attempt) * 2  # Backoff exponencial: 2, 4, 8 segundos
							warning(f"Rate limit detected. Waiting {wait_time}s before retrying...")
							time.sleep(wait_time)
							continue
					elif attempt < self.max_retries - 1:
						wait_time = 1 * (attempt + 1)
						debug(f"Error sending message (attempt {attempt + 1}/{self.max_retries}). Retrying in {wait_time}s...")
						time.sleep(wait_time)
						continue

					error(f"Final error sending message after {self.max_retries} attempts: {str(e)}")
					if result_queue:
						result_queue.put(None)
					break
		except Exception as e:
			error(f"Error processing message queue: {str(e)}")
			if result_queue:
				result_queue.put(None)

	def add_message(self, func, *args, wait_for_result=False, **kwargs):
		"""Añade un mensaje a la cola. Si wait_for_result=True, espera el resultado"""
		result_queue = queue.Queue() if wait_for_result else None
		self.queue.put({
			'func': func,
			'args': args,
			'kwargs': kwargs,
			'result_queue': result_queue
		})
		if wait_for_result:
			try:
				return result_queue.get(timeout=60)  # Esperar máximo 60 segundos
			except queue.Empty:
				error("Error processing message queue: Timeout esperando resultado del mensaje")
				return None
		return None

	def shutdown(self):
		"""Detiene la cola de mensajes"""
		self.running = False
		self.queue.put(None)

# Instanciar la cola de mensajes global
message_queue = MessageQueue(delay_between_messages=0.1, max_retries=5)

class DockerManager:
	def __init__(self):
		self.client = docker.from_env()
		self.compose_manager = ComposeProjectManager(self.client)

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

	# ========== COMPOSE PROJECT METHODS ==========

	def get_compose_projects(self):
		"""
		Obtiene todos los proyectos Docker Compose detectados.

		Returns:
			dict: Diccionario {project_name: ComposeProjectInfo}
		"""
		return self.compose_manager.get_all_projects()

	def is_compose_container(self, container):
		"""
		Verifica si un contenedor es parte de un proyecto Compose.

		Args:
			container: Objeto container de Docker SDK

		Returns:
			bool: True si es parte de un proyecto Compose
		"""
		is_compose = ComposeDetector.is_compose_container(container)
		if is_compose:
			project_name = ComposeDetector.get_project_name(container)
			service_name = ComposeDetector.get_service_name(container)
			debug(f"Container '{container.name}' is part of compose project '{project_name}' (service: {service_name})")
		return is_compose

	def get_container_project_info(self, container):
		"""
		Obtiene información del proyecto Compose al que pertenece un contenedor.

		Args:
			container: Objeto container de Docker SDK

		Returns:
			tuple: (project_name, service_name) o (None, None) si no es Compose
		"""
		if not ComposeDetector.is_compose_container(container):
			return None, None

		project_name = ComposeDetector.get_project_name(container)
		service_name = ComposeDetector.get_service_name(container)
		return project_name, service_name

	def get_project_info(self, project_name):
		"""
		Obtiene información completa de un proyecto Compose.

		Args:
			project_name: Nombre del proyecto

		Returns:
			ComposeProjectInfo: Información del proyecto o None si no existe
		"""
		return self.compose_manager.get_project_info(project_name)

	# ========== END COMPOSE PROJECT METHODS ==========

	def stop_container(self, container_id, container_name, from_schedule=False):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.stop()
			# Send confirmation only for manual commands when muted
			if from_schedule is False and is_muted():
				send_message_to_notification_channel(message=get_text("stopped_container", container_name))
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
			# Send confirmation only for manual commands when muted
			if from_schedule is False and is_muted():
				send_message_to_notification_channel(message=get_text("restarted_container", container_name))
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
			# Send confirmation only for manual commands when muted
			if from_schedule is False and is_muted():
				send_message_to_notification_channel(message=get_text("started_container", container_name))
			return None
		except Exception as e:
			error(f"Could not start container {container_name}. Error: [{e}]")
			return get_text("error_starting_container", container_name)

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			logs = container.logs().decode("utf-8")
			return get_text("showing_logs", container_name, logs[-3500:])
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
		Obtiene información formateada de un proyecto Compose para mostrar al usuario.

		Args:
			project_name: Nombre del proyecto

		Returns:
			str: Texto formateado con la información del proyecto
		"""
		try:
			project_info = self.get_project_info(project_name)
			if not project_info:
				return get_text("error_project_not_found", project_name)

			# Información básica
			text = f'📦 <b>{get_text("project_info_title", project_name)}</b>\n\n'
			text += '<pre><code>'

			# Nombre del proyecto
			text += f'🏷️  {get_text("project_name")}: {project_name}\n\n'

			# Directorio de trabajo
			working_dir = project_info.get_working_dir()
			if working_dir:
				text += f'📁 {get_text("project_working_dir")}: {working_dir}\n\n'

			# Archivo de configuración
			config_files = project_info.get_config_files()
			if config_files:
				# Extraer solo el nombre del archivo
				import os
				config_file_name = os.path.basename(config_files)
				text += f'📄 {get_text("project_config_file")}: {config_file_name}\n\n'

			# Número de servicios
			service_count = project_info.get_container_count()
			text += f'🐳 {get_text("project_services_count")}: {service_count}\n\n'

			# Lista de servicios con estado e imagen
			text += f'📋 {get_text("project_services_list")}:\n'
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)

				# Obtener imagen (nombre corto)
				image_with_tag = container.attrs.get('Config', {}).get('Image', 'N/A')
				# Si la imagen es muy larga, acortarla
				if len(image_with_tag) > 30:
					image_with_tag = image_with_tag[:27] + "..."

				text += f'  {status_emoji} {service_name} ({image_with_tag})\n'

			# Dependencias entre servicios
			text += f'\n🔗 {get_text("project_dependencies")}:\n'
			has_dependencies = False
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				depends_on = container.labels.get('com.docker.compose.depends_on', '')
				if depends_on:
					has_dependencies = True
					# Formatear dependencias (pueden venir separadas por comas)
					deps = depends_on.replace(',', ', ')
					text += f'  {service_name} → {deps}\n'

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

			image_status = ""
			possible_update = False
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', 'N/A')
			if CHECK_UPDATES:
				try:
					image_status = read_container_update_status(image_with_tag, container_name)
					if image_status is None:
						image_status = ""
				except Exception as e:
					debug(f"Queried for update {container_name} and it is not available: [{e}]")

				if image_status and get_text("NEED_UPDATE_CONTAINER_TEXT") in image_status:
					possible_update = True

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

			if CHECK_UPDATES:
				text += f"\n\n{image_status}"
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
				# Self-update: use updater container
				if not tag:
					container_environment = {'CONTAINER_NAME': container_name}
				else:
					container_environment = {'CONTAINER_NAME': container_name, 'TAG': tag}
				container_volumes = {'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}}
				new_container = self.client.containers.run(
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
					telegram_group=TELEGRAM_GROUP
				)
				return result
		except Exception as e:
			error(f"Could not update container {container_name}. Error: [{e}]")
			return get_text("error_updating_container", container_name)

	def force_check_update(self, container_id):
		try:
			container = self.client.containers.get(container_id)
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', '')
			local_image = container.image.id

			try:
				remote_image = self.client.images.pull(image_with_tag)
				if not remote_image or not remote_image.id:
					error(f"Failed to pull image {image_with_tag}. Verify that the image exists in the registry.")
					image_status = ""
					save_container_update_status(image_with_tag, container.name, image_status)
					return
			except docker.errors.ImageNotFound:
				error(f"Image {image_with_tag} not found in registry. Check the image name.")
				image_status = ""
				save_container_update_status(image_with_tag, container.name, image_status)
				return
			except docker.errors.APIError as e:
				error(f"Error pulling image {image_with_tag}. Error: [{e}]")
				image_status = ""
				save_container_update_status(image_with_tag, container.name, image_status)
				return

			local_image_normalized = local_image.replace('sha256:', '')
			remote_image_normalized = remote_image.id.replace('sha256:', '')

			debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image_normalized[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image_normalized[:CONTAINER_ID_LENGTH]}]")

			if local_image_normalized != remote_image_normalized:
				debug(f"{container.name} update detected! Deleting downloaded image [{remote_image_normalized[:CONTAINER_ID_LENGTH]}]")
				try:
					self.client.images.remove(remote_image.id)
				except:
					pass # Si no se puede borrar es porque esta siendo usada por otro contenedor
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}"))
				image_status = get_text("NEED_UPDATE_CONTAINER_TEXT")
				sent_message = send_message(message=get_text("available_update", container.name), reply_markup=markup)
				# Save container cache for this notification
				if sent_message:
					save_container_cache(sent_message.chat.id, sent_message.message_id, [container])
			else:
				image_status = get_text("UPDATED_CONTAINER_TEXT")
				send_message(message=get_text("already_updated", container.name))
		except Exception as e:
			error(f"Could not check update: [{e}]")
			image_status = ""

		if image_with_tag and container and container.name:
			save_container_update_status(image_with_tag, container.name, image_status)

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
			exec_command = ['sh', '-c', command]
			result = container.exec_run(exec_command)
			output = result.output.decode('utf-8')
			if not output:
				output = get_text("command_executed_without_output")
			return output
		except Exception as e:
			error(f"Error executing command [{command}] in container {container_name}. Error: [{e}]")
			return get_text("error_executing_command_container", command, container_name)

# Instanciamos el DockerManager
docker_manager = DockerManager()

class DockerEventMonitor:
	def __init__(self):
		self.client = docker.from_env()

	def detectar_eventos_contenedores(self):
		for event in self.client.events(decode=True):
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
			elif action == "create" and EXTENDED_MESSAGES:
				message = get_text("created_container", container_name)

			if message:
				try:
					if is_muted():
						debug(f"Message [{message}] omitted because muted")
						continue

					send_message_to_notification_channel(message=message)
				except Exception as e:
					error(f"Could not send notification [{message}]. Error: [{e}]")
					time.sleep(20) # Posible saturación de Telegram y el send_message lanza excepción

	def _event_loop_with_retry(self):
		"""Event loop wrapper with automatic retry on failure."""
		max_retries = 5
		retry_count = 0

		while retry_count < max_retries:
			try:
				debug("Event monitor: Starting event listener...")
				self.detectar_eventos_contenedores()
				# If we get here, the event stream ended normally (shouldn't happen)
				debug("Event monitor: Event stream ended unexpectedly, restarting...")
				retry_count += 1
			except Exception as e:
				retry_count += 1
				if retry_count >= max_retries:
					error(f"Event monitor failed {max_retries} times. Stopping. Last error: [{e}]")
					return
				error(f"Event monitor error (attempt {retry_count}/{max_retries}). Retrying in 5 seconds... Error: [{e}]")
				time.sleep(5)
				# Reconnect to Docker
				try:
					self.client = docker.from_env()
				except Exception as reconnect_error:
					error(f"Event monitor: Failed to reconnect to Docker: {reconnect_error}")

	def demonio_event(self):
		"""Start event daemon in a background thread."""
		thread = threading.Thread(target=self._event_loop_with_retry, daemon=True)
		thread.start()
		debug("Event monitor daemon started")


class DockerUpdateMonitor:
	def __init__(self):
		self.client = docker.from_env()

	def detectar_actualizaciones(self):
		while True:
			containers = self.client.containers.list(all=True)
			grouped_updates_containers = []
			should_notify = False
			for container in containers:
				if (container.status == "exited" or container.status == "dead") and not CHECK_UPDATE_STOPPED_CONTAINERS:
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
					remote_image = self.client.images.pull(image_with_tag)
					debug(f"Checking update: {container.name} ({image_with_tag}): LOCAL IMAGE [{local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}] - REMOTE IMAGE [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}]")
					if local_image != remote_image.id:
						if LABEL_AUTO_UPDATE in labels:
							if EXTENDED_MESSAGES and not is_muted():
								send_message_to_notification_channel(message=get_text("auto_update", container.name))
							debug(f"Auto-updating container {container.name}")
							x = None
							if not is_muted():
								x = send_message_to_notification_channel(message=get_text("updating", container.name))
							result = docker_manager.update(container_id=container.id, container_name=container.name, message=x, bot=bot)
							if not is_muted():
								delete_message(x.message_id)
								send_message_to_notification_channel(message=result)
							else:
								debug(f"Message [{result}] omitted because muted")
							continue
						old_image_status = read_container_update_status(image_with_tag, container.name)
						image_status = get_text("NEED_UPDATE_CONTAINER_TEXT")
						debug(f"{container.name} update detected! Deleting downloaded image [{remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]}]")
						try:
							self.client.images.remove(remote_image.id)
						except:
							pass # Si no se puede borrar es porque esta siendo usada por otro contenedor

						if container.name != CONTAINER_NAME:
							grouped_updates_containers.append(container.name)

						if image_status == old_image_status:
							debug("Update already notified")
							continue

						if container.name == CONTAINER_NAME:
							markup = InlineKeyboardMarkup(row_width = 1)
							markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}"))
							if not is_muted():
								sent_message = send_message(message=get_text("available_update", container.name), reply_markup=markup)
								# Save container cache for this notification
								if sent_message:
									save_container_cache(sent_message.chat.id, sent_message.message_id, [container])
							else:
								debug(f"Message [{get_text('available_update', container.name)}] omitted because muted")
							continue

						should_notify = True
					else: # Contenedor actualizado
						image_status = get_text("UPDATED_CONTAINER_TEXT")
				except Exception as e:
					error(f"Could not check update: [{e}]")
					image_status = ""
				save_container_update_status(image_with_tag, container.name, image_status)

			if grouped_updates_containers and should_notify:
				markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
				markup.add(*[
					InlineKeyboardButton(f'{ICON_CONTAINER_MARK_FOR_UPDATE} {name}', callback_data=f'toggleUpdate|{name}')
					for name in grouped_updates_containers
				])
				markup.add(
					InlineKeyboardButton(get_text("button_update_all"), callback_data="toggleUpdateAll"),
					InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar")
				)
				if not is_muted():
					message = send_message(message=get_text("available_updates", len(grouped_updates_containers)), reply_markup=markup)
					if message:
						save_update_data(TELEGRAM_GROUP, message.message_id, grouped_updates_containers)
				else:
					debug(f"Message [{get_text('available_updates', len(grouped_updates_containers))}] omitted because muted")
			debug(f"Waiting {CHECK_UPDATE_EVERY_HOURS} hours for the next update check...")
			time.sleep(CHECK_UPDATE_EVERY_HOURS * 3600)

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

	def _execute_action(self, data, line=None):
		"""
		DEPRECATED: Use _execute_schedule_action instead.
		This method is kept for backward compatibility only.
		"""
		return self._execute_schedule_action(data, line)

	def _execute_schedule_action(self, schedule: dict, line: str = None):
		"""
		Execute a schedule action from JSON format.

		Args:
			schedule: Schedule dict from JSON
			line: Deprecated, kept for compatibility but not used

		Returns:
			True if successful, False if failed
		"""
		try:
			action = schedule.get("action", "").lower()
			container = schedule.get("container", "")
			minutes = schedule.get("minutes")
			command = schedule.get("command", "")
			show_output = bool(schedule.get("show_output", False))
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
				containerId = get_container_id_by_name(container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				run(containerId, container, from_schedule=True)

			elif action == "stop":
				containerId = get_container_id_by_name(container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				stop(containerId, container, from_schedule=True)

			elif action == "restart":
				containerId = get_container_id_by_name(container)
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
				containerId = get_container_id_by_name(container)
				if not containerId:
					return handle_error(f"Container {container} not found for action {action}")
				execute_command(containerId, container, command, show_output)

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

def _build_schedule_summary(state: dict, include_step: bool = False, current_step: int = None, total_steps: int = None) -> str:
	"""Build a consistent schedule summary message from state dict"""
	lines = []

	# Add step indicator if requested
	if include_step and current_step and total_steps:
		lines.append(f"<i>Paso {current_step}/{total_steps}</i>\n")

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
	# Only show show_output if action is exec and show_output is not None
	if state.get("action") == "exec" and state.get("show_output") is not None:
		lines.append(f"<b>{get_text('schedule_label_show_output')}:</b> {get_text('schedule_yes') if state.get('show_output') else get_text('schedule_no')}")
	if state.get("command"):
		lines.append(f"<b>{get_text('schedule_label_command')}:</b> {state.get('command')}")

	return "\n".join(lines)

def _validate_containers_available() -> bool:
	"""Check if there are containers available (excluding bot container)"""
	containers = docker_manager.list_containers()
	available = [c for c in containers if c.name != CONTAINER_NAME]
	return len(available) > 0

def _get_available_containers() -> list:
	"""Get list of available containers (excluding bot container)"""
	containers = docker_manager.list_containers()
	return [c for c in containers if c.name != CONTAINER_NAME]

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
			enabled = sched.get('enabled', True)

			# Build schedule entry
			status_icon = "🟢" if enabled else "🔴"
			status_text = status_enabled if enabled else status_disabled

			lines.append(f"\n<b>{idx}. {name}</b>")
			lines.append(f"  {label_status}: <b>{status_icon} {status_text}</b>")
			lines.append(f"  {label_cron}: <code>{cron}</code>")
			lines.append(f"  {label_action}: <b>{action}</b>")

			# Add action-specific details
			if action == 'mute':
				lines.append(f"  {label_minutes}: <b>{minutes}</b>")
			elif action == 'exec':
				lines.append(f"  {label_container}: <b>{container}</b>")
				lines.append(f"  {label_command}: <code>{command}</code>")
				output_text = yes_text if show_output else no_text
				lines.append(f"  {label_show_output}: <b>{output_text}</b>")
			elif action in ('run', 'stop', 'restart'):
				lines.append(f"  {label_container}: <b>{container}</b>")

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
	status_text = get_text('schedule_status_enabled') if enabled else get_text('schedule_status_disabled')
	status_icon = "🟢" if enabled else "🔴"

	# Build message with schedule details
	message_text = f"<b>{schedule_name}</b>\n\n"
	message_text += f"<b>{get_text('schedule_label_status')}:</b> {status_icon} {status_text}\n"
	message_text += f"<b>{get_text('schedule_label_cron')}:</b> <code>{cron}</code>\n"
	message_text += f"<b>{get_text('schedule_label_action')}:</b> <b>{action}</b>\n"

	if action == 'mute':
		message_text += f"<b>{get_text('schedule_label_minutes')}:</b> <b>{minutes}</b>\n"
	elif action == 'exec':
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{container}</b>\n"
		message_text += f"<b>{get_text('schedule_label_command')}:</b> <code>{command}</code>\n"
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> <b>{get_text('schedule_yes') if show_output else get_text('schedule_no')}</b>\n"
	elif action in ('run', 'stop', 'restart'):
		message_text += f"<b>{get_text('schedule_label_container')}:</b> <b>{container}</b>\n"

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
		markup.add(InlineKeyboardButton(get_text("schedule_edit_show_output"), callback_data=f"scheduleEditField|show_output|{schedule_id}"))

	# Add status toggle button
	status_button_text = get_text("schedule_button_disable") if enabled else get_text("schedule_button_enable")
	markup.add(InlineKeyboardButton(status_button_text, callback_data=f"scheduleEditStatus|{schedule_id}"))

	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	send_message(message=message_text, reply_markup=markup)

def ask_schedule_name(user_id: int):
	"""Ask user for schedule name"""
	state = init_add_schedule_state()

	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	message_text = get_text("schedule_ask_name")

	msg = send_message(message=message_text, reply_markup=markup)
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
		# Store container mapping in state to avoid callback length issues (64 char limit)
		for idx, container in enumerate(available_containers):
			markup.add(InlineKeyboardButton(container.name, callback_data=f"scheduleSelectContainer|{idx}"))
			schedule_state[f"container_{idx}"] = container.name
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		msg = send_message(message=message_text, reply_markup=markup)
		schedule_state["last_message_id"] = msg.message_id if msg else None
		save_schedule_state(user_id, schedule_state)

def is_valid_cron(cron_expr: str) -> bool:
	"""Validate cron expression"""
	try:
		croniter(cron_expr)
		return True
	except:
		return False

def confirm_schedule_creation(user_id: int, state: dict):
	"""Show confirmation of schedule creation"""
	name = state.get("name")
	cron = state.get("cron")
	action = state.get("action")
	container = state.get("container")
	minutes = state.get("minutes")
	show_output = state.get("show_output")
	command = state.get("command")

	# Delete previous message if exists
	if state.get("last_message_id"):
		try:
			delete_message(state.get("last_message_id"))
		except:
			pass

	message_text = get_text("schedule_confirm_title") + "\n\n"
	message_text += f"<b>{get_text('schedule_label_name')}:</b> {name}\n"
	message_text += f"<b>{get_text('schedule_label_cron')}:</b> {cron}\n"
	message_text += f"<b>{get_text('schedule_label_action')}:</b> {action}\n"

	if container:
		message_text += f"<b>{get_text('schedule_label_container')}:</b> {container}\n"
	if minutes is not None:  # Use is not None to handle 0 correctly
		message_text += f"<b>{get_text('schedule_label_minutes')}:</b> {minutes}\n"
	# Only show output option for exec action
	if action == "exec" and show_output is not None:
		message_text += f"<b>{get_text('schedule_label_show_output')}:</b> {get_text('schedule_yes') if show_output else get_text('schedule_no')}\n"
	if command:
		message_text += f"<b>{get_text('schedule_label_command')}:</b> {command}\n"

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
			InlineKeyboardButton("exec", callback_data="scheduleSelectAction|exec")
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

@bot.message_handler(commands=["start", "list", "run", "stop", "restart", "delete", "exec", "checkupdate", "updateall", "changetag", "logs", "logfile", "compose", "mute", "schedule", "info", "version", "donate", "donors", "prune", "ports"])
def command_controller(message):
	userId = message.from_user.id
	comando = message.text.split(' ', 1)[0]
	messageId = message.id
	container_id = None
	if not comando in ('/mute', f'/mute@{bot.get_me().username}'
					,'/schedule', f'/schedule@{bot.get_me().username}'):
		container_name = " ".join(message.text.split()[1:])
		if container_name:
			container_id = get_container_id_by_name(container_name, debugging=True)

	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1
	debug(f"COMMAND: {comando} | USER: {userId} | CHAT: {message.chat.id} | THREAD: {message_thread_id}")

	if message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if not is_admin(userId):
		warning(f"User {userId} ({message.from_user.username}) tried to use admin command without permission")
		send_message(chat_id=userId, message=get_text("user_not_admin"))
		return

	if comando not in ('/start', f'/start@{bot.get_me().username}'):
		delete_message(messageId)

	# Listar contenedores
	if comando in ('/start', f'/start@{bot.get_me().username}'):
		texto_inicial = get_text("menu")
		send_message(message=texto_inicial)
	elif comando in ('/list', f'/list@{bot.get_me().username}'):
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
		containers = docker_manager.list_containers(comando=comando)
		send_message(message=display_containers(containers), reply_markup=markup)
	elif comando in ('/run', f'/run@{bot.get_me().username}'):
		if container_id:
			run(container_id, container_name)
		else:
			# Get ALL containers to show projects with all containers, but filter standalone to only stopped
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_start"))
				return

			# Use hierarchical keyboard with filters:
			# - Standalone: only stopped/paused/exited/created
			# - Projects: hide if ALL containers are running/restarting
			markup, standalone_containers = build_hierarchical_keyboard(
				containers, "Run", CONTAINER_NAME,
				filter_standalone_status=['exited', 'stopped', 'paused', 'created'],
				filter_projects_with_all_status=['running', 'restarting']
			)
			sent_message = send_message(message=get_text("start_a_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/stop', f'/stop@{bot.get_me().username}'):
		if container_id:
			stop(container_id, container_name)
		else:
			# Get ALL containers to show projects with all containers, but filter standalone to only running
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_stop"))
				return

			# Use hierarchical keyboard with filters:
			# - Standalone: only running/restarting
			# - Projects: hide if ALL containers are stopped/paused/exited/created
			markup, standalone_containers = build_hierarchical_keyboard(
				containers, "Stop", CONTAINER_NAME,
				filter_standalone_status=['running', 'restarting'],
				filter_projects_with_all_status=['exited', 'stopped', 'paused', 'created']
			)
			sent_message = send_message(message=get_text("stop_a_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/restart', f'/restart@{bot.get_me().username}'):
		if container_id:
			restart(container_id, container_name)
		else:
			# Get ALL containers (not just running) to show with status indicators
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_restart"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			markup, standalone_containers = build_hierarchical_keyboard(containers, "Restart", CONTAINER_NAME)
			sent_message = send_message(message=get_text("restart_a_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/logs', f'/logs@{bot.get_me().username}'):
		if container_id:
			logs(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_logs"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for logs (can't get logs from whole project)
			# Filter: show all containers (you can see logs from any container)
			# Don't exclude bot container for logs (we want to see bot logs too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Logs",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("logs_command_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/logfile', f'/logfile@{bot.get_me().username}'):
		if container_id:
			log_file(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_logs"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for logfile (can't get logfile from whole project)
			# Filter: show all containers (you can get logfile from any container)
			# Don't exclude bot container for logfile (we want to see bot logfile too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Logfile",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("show_logsfile"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/compose', f'/compose@{bot.get_me().username}'):
		if container_id:
			compose(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("error_no_containers_available"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for compose (can't get compose file from whole project)
			# Filter: show all containers (you can get compose file from any container)
			# Don't exclude bot container for compose (we want to see bot compose too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Compose",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("show_compose"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/mute', f'/mute@{bot.get_me().username}'):
		try:
			minutes = int(message.text.split()[1])
		except (IndexError, ValueError):
			send_message(message=get_text("error_use_mute_command"))
			return
		mute(minutes)
	elif comando in ('/schedule', f'/schedule@{bot.get_me().username}'):
		show_schedule_menu(userId, message.chat.id)
	elif comando in ('/info', f'/info@{bot.get_me().username}'):
		if container_id:
			info(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_info"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for info (can't get info from whole project)
			# Filter: show all containers (you can see info from any container)
			# Don't exclude bot container (we want to see bot info too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Info",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("info_command_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/exec', f'/exec@{bot.get_me().username}'):
		if container_id:
			ask_command(userId, container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_exec"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for exec (can't exec on whole project)
			# Filter: only show running/restarting containers and projects with at least one running container
			# Don't exclude bot container (we want to exec into the bot too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Exec",
				None,  # Don't exclude any container
				filter_standalone_status=['running', 'restarting'],
				filter_projects_with_all_status=['exited', 'paused', 'dead', 'created']
			)
			sent_message = send_message(message=get_text("exec_command_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/delete', f'/delete@{bot.get_me().username}'):
		if container_id:
			confirm_delete(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_delete"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			markup, standalone_containers = build_hierarchical_keyboard(containers, "Delete", CONTAINER_NAME)
			sent_message = send_message(message=get_text("delete_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/checkupdate', f'/checkupdate@{bot.get_me().username}'):
		if container_id:
			docker_manager.force_check_update(container_id)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_checkupdate"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for checkupdate (can't check updates on whole project)
			# Filter: show all containers (you can check updates on any container)
			# Don't exclude bot container (we want to check bot updates too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"CheckUpdate",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("checkupdate_command_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/updateall', f'/updateall@{bot.get_me().username}'):
		containers = docker_manager.list_containers()
		containersToUpdate = []
		for container in containers:
			if update_available(container):
				containersToUpdate.append(container.name)
		if not containersToUpdate:
			send_message(message=get_text("already_updated_all"))
			return

		markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
		markup.add(*[
			InlineKeyboardButton(f'{ICON_CONTAINER_MARK_FOR_UPDATE} {name}', callback_data=f'toggleUpdate|{name}')
			for name in containersToUpdate
		])
		markup.add(
			InlineKeyboardButton(get_text("button_update_all"), callback_data="toggleUpdateAll"),
			InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar")
		)
		message = send_message(message=get_text("available_updates", len(containersToUpdate)), reply_markup=markup)
		if message:
			save_update_data(TELEGRAM_GROUP, message.message_id, containersToUpdate)

	elif comando in ('/changetag', f'/changetag@{bot.get_me().username}'):
		if container_id:
			change_tag_container(container_id, container_name)
		else:
			# Get ALL containers to show projects and standalone
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("error_no_containers_available"))
				return

			# Use hierarchical keyboard (Level 1: projects + standalone containers)
			# No project-level action for changetag (can't change tag for whole project)
			# Filter: show all containers (you can change tag on any container)
			# Don't exclude bot container (we want to change bot tag too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"ChangeTag",
				None  # Don't exclude any container
			)
			sent_message = send_message(message=get_text("change_tag_container"), reply_markup=markup)
			# Save container cache for standalone containers
			if sent_message and standalone_containers:
				save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
	elif comando in ('/prune', f'/prune@{bot.get_me().username}'):
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			botones.append(InlineKeyboardButton(get_text("button_containers"), callback_data=f'prune|confirmPruneContainers'))
			botones.append(InlineKeyboardButton(get_text("button_images"), callback_data=f'prune|confirmPruneImages'))
			botones.append(InlineKeyboardButton(get_text("button_networks"), callback_data=f'prune|confirmPruneNetworks'))
			botones.append(InlineKeyboardButton(get_text("button_volumes"), callback_data=f'prune|confirmPruneVolumes'))
			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("prune_system"), reply_markup=markup)
	elif comando in ('/version', f'/version@{bot.get_me().username}'):
		x = send_message(message=get_text("version", VERSION))
		if x:
			time.sleep(15)
			delete_message(x.message_id)

	elif comando in ('/donate', f'/donate@{bot.get_me().username}'):
		x = send_message(message=get_text("donate"))
		if x:
			time.sleep(45)
			delete_message(x.message_id)

	elif comando in ('/donors', f'/donors@{bot.get_me().username}'):
		print_donors()

	elif comando in ('/ports', f'/ports@{bot.get_me().username}'):
		show_container_ports()

def parse_call_data(call_data):
	parts = call_data.split("|")
	comando = parts[0]
	args = parts[1:]

	if comando not in CALL_PATTERNS:
		raise ValueError(f"COMMAND NOT IN PATTERN: {comando}")

	expected_keys = CALL_PATTERNS[comando]

	if len(args) != len(expected_keys):
		raise ValueError(f"INCORRECT LENGTH CALLBACK DATA FOR '{comando}': IT WAS EXPECTED {len(expected_keys)}")

	parsed = {"comando": comando}
	parsed.update(dict(zip(expected_keys, args)))
	return parsed

@bot.callback_query_handler(func=lambda mensaje: True)
def button_controller(call):
	try:
		messageId = call.message.id
		chatId = call.message.chat.id
		userId = call.from_user.id

		# Responder inmediatamente al callback para evitar timeout
		bot.answer_callback_query(call.id, show_alert=False)

		if not is_admin(userId):
			warning(f"User {userId} ({call.from_user.username}) tried to use admin command without permission")
			send_message(chat_id=userId, message=get_text("user_not_admin"))
			return

		data = parse_call_data(call.data)
		comando = data["comando"]
		containerId = data.get("containerId")
		containerName = data.get("containerName")  # For toggles and project names
		tag = data.get("tag")
		action = data.get("action")
		containerIdx = data.get("containerIdx")
		originalMessageId = data.get("originalMessageId")
		commandId = data.get("commandId")
		scheduleHash = data.get("scheduleHash")
		field = data.get("field")
		scheduleId = data.get("scheduleId")
		value = data.get("value")

		# If containerId is present but containerName is not, get it from cache/Docker
		if containerId and not containerName:
			containerName = get_container_name(chatId, messageId, containerId)
			if not containerName:
				send_message(message=get_text("container_does_not_exist", containerId))
				debug(f"Container {containerId} not found in cache or Docker")
				return

		debug(f"BUTTON: {comando} | USER: {userId} | CHAT: {chatId}")
	except Exception as e:
		error(f"Error initializing callback: [{str(e)}]")
		try:
			bot.answer_callback_query(call.id, text=get_text("error_callback_processing"), show_alert=True)
		except:
			pass
		return

	try:
		# Don't delete message for toggle actions and hierarchical navigation
		if comando not in ["toggleUpdate", "toggleUpdateAll", "enterRestartProject", "backToRestartLevel1", "enterRunProject", "backToRunLevel1", "enterStopProject", "backToStopLevel1", "enterDeleteProject", "backToDeleteLevel1", "confirmDeleteWholeProject", "enterExecProject", "backToExecLevel1", "enterLogsProject", "backToLogsLevel1", "enterCheckUpdateProject", "backToCheckUpdateLevel1", "enterInfoProject", "showProjectInfo", "backToInfoLevel1", "enterChangeTagProject", "backToChangeTagLevel1", "enterLogfileProject", "backToLogfileLevel1", "enterComposeProject", "backToComposeLevel1"]:
			delete_message(messageId)

		if call.data == "cerrar":
			# Clean up any cached data for this message
			update_data = read_cache_item(f"update_data_{chatId}_{messageId}")
			if update_data is not None:
				clear_update_data(chatId, messageId)
			# Clean up container name cache
			clear_container_cache(chatId, messageId)
			return

		# RUN
		if comando == "run":
			run(containerId, containerName)

		# STOP
		elif comando == "stop":
			stop(containerId, containerName)

		# RESTART
		elif comando == "restart":
			restart(containerId, containerName)

		# LOGS
		elif comando == "logs":
			logs(containerId, containerName)

		# LOGS EN FICHERO
		elif comando == "logfile":
			log_file(containerId, containerName)

		# COMPOSE
		elif comando == "compose":
			compose(containerId, containerName)

		# INFO
		elif comando == "info":
			info(containerId, containerName)

		# CONFIRM UPDATE
		elif comando == "confirmUpdate":
			confirm_update(containerId, containerName)

		# CHECK UPDATE
		elif comando == "checkUpdate":
			docker_manager.force_check_update(containerId)

		# UPDATE
		elif comando == "update":
			update(containerId, containerName)

		# CONFIRM UPDATE ALL
		elif comando == "updateAll":
			containers = docker_manager.list_containers()
			for container in containers:
				if update_available(container):
					update(container.id, container.name)

		# CONFIRM DELETE
		elif comando == "confirmDelete":
			confirm_delete(containerId, containerName)

		# ASK FOR COMMAND
		elif comando == "askCommand":
			ask_command(userId, containerId, containerName)

		# EXEC
		elif comando == "exec":
			command = load_command_cache(commandId)
			clear_command_cache(commandId)
			if command is not None:
				execute_command(containerId, containerName, command)
			else:
				error(f"Command cache not found for ID: {commandId}")
				send_message(message=get_text("error_callback_processing"))

		# CANCEL ASK
		elif comando == "cancelAskCommand":
			clear_command_request_state(userId)

		# CANCEL EXEC
		elif comando == "cancelExec":
			clear_command_cache(commandId)

		# DELETE
		elif comando == "delete":
			x = send_message(message=get_text("deleting", containerName))
			result = docker_manager.delete(container_id=containerId, container_name=containerName)
			delete_message(x.message_id)
			send_message(message=result)

		# CHANGE_TAG_CONTAINER
		elif comando == "changeTagContainer":
			change_tag_container(containerId, containerName)

		# CHANGE_TAG_CONTAINER
		elif comando == "confirmChangeTag":
			# Get container name from cache or Docker
			container = get_container_from_cache_or_docker(chatId, messageId, containerId)
			containerName = container.name if container else "Unknown"
			confirm_change_tag(containerId, containerName, tag)

		# CHANGE_TAG
		elif comando == "changeTag":
			# Get container name from cache or Docker
			container = get_container_from_cache_or_docker(chatId, messageId, containerId)
			containerName = container.name if container else "Unknown"
			x = send_message(message=get_text("updating", containerName))
			result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot, tag=tag)
			delete_message(x.message_id)
			send_message(message=result)

		# DELETE SCHEDULE
		elif comando == "deleteSchedule":
			schedules = schedule_manager.get_all_schedules()
			idx = _validate_schedule_index(scheduleHash, schedules)
			if idx >= 0:
				schedule_to_delete = schedules[idx]
				schedule_manager.delete_schedule(schedule_to_delete["name"])
				send_message(message=get_text("deleted_schedule", schedule_to_delete["name"]))
			else:
				send_message(message=get_text("error_schedule_not_found"))

		# MARCAR COMO UPDATE
		elif comando == "toggleUpdate":
			containers, selected = load_update_data(chatId, messageId)
			if containerName in selected:
				selected.remove(containerName)
			else:
				selected.add(containerName)
			save_update_data(chatId, messageId, containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Update", get_text("button_update"), get_text("button_update_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# MARCAR COMO UPDATE TODOS
		elif comando == "toggleUpdateAll":
			containers, selected = load_update_data(chatId, messageId)
			for container in containers:
				if container not in selected:
					selected.add(container)
			save_update_data(chatId, messageId, containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Update", get_text("button_update"), get_text("button_update_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# CONFIRM UPDATE SELECTED
		elif comando == "confirmUpdateSelected":
			confirm_update_selected(chatId, messageId)

		# UPDATE SELECTED
		elif comando == "updateSelected":
			containers, selected = load_update_data(chatId, originalMessageId)
			for containerName in selected:
				container_id = get_container_id_by_name(container_name=containerName)
				if not container_id:
					send_message(message=get_text("container_does_not_exist", containerName))
					debug(f"Container {containerName} not found")
					continue
				client = docker.from_env()
				container = client.containers.get(container_id)
				if update_available(container):
					update(container.id, container.name)
			clear_update_data(chatId, originalMessageId)


		# ENTER RESTART PROJECT (Level 2: show project containers)
		elif comando == "enterRestartProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]

				# Determine status indicator
				status_indicator = "🟢" if container.status in ['running', 'restarting'] else "🔴"

				# Determine callback action based on status
				if container.status in ['running', 'restarting']:
					callback_action = "restart"
				else:
					callback_action = "run"

				botones.append(
					InlineKeyboardButton(
						f"🐳{status_indicator} {service_name}",
						callback_data=f"{callback_action}|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			# Add container buttons to markup
			markup.add(*botones)

			# Add bottom row with "Restart whole project" and "Back" buttons side by side
			markup.add(
				InlineKeyboardButton(
					f"🔄 {get_text('button_restart_project')}",
					callback_data=f"restartWholeProject|{project_name}"
				),
				InlineKeyboardButton(
					get_text('button_back'),
					callback_data="backToRestartLevel1"
				)
			)
			# Save container cache for this message
			save_container_cache(chatId, messageId, project_info.containers)
			edit_message_text(
				get_text("select_container_or_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO RESTART LEVEL 1
		elif comando == "backToRestartLevel1":
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_restart"))
				return

			markup, standalone_containers = build_hierarchical_keyboard(containers, "Restart", CONTAINER_NAME)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("restart_a_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# RESTART WHOLE PROJECT
		elif comando == "restartWholeProject":
			project_name = containerName
			restart_compose_project(project_name)

		# ENTER RUN PROJECT (Level 2: show project containers)
		elif comando == "enterRunProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]

				# Determine status indicator
				status_indicator = "🟢" if container.status in ['running', 'restarting'] else "🔴"

				# Determine callback action based on status
				if container.status in ['running', 'restarting']:
					callback_action = "restart"
				else:
					callback_action = "run"

				botones.append(
					InlineKeyboardButton(
						f"🐳{status_indicator} {service_name}",
						callback_data=f"{callback_action}|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			# Add container buttons to markup
			markup.add(*botones)

			# Add bottom row with "Start whole project" and "Back" buttons side by side
			markup.add(
				InlineKeyboardButton(
					f"▶️ {get_text('button_run_project')}",
					callback_data=f"runWholeProject|{project_name}"
				),
				InlineKeyboardButton(
					get_text('button_back'),
					callback_data="backToRunLevel1"
				)
			)
			# Save container cache for this message
			save_container_cache(chatId, messageId, project_info.containers)
			edit_message_text(
				get_text("select_container_or_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO RUN LEVEL 1
		elif comando == "backToRunLevel1":
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_start"))
				return

			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Run",
				CONTAINER_NAME,
				filter_standalone_status=['exited', 'stopped', 'paused', 'created'],
				filter_projects_with_all_status=['running', 'restarting']
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("start_a_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# RUN WHOLE PROJECT
		elif comando == "runWholeProject":
			project_name = containerName
			run_compose_project(project_name)

		# ENTER STOP PROJECT (Level 2: show project containers)
		elif comando == "enterStopProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]

				# Determine status indicator
				status_indicator = "🟢" if container.status in ['running', 'restarting'] else "🔴"

				# Determine callback action based on status
				if container.status in ['running', 'restarting']:
					callback_action = "stop"
				else:
					callback_action = "run"

				botones.append(
					InlineKeyboardButton(
						f"🐳{status_indicator} {service_name}",
						callback_data=f"{callback_action}|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			# Add container buttons to markup
			markup.add(*botones)

			# Add bottom row with "Stop whole project" and "Back" buttons side by side
			markup.add(
				InlineKeyboardButton(
					f"⏹️ {get_text('button_stop_project')}",
					callback_data=f"stopWholeProject|{project_name}"
				),
				InlineKeyboardButton(
					get_text('button_back'),
					callback_data="backToStopLevel1"
				)
			)
			# Save container cache for this message
			save_container_cache(chatId, messageId, project_info.containers)
			edit_message_text(
				get_text("select_container_or_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO STOP LEVEL 1
		elif comando == "backToStopLevel1":
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_stop"))
				return

			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Stop",
				CONTAINER_NAME,
				filter_standalone_status=['running', 'restarting'],
				filter_projects_with_all_status=['exited', 'stopped', 'paused', 'created']
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("stop_a_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# STOP WHOLE PROJECT
		elif comando == "stopWholeProject":
			project_name = containerName
			stop_compose_project(project_name)

		# ENTER DELETE PROJECT (Level 2: show project containers)
		elif comando == "enterDeleteProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]

				# Determine status indicator
				status_indicator = get_status_emoji(container.status, container.name, container)

				botones.append(
					InlineKeyboardButton(
						f"🐳{status_indicator} {service_name}",
						callback_data=f"confirmDelete|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			# Add container buttons to markup
			markup.add(*botones)

			# Add bottom row with "Delete whole project" and "Back" buttons side by side
			markup.add(
				InlineKeyboardButton(
					f"🗑️ {get_text('button_delete_project')}",
					callback_data=f"confirmDeleteWholeProject|{project_name}"
				),
				InlineKeyboardButton(
					get_text('button_back'),
					callback_data="backToDeleteLevel1"
				)
			)
			# Save container cache for this message
			save_container_cache(chatId, messageId, project_info.containers)
			edit_message_text(
				get_text("select_container_or_project_delete", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO DELETE LEVEL 1
		elif comando == "backToDeleteLevel1":
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_to_delete"))
				return

			markup, standalone_containers = build_hierarchical_keyboard(containers, "Delete", CONTAINER_NAME)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("delete_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER EXEC PROJECT (Level 2: show project containers)
		elif comando == "enterExecProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			# Only show running/restarting containers
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]

				# Filter: only show running/restarting containers
				if container.status not in ['running', 'restarting']:
					continue

				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"askCommand|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToExecLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO EXEC LEVEL 1
		elif comando == "backToExecLevel1":
			containers = docker_manager.list_containers()
			if not containers or all(c.name == CONTAINER_NAME for c in containers):
				send_message(message=get_text("no_containers_for_exec"))
				return

			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Exec",
				CONTAINER_NAME,
				filter_standalone_status=['running', 'restarting'],
				filter_projects_with_all_status=['exited', 'paused', 'dead', 'created']
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("exec_command_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER LOGS PROJECT (Level 2: show project containers)
		elif comando == "enterLogsProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"logs|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToLogsLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO LOGS LEVEL 1
		elif comando == "backToLogsLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_logs"))
				return

			# Don't exclude bot container for logs (we want to see bot logs too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Logs",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("logs_command_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER LOGFILE PROJECT (Level 2: show project containers)
		elif comando == "enterLogfileProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"logfile|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToLogfileLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO LOGFILE LEVEL 1
		elif comando == "backToLogfileLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_logs"))
				return

			# Don't exclude bot container for logfile (we want to see bot logfile too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Logfile",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("show_logsfile"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER COMPOSE PROJECT (Level 2: show project containers)
		elif comando == "enterComposeProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"compose|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToComposeLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO COMPOSE LEVEL 1
		elif comando == "backToComposeLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("error_no_containers_available"))
				return

			# Don't exclude bot container for compose (we want to see bot compose too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Compose",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("show_compose"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER CHECKUPDATE PROJECT (Level 2: show project containers)
		elif comando == "enterCheckUpdateProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				update_emoji = get_update_emoji(container.name)
				botones.append(
					InlineKeyboardButton(
						f"{update_emoji} {service_name}",
						callback_data=f"checkUpdate|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToCheckUpdateLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO CHECKUPDATE LEVEL 1
		elif comando == "backToCheckUpdateLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_checkupdate"))
				return

			# Don't exclude bot container (we want to check bot updates too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"CheckUpdate",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("checkupdate_command_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER INFO PROJECT (Level 2: show project containers)
		elif comando == "enterInfoProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"info|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add "View project info" and "Back" buttons in the same row
			markup.row(
				InlineKeyboardButton(
					get_text("button_view_project_info"),
					callback_data=f"showProjectInfo|{project_name}"
				),
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToInfoLevel1"
				)
			)

			# Save container cache for this project
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# SHOW PROJECT INFO (display project information)
		elif comando == "showProjectInfo":
			project_name = containerName

			# Get formatted project info
			info_text = docker_manager.get_project_info_formatted(project_name)

			# Build keyboard with close button
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(
				InlineKeyboardButton(
					get_text("button_close"),
					callback_data="cerrar"
				)
			)

			edit_message_text(
				info_text,
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO INFO LEVEL 1
		elif comando == "backToInfoLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("no_containers_for_info"))
				return

			# Don't exclude bot container (we want to see bot info too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"Info",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("info_command_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# ENTER CHANGETAG PROJECT (Level 2: show project containers)
		elif comando == "enterChangeTagProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			# Build Level 2 keyboard
			markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
			botones = []

			# Add individual container buttons (sorted by service name)
			for service_name in sorted(project_info.get_service_names()):
				container = project_info.services[service_name]
				status_emoji = get_status_emoji(container.status, container.name, container)
				botones.append(
					InlineKeyboardButton(
						f"{status_emoji} {service_name}",
						callback_data=f"changeTagContainer|{container.id[:CONTAINER_ID_LENGTH]}"
					)
				)

			markup.add(*botones)

			# Add back button
			markup.add(
				InlineKeyboardButton(
					get_text("button_back"),
					callback_data="backToChangeTagLevel1"
				)
			)

			# Save container cache for project containers
			save_container_cache(chatId, messageId, project_info.containers)

			edit_message_text(
				get_text("select_container_from_project", project_name),
				chatId,
				messageId,
				reply_markup=markup
			)

		# BACK TO CHANGETAG LEVEL 1
		elif comando == "backToChangeTagLevel1":
			containers = docker_manager.list_containers()
			if not containers:
				send_message(message=get_text("error_no_containers_available"))
				return

			# Don't exclude bot container (we want to change bot tag too)
			markup, standalone_containers = build_hierarchical_keyboard(
				containers,
				"ChangeTag",
				None  # Don't exclude any container
			)
			# Save container cache for standalone containers
			if standalone_containers:
				save_container_cache(chatId, messageId, standalone_containers)
			edit_message_text(
				get_text("change_tag_container"),
				chatId,
				messageId,
				reply_markup=markup
			)

		# CONFIRM DELETE WHOLE PROJECT (show warning)
		elif comando == "confirmDeleteWholeProject":
			project_name = containerName
			project_info = docker_manager.get_project_info(project_name)

			if not project_info:
				send_message(message=get_text("error_project_not_found", project_name))
				return

			container_count = project_info.get_container_count()
			markup = InlineKeyboardMarkup(row_width=2)
			markup.add(
				InlineKeyboardButton(
					f"✅ {get_text('button_yes_delete')}",
					callback_data=f"deleteWholeProject|{project_name}"
				),
				InlineKeyboardButton(
					get_text('button_cancel'),
					callback_data="backToDeleteLevel1"
				)
			)
			edit_message_text(
				get_text("confirm_delete_project", project_name, container_count),
				chatId,
				messageId,
				reply_markup=markup
			)

		# DELETE WHOLE PROJECT
		elif comando == "deleteWholeProject":
			project_name = containerName
			delete_compose_project(project_name)

		# PRUNE
		elif comando == "prune":
			# PRUNE CONTAINERS
			if action == "confirmPruneContainers":
				confirm_prune_containers()
			elif action == "pruneContainers":
				result, data = docker_manager.prune_containers()
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
				fichero_temporal = get_temporal_file(data, get_text("button_containers"))
				x = send_message(message=get_text("loading_file"))
				send_document(document=fichero_temporal, reply_markup=markup, caption=result)
				delete_message(x.message_id)

			# PRUNE IMAGES
			elif action == "confirmPruneImages":
				confirm_prune_images()
			elif action == "pruneImages":
				result, data = docker_manager.prune_images()
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
				fichero_temporal = get_temporal_file(data, get_text("button_images"))
				x = send_message(message=get_text("loading_file"))
				send_document(document=fichero_temporal, reply_markup=markup, caption=result)
				delete_message(x.message_id)

			# PRUNE NETWORKS
			elif action == "confirmPruneNetworks":
				confirm_prune_networks()
			elif action == "pruneNetworks":
				result, data = docker_manager.prune_networks()
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
				fichero_temporal = get_temporal_file(data, get_text("button_networks"))
				x = send_message(message=get_text("loading_file"))
				send_document(document=fichero_temporal, reply_markup=markup, caption=result)
				delete_message(x.message_id)

			# PRUNE VOLUMES
			elif action == "confirmPruneVolumes":
				confirm_prune_volumes()
			elif action == "pruneVolumes":
				result, data = docker_manager.prune_volumes()
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
				fichero_temporal = get_temporal_file(data, get_text("button_volumes"))
				x = send_message(message=get_text("loading_file"))
				send_document(document=fichero_temporal, reply_markup=markup, caption=result)
				delete_message(x.message_id)

		# PORTS CALLBACKS
		elif comando == "generatePort":
			# Generate a random available port
			port = get_random_available_port()

			# Build the message with the generated port
			if port:
				result_message = get_text("ports_generated_port", port)
			else:
				result_message = get_text("ports_no_available_port")

			# Delete the original message and send a new one with the result
			delete_message(messageId, chatId)
			send_message(chat_id=chatId, message=result_message)

		# SCHEDULE CALLBACKS
		elif comando == "scheduleAdd":
			ask_schedule_name(userId)

		elif comando == "scheduleEdit":
			show_schedule_edit_list(userId, chatId)

		elif comando == "scheduleSelectEdit":
			schedules = schedule_manager.get_all_schedules()
			idx = _validate_schedule_index(action, schedules)
			if idx >= 0:
				show_schedule_edit_options(userId, schedules[idx]["name"])
			else:
				send_message(message=get_text("error_invalid_selection"))

		elif comando == "scheduleDelete":
			show_schedule_delete_list(userId, chatId)

		elif comando == "scheduleSelectDelete":
			schedules = schedule_manager.get_all_schedules()
			idx = _validate_schedule_index(scheduleHash, schedules)
			if idx >= 0:
				schedule_to_delete = schedules[idx]
				schedule_manager.delete_schedule(schedule_to_delete["name"])
				send_message(message=get_text("schedule_deleted", schedule_to_delete["name"]))
				# Show the updated schedule menu
				show_schedule_menu(userId, chatId)
			else:
				send_message(message=get_text("error_invalid_selection"))

		elif comando == "scheduleSelectToggle":
			schedules = schedule_manager.get_all_schedules()
			idx = _validate_schedule_index(scheduleHash, schedules)
			if idx >= 0:
				schedule_to_toggle = schedules[idx]
				new_status = schedule_manager.toggle_schedule(schedule_to_toggle["name"])
				if new_status is not None:
					if new_status:
						send_message(message=get_text("schedule_enabled", schedule_to_toggle["name"]))
					else:
						send_message(message=get_text("schedule_disabled", schedule_to_toggle["name"]))
				else:
					send_message(message=get_text("error_invalid_selection"))
			else:
				send_message(message=get_text("error_invalid_selection"))

		elif comando == "scheduleSelectAction":
			schedule_state = load_schedule_state(userId)
			if schedule_state:
				schedule_state["action"] = action

				# Delete previous message if exists
				if schedule_state.get("last_message_id"):
					try:
						delete_message(schedule_state.get("last_message_id"))
					except:
						pass

				if action == "mute":
					schedule_state["step"] = "ask_minutes"
					schedule_state["show_output"] = None  # Not applicable for mute

					# Build message with summary
					message_text = _build_schedule_summary(schedule_state)
					message_text += f"\n\n{get_text('schedule_ask_minutes')}"

					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					schedule_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, schedule_state)
				else:
					# For run, stop, restart, exec - ask for container
					# show_output will remain None until after container selection for exec
					schedule_state["show_output"] = None
					save_schedule_state(userId, schedule_state)
					show_schedule_container_selection(userId, action)

		elif comando == "scheduleSelectContainer":
			schedule_state = load_schedule_state(userId)
			if schedule_state:
				# Retrieve container name from state mapping (containerIdx is the index)
				container_key = f"container_{containerIdx}"
				container_name = schedule_state.get(container_key)
				if container_name:
					schedule_state["container"] = container_name
				else:
					error(f"Container not found in state for key: {container_key}")
					send_message(message=get_text("error_invalid_selection"))
					return

				# Delete previous message if exists
				if schedule_state.get("last_message_id"):
					try:
						delete_message(schedule_state.get("last_message_id"))
					except:
						pass

				# If action is exec, ask for show_output; otherwise confirm
				if schedule_state.get("action") == "exec":
					schedule_state["step"] = "ask_show_output"
					schedule_state["show_output"] = False  # Initialize for display

					# Build message with summary
					message_text = f"<b>{get_text('schedule_label_name')}:</b> {schedule_state.get('name')}\n"
					message_text += f"<b>{get_text('schedule_label_cron')}:</b> {schedule_state.get('cron')}\n"
					message_text += f"<b>{get_text('schedule_label_action')}:</b> {schedule_state.get('action')}\n"
					message_text += f"<b>{get_text('schedule_label_container')}:</b> {container_name}\n\n"
					message_text += get_text("schedule_ask_show_output")

					markup = InlineKeyboardMarkup(row_width=2)
					markup.add(
						InlineKeyboardButton(get_text("button_yes"), callback_data="scheduleSelectShowOutput|yes"),
						InlineKeyboardButton(get_text("button_no"), callback_data="scheduleSelectShowOutput|no")
					)
					msg = send_message(message=message_text, reply_markup=markup)
					schedule_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, schedule_state)
				else:
					schedule_state["step"] = "confirm"
					save_schedule_state(userId, schedule_state)
					confirm_schedule_creation(userId, schedule_state)

		elif comando == "scheduleSelectShowOutput":
			schedule_state = load_schedule_state(userId)
			if schedule_state:
				schedule_state["show_output"] = (action == "yes")
				schedule_state["step"] = "ask_command"

				# Delete previous message if exists
				if schedule_state.get("last_message_id"):
					try:
						delete_message(schedule_state.get("last_message_id"))
					except:
						pass

				# Build message with summary
				message_text = _build_schedule_summary(schedule_state)
				message_text += f"\n\n{get_text('schedule_ask_command')}"

				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
				msg = send_message(message=message_text, reply_markup=markup)
				schedule_state["last_message_id"] = msg.message_id if msg else None
				save_schedule_state(userId, schedule_state)

		elif comando == "scheduleConfirm":
			schedule_state = load_schedule_state(userId)
			if schedule_state:
				try:
					schedule_manager.add_schedule(
						name=schedule_state["name"],
						cron=schedule_state["cron"],
						action=schedule_state["action"],
						container=schedule_state.get("container"),
						minutes=schedule_state.get("minutes"),
						show_output=schedule_state.get("show_output", False),
						command=schedule_state.get("command")
					)
					send_message(message=get_text("schedule_added_success", schedule_state["name"]))
					clear_schedule_state(userId)
					# Show the updated schedule menu
					show_schedule_menu(userId, chatId)
				except Exception as e:
					send_message(message=get_text("error_adding_schedule", str(e)))
					error(f"Error adding schedule: {e}")

		elif comando == "scheduleEditField":
			if field and scheduleId:
				schedule = schedule_manager.get_schedule_by_id(int(scheduleId))
				if not schedule:
					send_message(message=get_text("error_invalid_selection"))
					return

				schedule_name = schedule.get('name', '')

				# Initialize edit state
				edit_state = {
					"schedule_name": schedule_name,
					"schedule_id": int(scheduleId),
					"field": field,
					"last_message_id": None
				}

				# Ask for the new value based on field
				if field == "name":
					message_text = f"<b>{get_text('schedule_edit_name')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_name')}\n"
					message_text += f"<i>{get_text('current_value')}: {schedule_name}</i>"

					# For text fields, ask for input
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

				elif field == "cron":
					current_cron = schedule.get('cron', '* * * * *')
					message_text = f"<b>{get_text('schedule_edit_cron')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_cron')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_cron}</i>"

					# For text fields, ask for input
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

				elif field == "container":
					current_container = schedule.get('container', '')
					message_text = f"<b>{get_text('schedule_edit_container')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_container')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_container}</i>\n\n"

					# Show container selection
					available_containers = _get_available_containers()

					if not available_containers:
						send_message(message=get_text("error_no_containers_available"))
						return

					markup = InlineKeyboardMarkup(row_width=2)
					# Store container mapping to avoid callback length issues (64 char limit)
					for idx, container in enumerate(available_containers):
						markup.add(InlineKeyboardButton(container.name, callback_data=f"scheduleEditValue|container|{scheduleId}|{idx}"))
						edit_state[f"container_{idx}"] = container.name
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

				elif field == "minutes":
					current_minutes = schedule.get('minutes', '')
					message_text = f"<b>{get_text('schedule_edit_minutes')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_minutes')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"

					# For text fields, ask for input
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

				elif field == "command":
					current_command = schedule.get('command', '')
					message_text = f"<b>{get_text('schedule_edit_command')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_command')}\n"
					message_text += f"<i>{get_text('current_value')}: {current_command}</i>"

					# For text fields, ask for input
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

				elif field == "show_output":
					current_output = schedule.get('show_output', False)
					message_text = f"<b>{get_text('schedule_edit_show_output')}</b>\n\n"
					message_text += f"{get_text('schedule_ask_show_output')}\n"
					message_text += f"<i>{get_text('current_value')}: {get_text('schedule_yes') if current_output else get_text('schedule_no')}</i>"

					markup = InlineKeyboardMarkup(row_width=2)
					markup.add(
						InlineKeyboardButton(get_text("button_yes"), callback_data=f"scheduleEditValue|show_output|{scheduleId}|yes"),
						InlineKeyboardButton(get_text("button_no"), callback_data=f"scheduleEditValue|show_output|{scheduleId}|no")
					)
					msg = send_message(message=message_text, reply_markup=markup)
					edit_state["last_message_id"] = msg.message_id if msg else None
					save_schedule_state(userId, edit_state)

		elif comando == "scheduleEditValue":
			if field and scheduleId and value:
				schedule = schedule_manager.get_schedule_by_id(int(scheduleId))
				if not schedule:
					send_message(message=get_text("error_invalid_selection"))
					return

				schedule_name = schedule.get('name', '')

				# Update the schedule based on field type
				if field == "show_output":
					schedule_manager.update_schedule(schedule_name, show_output=(value == "yes"))
					send_message(message=get_text("schedule_updated_success", schedule_name))
				elif field == "container":
					# value is now the container index, retrieve name from edit state
					edit_state = load_schedule_state(userId)
					container_name = edit_state.get(f"container_{value}") if edit_state else None

					if container_name:
						schedule_manager.update_schedule(schedule_name, container=container_name)
						send_message(message=get_text("schedule_updated_success", schedule_name))
					else:
						send_message(message=get_text("error_invalid_selection"))
						return
				elif field == "command":
					schedule_manager.update_schedule(schedule_name, command=value)
					send_message(message=get_text("schedule_updated_success", schedule_name))

				# Show the schedule menu again
				show_schedule_menu(userId, chatId)

		elif comando == "scheduleEditStatus":
			if scheduleId:
				schedule = schedule_manager.get_schedule_by_id(int(scheduleId))

				if schedule:
					schedule_name = schedule.get('name', '')

					# Toggle the status
					new_enabled = not schedule.get("enabled", True)
					schedule_manager.update_schedule(schedule_name, enabled=new_enabled)

					# Show success message
					send_message(message=get_text("schedule_updated_success", schedule_name))

					# Show the schedule menu again
					show_schedule_menu(userId, chatId)
				else:
					send_message(message=get_text("error_invalid_selection"))
			else:
				send_message(message=get_text("error_invalid_selection"))
	except Exception as e:
		error(f"Error executing callback [{comando}]: [{str(e)}]")
		try:
			send_message(message=get_text("error_callback_processing"))
		except:
			pass

@bot.message_handler(func=lambda message: True)
def handle_text(message):
	userId = message.from_user.id
	username = message.from_user.username
	pending = load_command_request_state(userId)
	schedule_state = load_schedule_state(userId)
	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1

	if message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if not is_admin(userId):
		warning(f"User {userId} ({username}) tried to use admin command without permission")
		send_message(get_text("user_not_admin"), chat_id=userId)
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
	elif schedule_state:
		handle_schedule_flow(userId, message.text.strip(), schedule_state, message.chat.id, message.message_id)
	else:
		pass

def run(containerId, containerName, from_schedule=False):
	debug(f"Running command: run for container {containerName}")
	x = send_message(message=get_text("starting", containerName))
	result = docker_manager.start_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def stop(containerId, containerName, from_schedule=False):
	debug(f"Running command: stop for container {containerName}")
	x = send_message(message=get_text("stopping", containerName))
	result = docker_manager.stop_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def restart(containerId, containerName, from_schedule=False):
	debug(f"Running command: restart for container {containerName}")
	x = send_message(message=get_text("restarting", containerName))
	result = docker_manager.restart_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def restart_compose_project(project_name):
	"""
	Reinicia un proyecto Docker Compose completo respetando el orden de dependencias.

	Args:
		project_name: Nombre del proyecto Compose a reiniciar
	"""
	debug(f"Running command: restart_compose_project for project {project_name}")

	# Obtener información del proyecto
	project_info = docker_manager.get_project_info(project_name)
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Obtener contenedores ordenados por dependencias
	containers = project_info.containers
	sorted_containers = docker_manager.compose_manager.sort_containers_by_dependencies(containers)

	# Mensaje inicial
	send_message(message=get_text("restarting_project", project_name))

	# Parar contenedores en orden inverso (los que dependen primero)
	for container in reversed(sorted_containers):
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("stopping_service", service_name))
		try:
			container.stop(timeout=10)
		except Exception as e:
			debug(f"Error stopping {service_name}: {e}")
			send_message(message=get_text("error_stopping_service", service_name))

	# Iniciar contenedores en orden correcto (dependencias primero)
	for container in sorted_containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("starting_service", service_name))
		try:
			container.start()
		except Exception as e:
			debug(f"Error starting {service_name}: {e}")
			send_message(message=get_text("error_starting_service", service_name))

	# Mensaje final
	send_message(message=get_text("project_restarted_success", project_name))

def restart_compose_project_after_update(project_name):
	"""
	Reinicia un proyecto Docker Compose completo después de actualizar un contenedor.
	Muestra mensajes informativos sobre el reinicio del proyecto.

	Args:
		project_name: Nombre del proyecto Compose a reiniciar
	"""
	debug(f"Restarting project {project_name} after container update")

	# Obtener información del proyecto
	project_info = docker_manager.get_project_info(project_name)
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Obtener contenedores ordenados por dependencias
	containers = project_info.containers
	sorted_containers = docker_manager.compose_manager.sort_containers_by_dependencies(containers)
	container_count = len(sorted_containers)

	# Mensaje inicial
	send_message(message=get_text("restarting_project_after_update", project_name, container_count))

	# Parar contenedores en orden inverso (los que dependen primero)
	for container in reversed(sorted_containers):
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("stopping_service", service_name))
		try:
			container.stop(timeout=10)
		except Exception as e:
			debug(f"Error stopping {service_name}: {e}")
			if EXTENDED_MESSAGES:
				send_message(message=get_text("error_stopping_service", service_name))

	# Iniciar contenedores en orden correcto (dependencias primero)
	for container in sorted_containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("starting_service", service_name))
		try:
			container.start()
		except Exception as e:
			debug(f"Error starting {service_name}: {e}")
			if EXTENDED_MESSAGES:
				send_message(message=get_text("error_starting_service", service_name))

	# Mensaje final
	send_message(message=get_text("project_restarted_after_update_success", project_name, container_count))

def run_compose_project(project_name):
	"""
	Inicia un proyecto Docker Compose completo respetando el orden de dependencias.

	Args:
		project_name: Nombre del proyecto Compose a iniciar
	"""
	debug(f"Running command: run_compose_project for project {project_name}")

	# Obtener información del proyecto
	project_info = docker_manager.get_project_info(project_name)
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Obtener contenedores ordenados por dependencias
	containers = project_info.containers
	sorted_containers = docker_manager.compose_manager.sort_containers_by_dependencies(containers)

	# Mensaje inicial
	send_message(message=get_text("starting_project", project_name))

	# Iniciar contenedores en orden correcto (dependencias primero)
	for container in sorted_containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("starting_service", service_name))
		try:
			container.start()
		except Exception as e:
			debug(f"Error starting {service_name}: {e}")
			send_message(message=get_text("error_starting_service", service_name))

	# Mensaje final
	send_message(message=get_text("project_started_success", project_name))

def stop_compose_project(project_name):
	"""
	Para un proyecto Docker Compose completo respetando el orden de dependencias.

	Args:
		project_name: Nombre del proyecto Compose a parar
	"""
	debug(f"Running command: stop_compose_project for project {project_name}")

	# Obtener información del proyecto
	project_info = docker_manager.get_project_info(project_name)
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Obtener contenedores ordenados por dependencias
	containers = project_info.containers
	sorted_containers = docker_manager.compose_manager.sort_containers_by_dependencies(containers)

	# Mensaje inicial
	send_message(message=get_text("stopping_project", project_name))

	# Parar contenedores en orden inverso (los que dependen primero)
	for container in reversed(sorted_containers):
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("stopping_service", service_name))
		try:
			container.stop(timeout=10)
		except Exception as e:
			debug(f"Error stopping {service_name}: {e}")
			send_message(message=get_text("error_stopping_service", service_name))

	# Mensaje final
	send_message(message=get_text("project_stopped_success", project_name))

def delete_compose_project(project_name):
	"""
	Elimina un proyecto Docker Compose completo.

	Args:
		project_name: Nombre del proyecto Compose a eliminar
	"""
	debug(f"Running command: delete_compose_project for project {project_name}")

	# Obtener información del proyecto
	project_info = docker_manager.get_project_info(project_name)
	if not project_info:
		send_message(message=get_text("error_project_not_found", project_name))
		return

	# Obtener contenedores del proyecto
	containers = project_info.containers
	container_count = len(containers)

	# Mensaje inicial
	send_message(message=get_text("deleting_project", project_name, container_count))

	# Eliminar cada contenedor
	for container in containers:
		service_name = container.labels.get('com.docker.compose.service', container.name)
		if EXTENDED_MESSAGES:
			send_message(message=get_text("deleting_service", service_name))
		try:
			container.remove(force=True)
		except Exception as e:
			debug(f"Error deleting {service_name}: {e}")
			send_message(message=get_text("error_deleting_service", service_name))

	# Mensaje final
	send_message(message=get_text("project_deleted_success", project_name, container_count))

def logs(containerId, containerName):
	debug(f"Running command: logs for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	result = docker_manager.show_logs(container_id=containerId, container_name=containerName)
	send_message(message=result, reply_markup=markup)

def log_file(containerId, containerName):
	debug(f"Running command: log_file for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
	result = docker_manager.show_logs_raw(container_id=containerId, container_name=containerName)
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

		with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
			mute_file.write(str(time.time() + minutes * 60))
		debug(f"Bot muted for {minutes} minutes")
		if EXTENDED_MESSAGES:
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

		with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
			mute_file.write('0')
		debug("Bot unmuted")
		if EXTENDED_MESSAGES:
			send_message(message=get_text("unmuted"))

def is_muted():
	with open(FULL_MUTE_FILE_PATH, 'r') as fichero:
		mute_until = float(fichero.readline().strip())
		return time.time() < mute_until

def check_mute():
	global _unmute_timer

	with open(FULL_MUTE_FILE_PATH, 'r+') as fichero:
		mute_until = float(fichero.readline().strip())

		if mute_until != 0:
			if time.time() >= mute_until:
				# Mute time has expired, unmute immediately
				fichero.seek(0)
				fichero.write('0')
				fichero.truncate()
				unmute()
			else:
				# Mute is still active, calculate remaining time and set timer (only if no timer exists)
				if _unmute_timer is None:
					mute_until_seconds = mute_until - time.time()
					_unmute_timer = threading.Timer(mute_until_seconds, unmute)
					_unmute_timer.start()

def compose(containerId, containerName):
	debug(f"Running command: compose for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
	result = docker_manager.get_docker_compose(container_id=containerId, container_name=containerName)
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
	result, possible_update = docker_manager.get_info(container_id=containerId, container_name=containerName)
	delete_message(x.message_id)
	if possible_update:
		markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{containerId}"))
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	send_message(message=result, reply_markup=markup)

def confirm_prune_containers():
	debug("Running command: confirm_prune_containers")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneContainers"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_containers"), reply_markup=markup)

def confirm_prune_images():
	debug("Running command: confirm_prune_images")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneImages"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_images"), reply_markup=markup)

def confirm_prune_networks():
	debug("Running command: confirm_prune_networks")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneNetworks"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_networks"), reply_markup=markup)

def confirm_prune_volumes():
	debug("Running command: confirm_prune_volumes")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneVolumes"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_volumes"), reply_markup=markup)

def confirm_delete(containerId, containerName):
	debug(f"Running command: confirm_delete for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_delete"), callback_data=f"delete|{containerId}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_delete", containerName), reply_markup=markup)

def ask_command(userId, containerId, containerName):
	debug(f"Running command: ask_command for container {containerName}")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelAskCommand"))
	x = send_message(message=get_text("prompt_enter_command", containerName), reply_markup=markup)
	if x:
		save_command_request_state(userId, containerId, containerName, x.message_id)

def confirm_execute_command(containerId, containerName, command):
	debug(f"Running command: confirm_exec for container {containerName} with command [{command}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	commandId = save_command_cache(command)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"exec|{containerId}|{commandId}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data=f"cancelExec|{commandId}"))
	send_message(message=get_text("confirm_exec", containerName, command), reply_markup=markup)

def execute_command(containerId, containerName, command, sendMessage=True):
	debug(f"Running command: exec for container {containerName} with command [{command}]")
	result = docker_manager.execute_command(container_id=containerId, container_name=containerName, command=command)
	if sendMessage:
		max_length = 3500
		if len(result) <= max_length:
			send_message(message=get_text("executed_command", containerName, command, result))
		else:
			first_part = result[:max_length]
			send_message(message=get_text("executed_command", containerName, command, first_part))
			for i in range(max_length, len(result), max_length):
				part = result[i:i + max_length]
				send_message(message=f"<pre><code>{part}</code></pre>")

def confirm_change_tag(containerId, containerName, tag):
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_change_tag", tag), callback_data=f"changeTag|{containerId}|{tag}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_change_tag", containerName, tag), reply_markup=markup)

def update(containerId, containerName):
	x = send_message(message=get_text("updating", containerName))

	# Get container to check if it's part of a Compose project
	client = docker.from_env()
	container = client.containers.get(containerId)
	project_name = ComposeDetector.get_project_name(container)

	# Perform the update
	result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot)
	delete_message(x.message_id)
	send_message(message=result)

	# If container is part of a Compose project, restart the entire project
	if project_name:
		project_info = docker_manager.get_project_info(project_name)
		if project_info and project_info.get_container_count() > 1:
			# Only restart project if it has more than 1 container
			restart_compose_project_after_update(project_name)

def change_tag_container(containerId, containerName):
	try:
		markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
		client = docker.from_env()
		container = client.containers.get(containerId)
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

def confirm_update(containerId, containerName):
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_update"), callback_data=f"update|{containerId}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_update", containerName), reply_markup=markup)

def confirm_update_selected(chatId, messageId):
	_, selected = load_update_data(chatId, messageId)
	containersToUpdate = ""
	for container in selected:
		containersToUpdate += f"· <b>{container}</b>\n"
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_update"), callback_data=f"updateSelected|{messageId}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_update_all", containersToUpdate), reply_markup=markup)

def build_generic_keyboard(container_available, selected_containers, originalMessageId, action_type, button_text, button_text_all=None):
	"""Generic keyboard builder for run/stop/restart actions"""
	markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)
	botones = []
	for container in container_available:
		icono = ICON_CONTAINER_MARKED_FOR_UPDATE if container in selected_containers else ICON_CONTAINER_MARK_FOR_UPDATE
		botones.append(
			InlineKeyboardButton(f"{icono} {container}", callback_data=f"toggle{action_type}|{container}")
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

def build_hierarchical_keyboard(containers, action_type, bot_container_name, filter_standalone_status=None, filter_projects_with_all_status=None):
	"""
	Build hierarchical keyboard with Compose projects and standalone containers.
	Level 1: Shows projects (📦) and standalone containers (🐳)

	Args:
		containers: List of container objects
		action_type: Type of action (Restart, Stop, Run)
		bot_container_name: Name of the bot container to exclude
		filter_standalone_status: Optional list of statuses to filter standalone containers (e.g., ['running', 'restarting'])
		filter_projects_with_all_status: Optional list of statuses - hide projects where ALL containers have these statuses

	Returns:
		InlineKeyboardMarkup: Keyboard with projects and standalone containers
	"""
	markup = InlineKeyboardMarkup(row_width=BUTTON_COLUMNS)

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
			project_info = docker_manager.get_project_info(project_name)
			if project_info:
				all_containers = project_info.containers
				# Check if ALL containers in the project have one of the filtered statuses
				if all_containers and all(c.status in filter_projects_with_all_status for c in all_containers):
					continue  # Skip this project

		# Get container count (filtered by status if applicable)
		project_info = docker_manager.get_project_info(project_name)
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
				callback_data=f"enter{action_type}Project|{project_name}"
			)
		)

	# Add standalone container buttons (sorted) with status indicators
	for container in sorted(standalone_containers, key=lambda x: x.name.lower()):
		# Apply status filter if specified (only for standalone containers)
		if filter_standalone_status and container.status not in filter_standalone_status:
			continue

		# Get status emoji
		status_emoji = get_status_emoji(container.status, container.name, container)

		# Determine callback action based on action_type
		if action_type == "Delete":
			callback_action = "confirmDelete"  # delete requires confirmation
		elif action_type == "Exec":
			callback_action = "askCommand"  # exec requires command input
		elif action_type == "Logs":
			callback_action = "logs"  # show logs
		elif action_type == "CheckUpdate":
			callback_action = "checkUpdate"  # check for updates
		elif action_type == "Info":
			callback_action = "info"  # show info
		elif container.status in ['running', 'restarting']:
			callback_action = action_type.lower()  # restart/stop
		else:
			callback_action = "run"  # start stopped containers

		botones.append(
			InlineKeyboardButton(
				f"🐳 {status_emoji} {container.name}",
				callback_data=f"{callback_action}|{container.id[:CONTAINER_ID_LENGTH]}"
			)
		)

	markup.add(*botones)

	# Add cancel button
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))

	return markup, standalone_containers

def is_admin(userId):
	return str(userId) in str(TELEGRAM_ADMIN).split(',')

def update_available(container):
	image_with_tag = container.attrs['Config']['Image']
	update = False
	if CHECK_UPDATES:
		try:
			image_status = read_container_update_status(image_with_tag, container.name)
			if image_status and "⬆️" in image_status:
				update = True
		except:
			pass
	return update

def display_containers(containers):
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
		has_update = update_available(container)
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

	# Show multi-container projects first
	for project_name in sorted(project_containers.keys()):
		project_conts = project_containers[project_name]
		container_count = len(project_conts)
		result += f"📦 {project_name} ({get_text('compose_project_containers', container_count)})\n"

		# Sort containers within project by name
		sorted_project_conts = sorted(project_conts, key=lambda x: x.name.lower())
		for container in sorted_project_conts:
			# Use cached info
			_, service_name = container_info_cache[container.id]
			result += f"  {get_status_emoji(container.status, container.name, container)} {service_name}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"
		result += "\n"  # Empty line between projects

	# Show standalone containers
	if standalone_containers:
		# Sort standalone containers by name
		sorted_standalone = sorted(standalone_containers, key=lambda x: x.name.lower())
		for container in sorted_standalone:
			result += f"🐳 {get_status_emoji(container.status, container.name, container)} {container.name}"
			if update_cache[container.id]:
				result += " ⬆️"
			result += "\n"

	result += "</pre>"
	return result

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

def get_update_emoji(containerName):
	status = "✅"

	container_id = get_container_id_by_name(container_name=containerName)
	if not container_id:
		return status

	try:
		client = docker.from_env()
		container = client.containers.get(container_id)
		image_with_tag = container.attrs['Config']['Image']
		image_status = read_container_update_status(image_with_tag, container.name)
		if image_status and get_text("NEED_UPDATE_CONTAINER_TEXT") in image_status:
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
	import random
	import socket

	def is_port_available(port):
		"""Check if a port is available by trying to bind to it"""
		# Check TCP
		try:
			with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
				s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
				s.bind(('0.0.0.0', port))
				return True
		except OSError:
			return False

	# Get all ports used by containers
	containers = docker_manager.list_containers()
	used_ports = set()

	for container in containers:
		try:
			# Skip containers with host network
			network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
			if network_mode == 'host':
				continue

			# Get port bindings
			port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
			if port_bindings:
				for container_port, host_bindings in port_bindings.items():
					if host_bindings:
						for host_binding in host_bindings:
							host_port = host_binding.get('HostPort', '')
							if host_port:
								try:
									used_ports.add(int(host_port))
								except ValueError:
									pass
		except Exception as e:
			debug(f"Error getting ports from container {container.name}: {e}")
			continue

	# Try to find an available port (max 100 attempts)
	for _ in range(100):
		port = random.randint(10000, 60000)
		# Check if port is not used by Docker containers AND is available on the system
		if port not in used_ports and is_port_available(port):
			return port

	# If no port found after 100 attempts, return None
	return None

def show_container_ports():
	"""
	Show all ports used by containers
	"""
	containers = docker_manager.list_containers()
	message_lines = []
	host_network_containers = []

	for container in containers:
		try:
			# Check if container uses host network
			network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
			if network_mode == 'host':
				host_network_containers.append(container.name)
				continue

			# Get port bindings
			port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
			if port_bindings:
				ports = []
				for container_port, host_bindings in port_bindings.items():
					if host_bindings:
						for host_binding in host_bindings:
							host_port = host_binding.get('HostPort', '')
							if host_port:
								ports.append(host_port)

				if ports:
					# Get container status
					status = container.status
					emoji = "🟢" if status == "running" else "🔴"
					ports_str = ", ".join(sorted(ports, key=int))
					message_lines.append(f"{emoji} <b>{container.name}</b>: {ports_str}")
		except Exception as e:
			debug(f"Error getting ports from container {container.name}: {e}")
			continue

	# Build final message
	if message_lines:
		message = get_text("ports_list_header") + "\n\n" + "\n".join(message_lines)
	else:
		message = get_text("ports_no_containers")

	# Add note about host network containers
	if host_network_containers:
		message += "\n\n" + get_text("ports_host_network_note")

	# Add inline keyboard with Generate and Close buttons
	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(
		InlineKeyboardButton(get_text("ports_button_generate"), callback_data="generatePort"),
		InlineKeyboardButton(get_text("button_close"), callback_data="cerrar")
	)

	send_message(message=message, reply_markup=markup)

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

def get_container_id_by_name(container_name, debugging=False):
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

def write_cache_item(key, value):
	"""Write cache item with thread-safe lock to prevent corruption."""
	with _cache_lock:
		try:
			pickle.dump(value, open(f'{DIR["cache"]}{key}', 'wb'))
		except Exception as e:
			error(f"Error writing cache item: {key} - {e}")

def read_cache_item(key):
	"""Read cache item with thread-safe lock to prevent corruption."""
	with _cache_lock:
		try:
			return pickle.load(open(f'{DIR["cache"]}{key}', 'rb'))
		except:
			return None

def delete_cache_item(key):
	"""Delete cache item with thread-safe lock to prevent corruption."""
	path = f'{DIR["cache"]}{key}'
	with _cache_lock:
		try:
			if os.path.exists(path):
				os.remove(path)
		except Exception as e:
			pass

def save_container_update_status(image_with_tag, container_name, value):
	key = f'{sanitize_text_for_filename(image_with_tag)}_{sanitize_text_for_filename(container_name)}'
	write_cache_item(key, value)

def read_container_update_status(image_with_tag, container_name):
	key = f'{sanitize_text_for_filename(image_with_tag)}_{sanitize_text_for_filename(container_name)}'
	return read_cache_item(key)

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
	return containers, selected

def clear_update_data(chat_id, message_id):
	delete_cache_item(f"update_data_{chat_id}_{message_id}")

def save_command_cache(command):
	command_id = uuid.uuid4().hex[:8]
	key = f"exec_{command_id}"
	write_cache_item(key, command)
	return command_id

def load_command_cache(command_id):
	key = f"exec_{command_id}"
	return read_cache_item(key)

def clear_command_cache(command_id):
	key = f"exec_{command_id}"
	delete_cache_item(key)

def save_command_request_state(user_id, containerId, containerName, deleteMessage):
	key = f"pending_command_{user_id}"
	value = {"containerId": containerId, "containerName": containerName, "deleteMessage": deleteMessage}
	write_cache_item(key, value)

def load_command_request_state(user_id):
	key = f"pending_command_{user_id}"
	return read_cache_item(key)

def clear_command_request_state(user_id):
	key = f"pending_command_{user_id}"
	delete_cache_item(key)

def save_container_cache(chat_id, message_id, containers):
	"""
	Guarda mapeo de container_id -> container_name para un mensaje con TTL de 24h

	Args:
		chat_id: ID del chat
		message_id: ID del mensaje
		containers: Lista de objetos container
	"""
	from datetime import datetime
	cache_data = {
		"_timestamp": datetime.now().isoformat(),
		"containers": {}
	}
	for container in containers:
		cache_data["containers"][container.id[:CONTAINER_ID_LENGTH]] = container.name

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

	# Check expiry (24 hours)
	if "_timestamp" in cache_data:
		try:
			timestamp = datetime.fromisoformat(cache_data["_timestamp"])
			if datetime.now() - timestamp > timedelta(hours=24):
				clear_container_cache(chat_id, message_id)
				return None
		except:
			pass

	return cache_data.get("containers", {}).get(container_id)

def clear_container_cache(chat_id, message_id):
	"""Limpia la caché de contenedores para un mensaje"""
	delete_cache_item(f"containers_{chat_id}_{message_id}")

def get_container_name_by_id(container_id):
	"""
	Obtiene el nombre de un contenedor por su ID desde Docker API

	Args:
		container_id: ID del contenedor

	Returns:
		str: Nombre del contenedor o None si no existe
	"""
	try:
		container = docker_manager.client.containers.get(container_id)
		return container.name
	except Exception as e:
		debug(f"Container {container_id} not found: {e}")
		return None

def get_container_name(chat_id, message_id, container_id):
	"""
	Obtiene nombre de contenedor con caché + fallback a Docker API

	Args:
		chat_id: ID del chat
		message_id: ID del mensaje
		container_id: ID del contenedor

	Returns:
		str: Nombre del contenedor o None si no existe
	"""
	# 1. Intentar caché
	name = load_container_name(chat_id, message_id, container_id)
	if name:
		return name

	# 2. Fallback a Docker API
	return get_container_name_by_id(container_id)

def short_hash(text, length=30):
	hash_obj = hashlib.sha256(text.encode())
	return hash_obj.hexdigest()[:length]

def generate_docker_compose(container):
	container_attrs = container.attrs['Config']
	container_command = container_attrs.get('Cmd', None)
	container_environment = container_attrs.get('Env', None)
	container_volumes = container.attrs['HostConfig'].get('Binds', None)
	container_network_mode = container.attrs['HostConfig'].get('NetworkMode', None)
	container_ports = container.attrs['HostConfig'].get('PortBindings', None)
	container_restart_policy = container.attrs['HostConfig'].get('RestartPolicy', None)
	container_devices = container.attrs['HostConfig'].get('Devices', None)
	container_labels = container_attrs.get('Labels', None)
	image_with_tag = container_attrs['Image']

	docker_compose = {
		'version': '3',
		'services': {
			container.name: {
				'image': image_with_tag,
				'container_name': container.name
			}
		}
	}

	add_if_present(docker_compose['services'][container.name], 'command', container_command)
	add_if_present(docker_compose['services'][container.name], 'environment', container_environment)
	add_if_present(docker_compose['services'][container.name], 'volumes', container_volumes)
	add_if_present(docker_compose['services'][container.name], 'network_mode', container_network_mode)
	if container_restart_policy:
		add_if_present(docker_compose['services'][container.name], 'restart', str(container_restart_policy.get('Name', '')))
	add_if_present(docker_compose['services'][container.name], 'devices', container_devices)
	add_if_present(docker_compose['services'][container.name], 'labels', container_labels)

	if container_ports:
		docker_compose['services'][container.name]['ports'] = []
		for container_port, host_bindings in container_ports.items():
			for host_binding in host_bindings:
				port_info = f"{host_binding.get('HostPort', '')}:{container_port}"
				docker_compose['services'][container.name]['ports'].append(port_info)

	yaml_data = yaml.safe_dump(docker_compose, default_flow_style=False, sort_keys=False)
	return yaml_data

def add_if_present(dictionary, key, value):
	if value:
		dictionary[key] = value

# ============================================================================
# FUNCIONES INTERNAS DE TELEGRAM (sin cola)
# ============================================================================
def _send_message_direct(chat_id, message, reply_markup, parse_mode, disable_web_page_preview):
	"""Envía un mensaje directamente sin usar la cola"""
	try:
		if message is None:
			message = ""
		if TELEGRAM_THREAD == 1:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
		else:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview, message_thread_id=TELEGRAM_THREAD)
	except Exception as e:
		error(f"Error sending message to chat {chat_id}. Message: [{str(message)}]. Error: [{str(e)}]")
		raise

def _send_document_direct(chat_id, document, reply_markup, caption, parse_mode):
	"""Envía un documento directamente sin usar la cola"""
	try:
		if TELEGRAM_THREAD == 1:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, parse_mode=parse_mode)
		else:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, message_thread_id=TELEGRAM_THREAD, parse_mode=parse_mode)
	except Exception as e:
		error(f"Error sending document to chat {chat_id}. Error: [{e}]")
		raise

def _delete_message_direct(chat_id, message_id):
	"""Elimina un mensaje directamente sin usar la cola"""
	try:
		if chat_id and message_id:
			bot.delete_message(chat_id, message_id)
	except Exception as e:
		# Silently ignore errors when deleting messages (they may have been deleted already)
		pass

def _edit_message_text_direct(chat_id, message_id, text, parse_mode, reply_markup):
	"""Edita el texto de un mensaje directamente sin usar la cola"""
	try:
		return bot.edit_message_text(text, chat_id, message_id, parse_mode=parse_mode, reply_markup=reply_markup)
	except Exception as e:
		debug(f"No se pudo editar mensaje {message_id}: {e}")
		raise

def _edit_message_reply_markup_direct(chat_id, message_id, reply_markup):
	"""Edita el markup de un mensaje directamente sin usar la cola"""
	try:
		return bot.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
	except Exception as e:
		debug(f"No se pudo editar markup del mensaje {message_id}: {e}")
		raise

# ============================================================================
# FUNCIONES PÚBLICAS CON COLA DE MENSAJES
# ============================================================================
def delete_message(message_id, chat_id=None):
	"""Elimina un mensaje usando la cola (asíncrono)"""
	if chat_id is None:
		chat_id = TELEGRAM_GROUP
	message_queue.add_message(_delete_message_direct, chat_id, message_id, wait_for_result=False)

def send_message(chat_id=TELEGRAM_GROUP, message=None, reply_markup=None, parse_mode="html", disable_web_page_preview=True):
	"""Envía un mensaje usando la cola (espera resultado para obtener message_id)"""
	return message_queue.add_message(_send_message_direct, chat_id, message, reply_markup, parse_mode, disable_web_page_preview, wait_for_result=True)

def send_message_to_notification_channel(chat_id=TELEGRAM_NOTIFICATION_CHANNEL, message=None, reply_markup=None, parse_mode="html", disable_web_page_preview=True):
	"""Envía un mensaje al canal de notificaciones usando la cola"""
	if TELEGRAM_NOTIFICATION_CHANNEL is None or TELEGRAM_NOTIFICATION_CHANNEL == '':
		return send_message(chat_id=TELEGRAM_GROUP, message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
	return send_message(chat_id=chat_id, message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)

def send_document(chat_id=TELEGRAM_GROUP, document=None, reply_markup=None, caption=None, parse_mode="html"):
	"""Envía un documento usando la cola (espera resultado para obtener message_id)"""
	return message_queue.add_message(_send_document_direct, chat_id, document, reply_markup, caption, parse_mode, wait_for_result=True)

def edit_message_text(text, chat_id, message_id, parse_mode="html", reply_markup=None):
	"""Edita el texto de un mensaje usando la cola (asíncrono, no bloquea si falla)"""
	message_queue.add_message(_edit_message_text_direct, chat_id, message_id, text, parse_mode, reply_markup, wait_for_result=False)

def edit_message_reply_markup(chat_id, message_id, reply_markup):
	"""Edita el markup de un mensaje usando la cola (asíncrono)"""
	message_queue.add_message(_edit_message_reply_markup_direct, chat_id, message_id, reply_markup, wait_for_result=False)

def delete_updater():
	container_id = get_container_id_by_name(UPDATER_CONTAINER_NAME)
	if container_id:
		client = docker.from_env()
		container = client.containers.get(container_id)
		try:
			updater_image = container.image.id
			container.stop()
			container.remove()
			client.images.remove(updater_image)
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
		return None  # Acción no reconocida

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
		client = docker.from_env()
		info = client.info()
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

	url = f"https://hub.docker.com/v2/repositories/{repo_name}/tags?page_size=99"
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
			return ['latest']

		# Get tags
		tags_url = f'https://ghcr.io/v2/{repo_name}/tags/list'
		tags = requests.get(tags_url, headers={'Authorization': f'Bearer {token}'}, timeout=10).json().get('tags', [])

		if not tags:
			return ['latest']

		# Sort: version tags first (newest), then others
		version_tags = sorted([t for t in tags if t and t[0] == 'v' and any(c.isdigit() for c in t)], reverse=True)
		other_tags = sorted([t for t in tags if t not in version_tags])

		return (version_tags + other_tags)[:20]  # Limit to 20

	except Exception as e:
		error(f"Error getting tags from ghcr.io/{repo_name}: {e}")
		return ['latest']

# Global schedule monitor instance (used by /schedule command)
schedule_monitor = None

if __name__ == '__main__':
	debug(f"Starting bot version {VERSION}")

	eventMonitor = DockerEventMonitor()
	eventMonitor.demonio_event()
	debug("Starting event monitor daemon")
	if CHECK_UPDATES:
		updateMonitor = DockerUpdateMonitor()
		updateMonitor.demonio_update()
		debug("Update daemon started")
	else:
		debug("Update daemon disabled")

	schedule_monitor = DockerScheduleMonitor()
	schedule_monitor.demonio_schedule()
	debug("Schedule daemon started")

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
		telebot.types.BotCommand("/compose", get_text("menu_compose")),
		telebot.types.BotCommand("/prune", get_text("menu_prune")),
		telebot.types.BotCommand("/mute", get_text("menu_mute")),
		telebot.types.BotCommand("/info", get_text("menu_info")),
		telebot.types.BotCommand("/ports", get_text("menu_ports")),
		telebot.types.BotCommand("/version", get_text("menu_version")),
		telebot.types.BotCommand("/donate", get_text("menu_donate")),
		telebot.types.BotCommand("/donors", get_text("menu_donors"))
		])
	delete_updater()
	check_CONTAINER_NAME()
	check_mute()
	starting_message = f"🫡 <b>{CONTAINER_NAME}</b>\n{get_text('active')}"
	if CHECK_UPDATES:
		starting_message += f"\n✅ {get_text('check_for_updates')}"
	else:
		starting_message += f"\n❌ {get_text('check_for_updates')}"
	starting_message += f"\n<i>⚙️ v{VERSION}</i>"
	starting_message += f"\n{get_text('channel')}"
	send_message(message=starting_message)
	bot.infinity_polling(timeout=60)
