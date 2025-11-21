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
from datetime import datetime
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from docker_update import extract_container_config, perform_update

VERSION = "3.10.1"

_unmute_timer = None

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
def load_locale(locale):
	with open(f"/app/locale/{locale}.json", "r", encoding="utf-8") as file:
		return json.load(file)

def get_text(key, *args):
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

	for i, arg in enumerate(args, start=1):
		placeholder = f"${i}"
		translated_text = translated_text.replace(placeholder, str(arg))

	return translated_text


# Comprobación inicial de variables
if TELEGRAM_TOKEN is None or TELEGRAM_TOKEN == '':
	error(get_text("error_bot_token"))
	sys.exit(1)
if TELEGRAM_ADMIN is None or TELEGRAM_ADMIN == '':
	error(get_text("error_bot_telegram_admin"))
	sys.exit(1)
if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
	error(get_text("error_bot_telegram_admin_anonymous"))
	sys.exit(1)
if CONTAINER_NAME is None or CONTAINER_NAME == '':
	error(get_text("error_bot_container_name"))
	sys.exit(1)
if TELEGRAM_GROUP is None or TELEGRAM_GROUP == '':
	if len(str(TELEGRAM_ADMIN).split(',')) > 1:
		error(get_text("error_multiple_admin_only_with_group"))
		sys.exit(1)
	TELEGRAM_GROUP = TELEGRAM_ADMIN

try:
	TELEGRAM_THREAD = int(TELEGRAM_THREAD)
except:
	error(get_text("error_bot_telegram_thread", TELEGRAM_THREAD))
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
		debug(get_text("debug_message_queue_started"))

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
				error(get_text("debug_message_queue_error", str(e)))

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
							warning(get_text("debug_message_queue_rate_limit", wait_time))
							time.sleep(wait_time)
							continue
					elif attempt < self.max_retries - 1:
						wait_time = 1 * (attempt + 1)
						debug(get_text("debug_message_queue_retry", attempt + 1, self.max_retries, wait_time))
						time.sleep(wait_time)
						continue

					error(get_text("debug_message_queue_final_error", self.max_retries, str(e)))
					if result_queue:
						result_queue.put(None)
					break
		except Exception as e:
			error(get_text("debug_message_queue_error", str(e)))
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
				error(get_text("debug_message_queue_error", "Timeout esperando resultado del mensaje"))
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
			error(get_text("error_stopping_container_with_error", container_name, e))
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
			error(get_text("error_restarting_container_with_error", container_name, e))
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
			error(get_text("error_starting_container_with_error", container_name, e))
			return get_text("error_starting_container", container_name)

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			logs = container.logs().decode("utf-8")
			return get_text("showing_logs", container_name, logs[-3500:])
		except Exception as e:
			error(get_text("error_showing_logs_container_with_error", container_name, e))
			return get_text("error_showing_logs_container", container_name)

	def show_logs_raw(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return container.logs().decode("utf-8")
		except Exception as e:
			error(get_text("error_showing_logs_container_with_error", container_name, e))
			return get_text("error_showing_logs_container", container_name)

	def get_docker_compose(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return generate_docker_compose(container)
		except Exception as e:
			error(get_text("error_showing_compose_container_with_error", container_name, e))
			return get_text("error_showing_compose_container", container_name)

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
					error(get_text("error_stats_not_available", container_name, e))

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
					debug(get_text("debug_update_not_cached", container_name, e))

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
			text += f'- {get_text("container_id")}: {container_id}\n\n'
			text += f'- {get_text("used_image")}:\n{image_with_tag}\n\n'
			text += f'- {get_text("image_id")}: {container.image.id.replace("sha256:", "")[:CONTAINER_ID_LENGTH]}'
			if CHECK_UPDATES:
				text += f"\n\n{image_status}"
			text += "</code></pre>"
			return f'📜 {get_text("information")} <b>{container_name}</b>:\n{text}', possible_update
		except Exception as e:
			error(get_text("error_showing_info_container_with_error", container_name, e))
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
			error(get_text("error_updating_container_with_error", container_name, e))
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
					error(get_text("error_pulling_image", image_with_tag))
					image_status = ""
					save_container_update_status(image_with_tag, container.name, image_status)
					return
			except docker.errors.ImageNotFound:
				error(get_text("error_image_not_found", image_with_tag))
				image_status = ""
				save_container_update_status(image_with_tag, container.name, image_status)
				return
			except docker.errors.APIError as e:
				error(get_text("error_pulling_image_with_error", image_with_tag, e))
				image_status = ""
				save_container_update_status(image_with_tag, container.name, image_status)
				return

			local_image_normalized = local_image.replace('sha256:', '')
			remote_image_normalized = remote_image.id.replace('sha256:', '')

			debug(get_text("debug_checking_update", container.name, image_with_tag, local_image_normalized[:CONTAINER_ID_LENGTH], remote_image_normalized[:CONTAINER_ID_LENGTH]))

			if local_image_normalized != remote_image_normalized:
				debug(get_text("debug_update_detected", container.name, remote_image_normalized[:CONTAINER_ID_LENGTH]))
				try:
					self.client.images.remove(remote_image.id)
				except:
					pass # Si no se puede borrar es porque esta siendo usada por otro contenedor
				markup = InlineKeyboardMarkup(row_width = 1)
				markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}"))
				image_status = get_text("NEED_UPDATE_CONTAINER_TEXT")
				send_message(message=get_text("available_update", container.name), reply_markup=markup)
			else:
				image_status = get_text("UPDATED_CONTAINER_TEXT")
				send_message(message=get_text("already_updated", container.name))
		except Exception as e:
			error(get_text("error_checking_update_with_error", e))
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
				debug(get_text("debug_stopping_container", container_name))
				container.stop()
			container.remove()
			return get_text("deleted_container", container_name)
		except Exception as e:
			error(get_text("error_deleting_container_with_error", container_name, e))
			return get_text("error_deleting_container", container_name)
		
	def prune_containers(self):
		try:
			pruned_containers = self.client.containers.prune()
			if pruned_containers:
				file_size_bytes = sizeof_fmt(pruned_containers['SpaceReclaimed'])
			debug(get_text("debug_deleted", str(pruned_containers), str(file_size_bytes)))
			return get_text("prune_containers", str(file_size_bytes)), str(pruned_containers)
		except Exception as e:
			error(get_text("error_prune_containers_with_error", e))
			return get_text("error_prune_containers")
		
	def prune_images(self):
		try:
			pruned_images = self.client.images.prune(filters={'dangling': False})
			if pruned_images:
				file_size_bytes = sizeof_fmt(pruned_images['SpaceReclaimed'])
			debug(get_text("debug_deleted",  str(pruned_images), str(file_size_bytes)))
			return get_text("prune_images", str(file_size_bytes)), str(pruned_images)
		except Exception as e:
			error(get_text("error_prune_images_with_error", e))
			return get_text("error_prune_images")
		
	def prune_networks(self):
		try:
			pruned_networks = self.client.networks.prune()
			debug(get_text("debug_deleted", str(pruned_networks)))
			return get_text("prune_networks"), str(pruned_networks)
		except Exception as e:
			error(get_text("error_prune_networks_with_error", e))
			return get_text("error_prune_networks")		
		

	def prune_volumes(self):
		try:
			pruned_volumes = self.client.volumes.prune()
			if pruned_volumes:
				file_size_bytes = sizeof_fmt(pruned_volumes['SpaceReclaimed'])
			debug(get_text("debug_deleted",  str(pruned_volumes), str(file_size_bytes)))
			return get_text("prune_volumes", str(file_size_bytes)), str(pruned_volumes)
		except Exception as e:
			error(get_text("error_prune_volumes_with_error", e))
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
			error(get_text("error_executing_command_container_with_error", command, container_name, e))
			return get_text("error_executing_command_container", command, container_name)

# Instanciamos el DockerManager
docker_manager = DockerManager()

class DockerEventMonitor:
	def __init__(self):
		self.client = docker.from_env()

	def detectar_eventos_contenedores(self):
		for event in self.client.events(decode=True):
			if 'status' in event and 'Actor' in event and 'Attributes' in event['Actor']:
				container_name = event['Actor']['Attributes'].get('name', '')
				status = event['status']

				message = None
				if status == "start":
					message = get_text("started_container", container_name)
				elif status == "die":
					message = get_text("stopped_container", container_name)
				elif status == "create" and EXTENDED_MESSAGES:
					message = get_text("created_container", container_name)
				
				if message:
					try:
						if is_muted():
							debug(get_text("debug_muted_message", message))
							continue

						send_message_to_notification_channel(message=message)
					except Exception as e:
						error(get_text("error_sending_updates", message, e))
						time.sleep(20) # Posible saturación de Telegram y el send_message lanza excepción

	def demonio_event(self):
		try:
			thread = threading.Thread(target=self.detectar_eventos_contenedores, daemon=True)
			thread.start()
		except Exception as e:
			error(get_text("error_monitor_daemon", e))
			self.demonio_event()


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
					debug(get_text("debug_ignore_check_for_update", container.name))
					continue

				labels = container.labels
				if LABEL_IGNORE_CHECK_UPDATES in labels:
					debug(get_text("debug_ignore_check_for_update", container.name))
					continue

				container_attrs = container.attrs['Config']
				image_with_tag = container_attrs['Image']
				try:
					local_image = container.image.id
					remote_image = self.client.images.pull(image_with_tag)
					debug(get_text("debug_checking_update", container.name, image_with_tag, local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH], remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
					if local_image != remote_image.id:
						if LABEL_AUTO_UPDATE in labels:
							if EXTENDED_MESSAGES and not is_muted():
								send_message_to_notification_channel(message=get_text("auto_update", container.name))
							debug(get_text("debug_auto_update", container.name))
							x = None
							if not is_muted():
								x = send_message_to_notification_channel(message=get_text("updating", container.name))
							result = docker_manager.update(container_id=container.id, container_name=container.name, message=x, bot=bot)
							if not is_muted():
								delete_message(x.message_id)
								send_message_to_notification_channel(message=result)
							else:
								debug(get_text("debug_muted_message", result))
							continue
						old_image_status = read_container_update_status(image_with_tag, container.name)
						image_status = get_text("NEED_UPDATE_CONTAINER_TEXT")
						debug(get_text("debug_update_detected", container.name, remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
						try:
							self.client.images.remove(remote_image.id)
						except:
							pass # Si no se puede borrar es porque esta siendo usada por otro contenedor

						if container.name != CONTAINER_NAME:
							grouped_updates_containers.append(container.name)
						
						if image_status == old_image_status:
							debug(get_text("debug_update_already_notified"))
							continue

						if container.name == CONTAINER_NAME:
							markup = InlineKeyboardMarkup(row_width = 1)
							markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}"))
							if not is_muted():
								send_message(message=get_text("available_update", container.name), reply_markup=markup)
							else:
								debug(get_text("debug_muted_message", get_text("available_update", container.name)))
							continue

						should_notify = True
					else: # Contenedor actualizado
						image_status = get_text("UPDATED_CONTAINER_TEXT")
				except Exception as e:
					error(get_text("error_checking_update_with_error", e))
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
					debug(get_text("debug_muted_message", get_text("available_updates", len(grouped_updates_containers))))
			debug(get_text("debug_waiting_next_check_updates", CHECK_UPDATE_EVERY_HOURS))
			time.sleep(CHECK_UPDATE_EVERY_HOURS * 3600)

	def demonio_update(self):
		try:
			thread = threading.Thread(target=self.detectar_actualizaciones, daemon=True)
			thread.start()
		except Exception as e:
			error(get_text("error_update_daemon", e))
			self.demonio_update()

class DockerScheduleMonitor:
	def __init__(self):
		super().__init__()
		self.cron_file = SCHEDULE_PATH + "/" + SCHEDULE_FILE
		self.last_run = {}
		self._reboot_tasks_executed = set()  # Track which @reboot tasks have been executed
		self._file_lock = threading.Lock()  # Lock for file synchronization
		self._ensure_cron_file_exists()
		self._execute_reboot_tasks()  # Execute @reboot tasks on startup

	def _ensure_cron_file_exists(self):
		if not os.path.exists(self.cron_file):
			open(self.cron_file, "w").close()  # Create an empty file

	def _execute_reboot_tasks(self):
		"""Execute all @reboot tasks immediately on bot startup"""
		try:
			with self._file_lock:
				with open(self.cron_file, "r") as file:
					lines = file.readlines()

			for line in lines:
				data = parse_cron_line(line)
				if data is None:
					continue

				schedule = data.get("schedule")

				# Only execute @reboot tasks
				if schedule == "@reboot":
					success = self._execute_action(data, line)
					if success:
						# Mark this task as executed (using line hash to identify it)
						self._reboot_tasks_executed.add(short_hash(line))
		except Exception as e:
			error(get_text("error_reading_schedule_file", e))

	def _execute_action(self, data, line=None):
		"""
		Execute a schedule action.

		Args:
			data: Parsed schedule data dict
			line: Original line from schedule file (for error reporting/deletion)

		Returns:
			True if successful, False if failed
		"""
		try:
			action = data.get("action")
			container = data.get("container")
			minutes = data.get("minutes")
			command = data.get("command")
			show_output = bool(data.get("show_output", "1"))

			if action == "run":
				containerId = get_container_id_by_name(container)
				if not containerId:
					error(get_text("error_schedule_container_not_found", container, action))
					if line:
						delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
						send_message(message=get_text("error_schedule_removed_line", line.strip()))
					return False
				run(containerId, container, from_schedule=True)

			elif action == "stop":
				containerId = get_container_id_by_name(container)
				if not containerId:
					error(get_text("error_schedule_container_not_found", container, action))
					if line:
						delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
						send_message(message=get_text("error_schedule_removed_line", line.strip()))
					return False
				stop(containerId, container, from_schedule=True)

			elif action == "restart":
				containerId = get_container_id_by_name(container)
				if not containerId:
					error(get_text("error_schedule_container_not_found", container, action))
					if line:
						delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
						send_message(message=get_text("error_schedule_removed_line", line.strip()))
					return False
				restart(containerId, container, from_schedule=True)

			elif action == "mute":
				try:
					minutes = int(minutes)
					if minutes <= 0:
						error(get_text("error_schedule_invalid_minutes", minutes))
						if line:
							delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
							send_message(message=get_text("error_schedule_removed_line", line.strip()))
						return False
					mute(minutes)
				except (ValueError, TypeError) as e:
					error(get_text("error_schedule_invalid_minutes", minutes))
					if line:
						delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
						send_message(message=get_text("error_schedule_removed_line", line.strip()))
					return False

			elif action == "exec":
				containerId = get_container_id_by_name(container)
				if not containerId:
					error(get_text("error_schedule_container_not_found", container, action))
					if line:
						delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
						send_message(message=get_text("error_schedule_removed_line", line.strip()))
					return False
				execute_command(containerId, container, command, show_output)

			return True

		except Exception as e:
			error(get_text("error_schedule_execution", action, str(e)))
			if line:
				delete_line_from_file(self.cron_file, short_hash(line), self._file_lock)
				send_message(message=get_text("error_schedule_removed_line", line.strip()))
			return False

	def run(self):
		"""Main loop: check and execute scheduled tasks every minute"""
		while True:
			try:
				with self._file_lock:
					with open(self.cron_file, "r") as file:
						lines = file.readlines()

				now = datetime.now()
				for line in lines:
					data = parse_cron_line(line)
					if data is None:  # Skip invalid lines
						continue

					schedule = data.get("schedule")

					# Skip @reboot tasks in the main loop (they're executed at startup)
					if schedule == "@reboot":
						continue

					# Check if this task should run now
					if self.should_run(schedule, now):
						self._execute_action(data, line)
			except Exception as e:
				error(get_text("error_reading_schedule_file", e))
			time.sleep(60)

	def should_run(self, schedule, now):
		"""
		Check if a cron expression should run at the given time.

		Note: @reboot tasks are handled separately in _execute_reboot_tasks()
		and should not reach this method.
		"""
		try:
			cron = croniter(schedule, now)
			last_execution = cron.get_prev(datetime)
			should_run = last_execution.year == now.year and \
						last_execution.month == now.month and \
						last_execution.day == now.day and \
						last_execution.hour == now.hour and \
						last_execution.minute == now.minute
			return should_run
		except Exception:
			return False

	def demonio_schedule(self):
		try:
			thread = threading.Thread(target=self.run, daemon=True)
			thread.start()
		except Exception as e:
			error(get_text("error_schedule_daemon", e))
			self.demonio_schedule()

@bot.message_handler(commands=["start", "list", "run", "stop", "restart", "delete", "exec", "checkupdate", "updateall", "changetag", "logs", "logfile", "compose", "mute", "schedule", "info", "version", "donate", "donors", "prune"])
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

	debug(f"COMMAND: {comando}")
	debug(f"USER: {userId}")
	debug(f"CHAT/GROUP: {message.chat.id}")
	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1
	debug(f"THREAD ID: {message_thread_id}")

	if message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if not is_admin(userId):
		warning(get_text("warning_not_admin", userId, message.from_user.username))
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
			containers = docker_manager.list_containers(comando=comando)
			container_names = [container.name for container in containers if CONTAINER_NAME != container.name]
			if not container_names:
				send_message(message=get_text("no_containers_to_start"))
				return

			markup = build_generic_keyboard(container_names, set(), 0, "Run", get_text("button_run"), get_text("button_run_all"))
			message = send_message(message=get_text("start_a_container"), reply_markup=markup)
			if message:
				save_action_data(TELEGRAM_GROUP, message.message_id, "run", container_names)
	elif comando in ('/stop', f'/stop@{bot.get_me().username}'):
		if container_id:
			stop(container_id, container_name)
		else:
			containers = docker_manager.list_containers(comando=comando)
			container_names = [container.name for container in containers if CONTAINER_NAME != container.name]
			if not container_names:
				send_message(message=get_text("no_containers_to_stop"))
				return

			markup = build_generic_keyboard(container_names, set(), 0, "Stop", get_text("button_stop"), get_text("button_stop_all"))
			message = send_message(message=get_text("stop_a_container"), reply_markup=markup)
			if message:
				save_action_data(TELEGRAM_GROUP, message.message_id, "stop", container_names)
	elif comando in ('/restart', f'/restart@{bot.get_me().username}'):
		if container_id:
			restart(container_id, container_name)
		else:
			containers = docker_manager.list_containers(comando=comando)
			container_names = [container.name for container in containers if CONTAINER_NAME != container.name]
			if not container_names:
				send_message(message=get_text("no_containers_to_restart"))
				return

			markup = build_generic_keyboard(container_names, set(), 0, "Restart", get_text("button_restart"), get_text("button_restart_all"))
			message = send_message(message=get_text("restart_a_container"), reply_markup=markup)
			if message:
				save_action_data(TELEGRAM_GROUP, message.message_id, "restart", container_names)
	elif comando in ('/logs', f'/logs@{bot.get_me().username}'):
		if container_id:
			logs(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'logs|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("show_logs"), reply_markup=markup)
	elif comando in ('/logfile', f'/logfile@{bot.get_me().username}'):
		if container_id:
			log_file(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'logfile|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("show_logsfile"), reply_markup=markup)
	elif comando in ('/compose', f'/compose@{bot.get_me().username}'):
		if container_id:
			compose(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'compose|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("show_compose"), reply_markup=markup)
	elif comando in ('/mute', f'/mute@{bot.get_me().username}'):
		try:
			minutes = int(message.text.split()[1])
		except (IndexError, ValueError):
			send_message(message=get_text("error_use_mute_command"))
			return
		mute(minutes)
	elif comando in ('/schedule', f'/schedule@{bot.get_me().username}'):
		full_schedule = message.text.replace(comando, '').replace('  ', ' ').lstrip()
		if not full_schedule or full_schedule == "": # CHECK AND DELETE
			markup = InlineKeyboardMarkup(row_width = 1)
			empty = False
			botones = []
			try:
				# Use lock for file synchronization
				if schedule_monitor:
					with schedule_monitor._file_lock:
						with open(FULL_SCHEDULE_PATH, "r") as file:
							lines = file.readlines()
				else:
					with open(FULL_SCHEDULE_PATH, "r") as file:
						lines = file.readlines()

				if len(lines) == 0:
					empty = True
				else:
					for line in lines:
						botones.append(InlineKeyboardButton(line, callback_data=f'deleteSchedule|{short_hash(line)}'))
			except Exception as e:
				error(get_text("error_reading_schedule_file", e))

			if empty:
				send_message(message=get_text("empty_schedule"))
			else:
				markup.add(*botones)
				markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
				send_message(message=get_text("delete_schedule"), reply_markup=markup)
		else: # SAVE
			# Validate that the schedule line is not empty
			if not full_schedule or not full_schedule.strip():
				send_message(message=get_text("error_adding_schedule", message.text))
				return

			data = parse_cron_line(full_schedule)
			if not data:
				send_message(message=get_text("error_adding_schedule", message.text))
				return

			action = data.get("action")
			container = data.get("container")

			# Validate container exists (except for 'mute' action which doesn't need a container)
			if action != 'mute' and not get_container_id_by_name(container):
				send_message(message=get_text("container_does_not_exist", container))
				return

			# Save to schedule file with lock for synchronization
			if schedule_monitor:
				with schedule_monitor._file_lock:
					with open(FULL_SCHEDULE_PATH, "a") as file:
						file.write(f'{full_schedule}\n')
			else:
				with open(FULL_SCHEDULE_PATH, "a") as file:
					file.write(f'{full_schedule}\n')
			send_message(message=get_text("schedule_saved", full_schedule))
	elif comando in ('/info', f'/info@{bot.get_me().username}'):
		if container_id:
			info(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'info|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("show_info"), reply_markup=markup)
	elif comando in ('/exec', f'/exec@{bot.get_me().username}'):
		if container_id:
			ask_command(userId, container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'askCommand|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("exec_command_container"), reply_markup=markup)
	elif comando in ('/delete', f'/delete@{bot.get_me().username}'):
		if container_id:
			confirm_delete(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'confirmDelete|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("delete_container"), reply_markup=markup)
	elif comando in ('/checkupdate', f'/checkupdate@{bot.get_me().username}'):
		if container_id:
			docker_manager.force_check_update(container_id)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_update_emoji(container.name)} {container.name}', callback_data=f'checkUpdate|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("update_container"), reply_markup=markup)
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
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name, container)} {container.name}', callback_data=f'changeTagContainer|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("change_tag_container"), reply_markup=markup)
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
			warning(get_text("warning_not_admin", userId, call.from_user.username))
			send_message(chat_id=userId, message=get_text("user_not_admin"))
			return

		data = parse_call_data(call.data)
		comando = data["comando"]
		containerId = data.get("containerId")
		containerName = data.get("containerName")
		tag = data.get("tag")
		action = data.get("action")
		originalMessageId = data.get("originalMessageId")
		commandId = data.get("commandId")
		scheduleHash = data.get("scheduleHash")
	except Exception as e:
		error(get_text("error_callback_initialization", str(e)))
		try:
			bot.answer_callback_query(call.id, text=get_text("error_callback_processing"), show_alert=True)
		except:
			pass
		return

	try:
		if comando not in ["toggleUpdate", "toggleUpdateAll", "toggleRun", "toggleRunAll", "toggleStop", "toggleStopAll", "toggleRestart", "toggleRestartAll"]:
			delete_message(messageId)

		if call.data == "cerrar":
			# Clean up any cached data for this message
			update_data = read_cache_item(f"update_data_{chatId}_{messageId}")
			if update_data is not None:
				clear_update_data(chatId, messageId)
			for action in ["run", "stop", "restart"]:
				action_data = read_cache_item(f"{action}_data_{chatId}_{messageId}")
				if action_data is not None:
					clear_action_data(chatId, messageId, action)
					break
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
			confirm_change_tag(containerId, containerName, tag)

		# CHANGE_TAG
		elif comando == "changeTag":
			x = send_message(message=get_text("updating", containerName))
			result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot, tag=tag)
			delete_message(x.message_id)
			send_message(message=result)

		# DELETE SCHEDULE
		elif comando == "deleteSchedule":
			if schedule_monitor:
				deleted = delete_line_from_file(FULL_SCHEDULE_PATH, scheduleHash, schedule_monitor._file_lock)
			else:
				deleted = delete_line_from_file(FULL_SCHEDULE_PATH, scheduleHash)
			send_message(message=get_text("deleted_schedule", deleted))

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
					debug(get_text("debug_container_not_found", containerName))
					continue
				client = docker.from_env()
				container = client.containers.get(container_id)
				if update_available(container):
					update(container.id, container.name)
			clear_update_data(chatId, originalMessageId)

		# TOGGLE RUN
		elif comando == "toggleRun":
			containers, selected = load_action_data(chatId, messageId, "run")
			if containerName in selected:
				selected.remove(containerName)
			else:
				selected.add(containerName)
			save_action_data(chatId, messageId, "run", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Run", get_text("button_run"), get_text("button_run_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# TOGGLE RUN ALL
		elif comando == "toggleRunAll":
			containers, selected = load_action_data(chatId, messageId, "run")
			for container in containers:
				if container not in selected:
					selected.add(container)
			save_action_data(chatId, messageId, "run", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Run", get_text("button_run"), get_text("button_run_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# CONFIRM RUN SELECTED
		elif comando == "confirmRunSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "run")
			containersToRun = ""
			for container in selected:
				containersToRun += f"· <b>{container}</b>\n"
			markup = InlineKeyboardMarkup(row_width = 1)
			markup.add(InlineKeyboardButton(get_text("button_run"), callback_data=f"runSelected|{originalMessageId}"))
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			send_message(message=get_text("confirm_run_selected", containersToRun), reply_markup=markup)

		# RUN SELECTED
		elif comando == "runSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "run")
			for containerName in selected:
				container_id = get_container_id_by_name(container_name=containerName)
				if not container_id:
					send_message(message=get_text("container_does_not_exist", containerName))
					debug(get_text("debug_container_not_found", containerName))
					continue
				run(container_id, containerName)
			clear_action_data(chatId, originalMessageId, "run")

		# TOGGLE STOP
		elif comando == "toggleStop":
			containers, selected = load_action_data(chatId, messageId, "stop")
			if containerName in selected:
				selected.remove(containerName)
			else:
				selected.add(containerName)
			save_action_data(chatId, messageId, "stop", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Stop", get_text("button_stop"), get_text("button_stop_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# TOGGLE STOP ALL
		elif comando == "toggleStopAll":
			containers, selected = load_action_data(chatId, messageId, "stop")
			for container in containers:
				if container not in selected:
					selected.add(container)
			save_action_data(chatId, messageId, "stop", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Stop", get_text("button_stop"), get_text("button_stop_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# CONFIRM STOP SELECTED
		elif comando == "confirmStopSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "stop")
			containersToStop = ""
			for container in selected:
				containersToStop += f"· <b>{container}</b>\n"
			markup = InlineKeyboardMarkup(row_width = 1)
			markup.add(InlineKeyboardButton(get_text("button_stop"), callback_data=f"stopSelected|{originalMessageId}"))
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			send_message(message=get_text("confirm_stop_selected", containersToStop), reply_markup=markup)

		# STOP SELECTED
		elif comando == "stopSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "stop")
			for containerName in selected:
				container_id = get_container_id_by_name(container_name=containerName)
				if not container_id:
					send_message(message=get_text("container_does_not_exist", containerName))
					debug(get_text("debug_container_not_found", containerName))
					continue
				stop(container_id, containerName)
			clear_action_data(chatId, originalMessageId, "stop")

		# TOGGLE RESTART
		elif comando == "toggleRestart":
			containers, selected = load_action_data(chatId, messageId, "restart")
			if containerName in selected:
				selected.remove(containerName)
			else:
				selected.add(containerName)
			save_action_data(chatId, messageId, "restart", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Restart", get_text("button_restart"), get_text("button_restart_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# TOGGLE RESTART ALL
		elif comando == "toggleRestartAll":
			containers, selected = load_action_data(chatId, messageId, "restart")
			for container in containers:
				if container not in selected:
					selected.add(container)
			save_action_data(chatId, messageId, "restart", containers, selected)

			markup = build_generic_keyboard(containers, selected, messageId, "Restart", get_text("button_restart"), get_text("button_restart_all"))
			edit_message_reply_markup(chatId, messageId, reply_markup=markup)

		# CONFIRM RESTART SELECTED
		elif comando == "confirmRestartSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "restart")
			containersToRestart = ""
			for container in selected:
				containersToRestart += f"· <b>{container}</b>\n"
			markup = InlineKeyboardMarkup(row_width = 1)
			markup.add(InlineKeyboardButton(get_text("button_restart"), callback_data=f"restartSelected|{originalMessageId}"))
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			send_message(message=get_text("confirm_restart_selected", containersToRestart), reply_markup=markup)

		# RESTART SELECTED
		elif comando == "restartSelected":
			containers, selected = load_action_data(chatId, originalMessageId, "restart")
			for containerName in selected:
				container_id = get_container_id_by_name(container_name=containerName)
				if not container_id:
					send_message(message=get_text("container_does_not_exist", containerName))
					debug(get_text("debug_container_not_found", containerName))
					continue
				restart(container_id, containerName)
			clear_action_data(chatId, originalMessageId, "restart")

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
	except Exception as e:
		error(get_text("error_callback_execution", comando, str(e)))
		try:
			send_message(message=get_text("error_callback_processing"))
		except:
			pass

@bot.message_handler(func=lambda message: True)
def handle_text(message):
	userId = message.from_user.id
	username = message.from_user.username
	pending = load_command_request_state(userId)
	debug(f"USER: {userId}")
	debug(f"CHAT/GROUP: {message.chat.id}")
	message_thread_id = message.message_thread_id
	if not message_thread_id:
		message_thread_id = 1
	debug(f"THREAD ID: {message_thread_id}")

	if message_thread_id != TELEGRAM_THREAD and (not message.reply_to_message or message.reply_to_message.from_user.id != bot.get_me().id):
		return

	if pending:
		if not is_admin(userId):
			warning(get_text("warning_not_admin", userId, username))
			send_message(get_text("user_not_admin"), chat_id=userId)
			return
		command_text = message.text.strip()
		containerId = pending.get("containerId")
		containerName = pending.get("containerName")
		deleteMessage = pending.get("deleteMessage")
		delete_message(deleteMessage)
		delete_message(message.message_id)
		clear_command_request_state(userId)
		confirm_execute_command(containerId, containerName, command_text)
	else:
		pass

def run(containerId, containerName, from_schedule=False):
	debug(get_text("run_command_for_container", "run", containerName))
	x = send_message(message=get_text("starting", containerName))
	result = docker_manager.start_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def stop(containerId, containerName, from_schedule=False):
	debug(get_text("run_command_for_container", "stop", containerName))
	x = send_message(message=get_text("stopping", containerName))
	result = docker_manager.stop_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def restart(containerId, containerName, from_schedule=False):
	debug(get_text("run_command_for_container", "restart", containerName))
	x = send_message(message=get_text("restarting", containerName))
	result = docker_manager.restart_container(container_id=containerId, container_name=containerName, from_schedule=from_schedule)
	if x:
		delete_message(x.message_id)
	if result:
		send_message(message=result)

def logs(containerId, containerName):
	debug(get_text("run_command_for_container", "logs", containerName))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	result = docker_manager.show_logs(container_id=containerId, container_name=containerName)
	send_message(message=result, reply_markup=markup)

def log_file(containerId, containerName):
	debug(get_text("run_command_for_container", "log_file", containerName))
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
	global _unmute_timer

	if minutes == 0:
		unmute()
		return

	# Cancel any existing unmute timer
	if _unmute_timer is not None:
		_unmute_timer.cancel()
		_unmute_timer = None

	with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
		mute_file.write(str(time.time() + minutes * 60))
	debug(get_text("muted", minutes))
	if EXTENDED_MESSAGES:
		if minutes == 1:
			send_message(message=get_text("muted_singular"))
		else:
			send_message(message=get_text("muted", minutes))
	_unmute_timer = threading.Timer(minutes * 60, unmute)
	_unmute_timer.start()

def unmute():
	global _unmute_timer

	# Cancel any existing unmute timer
	if _unmute_timer is not None:
		_unmute_timer.cancel()
		_unmute_timer = None

	with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
		mute_file.write('0')
	debug(get_text("unmuted"))
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
	debug(get_text("run_command_for_container", "compose", containerName))
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
	debug(get_text("run_command_for_container", "info", containerName))
	markup = InlineKeyboardMarkup(row_width = 1)
	x = send_message(message=get_text("obtaining_info", containerName))
	result, possible_update = docker_manager.get_info(container_id=containerId, container_name=containerName)
	delete_message(x.message_id)
	if possible_update:
		markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{containerId}|{containerName}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=result, reply_markup=markup)

def confirm_prune_containers():
	debug(get_text("run_command", "confirm_prune_containers"))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneContainers"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_containers"), reply_markup=markup)

def confirm_prune_images():
	debug(get_text("run_command", "confirm_prune_images"))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneImages"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_images"), reply_markup=markup)

def confirm_prune_networks():
	debug(get_text("run_command", "confirm_prune_networks"))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneNetworks"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_networks"), reply_markup=markup)

def confirm_prune_volumes():
	debug(get_text("run_command", "confirm_prune_volumes"))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"prune|pruneVolumes"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_prune_volumes"), reply_markup=markup)

def confirm_delete(containerId, containerName):
	debug(get_text("run_command_for_container", "confirm_delete", containerName))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_delete"), callback_data=f"delete|{containerId}|{containerName}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_delete", containerName), reply_markup=markup)

def ask_command(userId, containerId, containerName):
	debug(get_text("run_command_for_container", "ask_command", containerName))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cancelAskCommand"))
	x = send_message(message=get_text("prompt_enter_command", containerName), reply_markup=markup)
	if x:
		save_command_request_state(userId, containerId, containerName, x.message_id)

def confirm_execute_command(containerId, containerName, command):
	debug(get_text("run_command_for_container_command", "confirm_exec", containerName, command))
	markup = InlineKeyboardMarkup(row_width = 1)
	commandId = save_command_cache(command)
	markup.add(InlineKeyboardButton(get_text("button_confirm"), callback_data=f"exec|{containerId}|{containerName}|{commandId}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data=f"cancelExec|{commandId}"))
	send_message(message=get_text("confirm_exec", containerName, command), reply_markup=markup)

def execute_command(containerId, containerName, command, sendMessage=True):
	debug(get_text("run_command_for_container_command", "exec", containerName, command))
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
	markup.add(InlineKeyboardButton(get_text("button_confirm_change_tag", tag), callback_data=f"changeTag|{containerId}|{containerName}|{tag}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_change_tag", containerName, tag), reply_markup=markup)

def update(containerId, containerName):
	x = send_message(message=get_text("updating", containerName))
	result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot)
	delete_message(x.message_id)
	send_message(message=result)

def change_tag_container(containerId, containerName):
	try:
		markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
		client = docker.from_env()
		container = client.containers.get(containerId)
		repo = container.attrs['Config']['Image'].split(":")[0]
		tags = get_docker_tags(repo)

		if not tags:
			error(get_text("error_getting_tags", repo))
			send_message(message=get_text("error_getting_tags", repo))
			return

		botones = []
		for tag in tags:
			callback_data = f"confirmChangeTag|{containerId}|{containerName}|{tag}"
			if len(callback_data) <= 64:
				botones.append(InlineKeyboardButton(tag, callback_data=callback_data))
			else:
				warning(get_text("error_tag_name_too_long", containerName, tag))

		markup.add(*botones)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		send_message(message=get_text("change_tag", containerName), reply_markup=markup)
	except Exception as e:
		error(get_text("error_changing_tag_with_error", containerName, e))
		send_message(message=get_text("error_changing_tag", containerName))

def confirm_update(containerId, containerName):
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_update"), callback_data=f"update|{containerId}|{containerName}"))
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
	pending_updates = sum(1 for c in containers if update_available(c))

	# Build summary
	result = f"📊 <b>{get_text('containers')}:</b> {total_containers}\n"
	result += f"🟢 {get_text('status_running')}: {running_containers}\n"
	result += f"🔴 {get_text('status_stopped')}: {stopped_containers}\n"
	result += f"⬆️ {get_text('status_updates')}: {pending_updates}\n\n"

	# Build container list
	result += "<pre>"
	for container in containers:
		result += f"{get_status_emoji(container.status, container.name, container)} {container.name}"
		if update_available(container):
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
		error(get_text("error_checking_update_with_error", e))

	return status

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
				error(get_text("error_getting_donors_with_error", f"data is not a list [{str(data)}]"))
				return []
		except ValueError:
			error(get_text("error_getting_donors_with_error", f"data is not a json [{response.text}]"))
			return []
	else:
		error(get_text("error_getting_donors_with_error", f"error code [{response.status_code}]"))
		return []

def get_container_id_by_name(container_name, debugging=False):
	if debugging:
		debug(get_text("debug_find_container", container_name))
	containers = docker_manager.list_containers()
	for container in containers:
		if container.name == container_name:
			if debugging:
				debug(get_text("debug_container_found", container_name))
			return container.id[:CONTAINER_ID_LENGTH]
	if debugging:
		debug(get_text("debug_container_not_found", container_name))
	return None

def sanitize_text_for_filename(text):
	sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', text)
	sanitized = re.sub(r'_+', '_', sanitized)
	return sanitized

def write_cache_item(key, value):
	try:
		pickle.dump(value, open(f'{DIR["cache"]}{key}', 'wb'))
	except:
		error(get_text("error_writing_cache_with_error", key))

def read_cache_item(key):
	try:
		return pickle.load(open(f'{DIR["cache"]}{key}', 'rb'))
	except:
		return None
	
def delete_cache_item(key):
	path = f'{DIR["cache"]}{key}'
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

def save_action_data(chat_id, message_id, action_type, containers, selected=None):
	"""Generic function to save selection data for run/stop/restart actions"""
	if selected is None:
		selected = set()
	data = {
		"containers": containers,
		"selected": selected
	}
	write_cache_item(f"{action_type}_data_{chat_id}_{message_id}", data)

def load_action_data(chat_id, message_id, action_type):
	"""Generic function to load selection data for run/stop/restart actions"""
	data = read_cache_item(f"{action_type}_data_{chat_id}_{message_id}")
	if data is None or not isinstance(data, dict):
		return [], set()
	containers = data.get("containers", [])
	selected = data.get("selected", set())
	if not isinstance(selected, set):
		selected = set(selected)
	return containers, selected

def clear_action_data(chat_id, message_id, action_type):
	"""Generic function to clear selection data for run/stop/restart actions"""
	delete_cache_item(f"{action_type}_data_{chat_id}_{message_id}")

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
		error(get_text("error_sending_message", chat_id, str(message), str(e)))
		raise

def _send_document_direct(chat_id, document, reply_markup, caption, parse_mode):
	"""Envía un documento directamente sin usar la cola"""
	try:
		if TELEGRAM_THREAD == 1:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, parse_mode=parse_mode)
		else:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, message_thread_id=TELEGRAM_THREAD, parse_mode=parse_mode)
	except Exception as e:
		error(get_text("error_sending_document", chat_id, e))
		raise

def _delete_message_direct(chat_id, message_id):
	"""Elimina un mensaje directamente sin usar la cola"""
	try:
		bot.delete_message(chat_id, message_id)
	except Exception as e:
		debug(f"No se pudo eliminar mensaje {message_id}: {e}")

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
def delete_message(message_id):
	"""Elimina un mensaje usando la cola (asíncrono)"""
	message_queue.add_message(_delete_message_direct, TELEGRAM_GROUP, message_id, wait_for_result=False)

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
			error(get_text("error_deleting_container_with_error", UPDATER_CONTAINER_NAME, e))

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
		croniter(cron_expression)
		return True
	except Exception:
		return False

def delete_line_from_file(file_path, hash, lock=None):
	try:
		# Use lock if provided (for schedule file synchronization)
		if lock:
			with lock:
				with open(file_path, "r") as file:
					lines = file.readlines()

				deleted = ""
				with open(file_path, "w") as file:
					for line in lines:
						if short_hash(line) != hash:
							file.write(line)
						else:
							deleted = line.strip()
				return deleted
		else:
			with open(file_path, "r") as file:
				lines = file.readlines()

			deleted = ""
			with open(file_path, "w") as file:
				for line in lines:
					if short_hash(line) != hash:
						file.write(line)
					else:
						deleted = line.strip()
			return deleted
	except Exception as e:
		error(get_text("error_deleting_from_file_with_error", e))
		return ""  # Return empty string instead of None

def get_my_architecture():
	try:
		client = docker.from_env()
		info = client.info()
		architecture_docker = info['Architecture']
		return docker_architectures.get(architecture_docker, architecture_docker)
	except Exception as e:
		error(get_text("error_getting_architecture", e))
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
	debug(get_text("debug_starting_bot", VERSION))
	eventMonitor = DockerEventMonitor()
	eventMonitor.demonio_event()
	debug(get_text("debug_starting_monitor_daemon"))
	if CHECK_UPDATES:
		updateMonitor = DockerUpdateMonitor()
		updateMonitor.demonio_update()
		debug(get_text("debug_started_update_daemon"))
	else:
		debug(get_text("debug_disabled_update_daemon"))

	schedule_monitor = DockerScheduleMonitor()
	schedule_monitor.demonio_schedule()
	debug(get_text("debug_started_schedule_daemon"))

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
