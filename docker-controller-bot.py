import re
import os
import telebot
from telebot import util
from telebot.types import InlineKeyboardMarkup
from telebot.types import InlineKeyboardButton
from datetime import datetime
from croniter import croniter
from config import *
import docker
import io
import yaml
import time
import threading
import pickle
import json
import requests
import sys

VERSION = "3.3.0"

def debug(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - DEBUG: {message}')

def error(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - ERROR: {message}')

def warning(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - WARNING: {message}')

if LANGUAGE.lower() not in ("es", "en", "nl", "de", "ru", "gl"):
	error("LANGUAGE only can be ES/EN/NL/DE/RU/GL")
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
if "abc" == TELEGRAM_TOKEN:
	error(get_text("error_bot_token"))
	sys.exit(1)

if "abc" == TELEGRAM_ADMIN:
	error(get_text("error_bot_telegram_admin"))
	sys.exit(1)

if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
	error(get_text("error_bot_telegram_admin_anonymous"))
	sys.exit(1)

if "abc" == CONTAINER_NAME:
	error(get_text("error_bot_container_name"))
	sys.exit(1)

if "abc" == TELEGRAM_GROUP:
	if len(str(TELEGRAM_ADMIN).split(',')) > 1:
		error(get_text("error_multiple_admin_only_with_group"))
		sys.exit(1)
	TELEGRAM_GROUP = TELEGRAM_ADMIN

try:
	TELEGRAM_THREAD = int(TELEGRAM_THREAD)
except:
	error(get_text("error_bot_telegram_thread", TELEGRAM_THREAD))
	sys.exit(1)

try:
	CHECK_UPDATES = bool(int(CHECK_UPDATES_RAW))
except:
	error(get_text("error_bot_check_updates"))
	sys.exit(1)

try:
	CHECK_UPDATE_EVERY_HOURS = float(CHECK_UPDATE_EVERY_HOURS)
except ValueError:
	error(get_text("error_bot_check_updates_interval"))
	sys.exit(1)

try:
	EXTENDED_MESSAGES = bool(int(EXTENDED_MESSAGES_RAW))
except:
	error(get_text("error_bot_extended_messages"))
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
		else:
			containers = self.client.containers.list(all=True)
		status_order = {'running': 0, 'restarting': 1, 'paused': 2, 'exited': 3, 'created': 4, 'dead': 5}
		sorted_containers = sorted(containers, key=lambda x: (0 if x.name == CONTAINER_NAME else 1, status_order.get(x.status, 6), x.name.lower()))
		return sorted_containers

	def stop_container(self, container_id, container_name):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.stop()
			if is_muted():
				send_message_to_notification_channel(message=get_text("stopped_container", container_name))
			return None
		except Exception as e:
			error(get_text("error_stopping_container_with_error", container_name, e))
			return get_text("error_stopping_container", container_name)
		
	def restart_container(self, container_id, container_name):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.restart()
			if is_muted():
				send_message_to_notification_channel(message=get_text("restarted_container", container_name))
			return None
		except Exception as e:
			error(get_text("error_restarting_container_with_error", container_name, e))
			return get_text("error_restarting_container", container_name)

	def start_container(self, container_id, container_name):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container.start()
			if is_muted():
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
					image_status = read_cache_item(image_with_tag, container_name)
				except Exception as e:
					debug(get_text("debug_update_not_cached", container_name, e))

				if get_text("NEED_UPDATE_CONTAINER_TEXT") in image_status:
					possible_update = True

			text = '```\n'
			text += f'{get_text("status")}: {get_status_emoji(container.status, container_name)} ({container.status})\n\n'
			if container.status == "running":
				if 0.0 != used_cpu:
					text += f"- CPU: {used_cpu}%\n\n"
				if ("0.00 MB") not in ram:
					text += f"- RAM: {ram}\n\n"
			text += f'- {get_text("container_id")}: {container_id}\n\n'
			text += f'- {get_text("used_image")}:\n{image_with_tag}\n\n'
			text += f'- {get_text("image_id")}: {container.image.id.replace("sha256:", "")[:CONTAINER_ID_LENGTH]}'
			if CHECK_UPDATES:
				text += f"\n\n{image_status}"
			text += "```"
			return f'📜 {get_text("information")} *{container_name}*:\n{text}', possible_update
		except Exception as e:
			error(get_text("error_showing_info_container_with_error", container_name, e))
			return get_text("error_showing_info_container", container_name), False

	def update(self, container_id, container_name, message, bot, tag=None):
		try:
			if CONTAINER_NAME == container_name:
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
				client = self.client
				container = client.containers.get(container_id)
				container_attrs = container.attrs.get('Config', {})

				container_command = container_attrs.get('Cmd', [])
				container_environment = container_attrs.get('Env', [])
				host_config = container.attrs.get('HostConfig', {})

				container_volumes = host_config.get('Binds', []) if host_config else []
				container_network_mode = host_config.get('NetworkMode', None) if host_config else None
				container_ports = host_config.get('PortBindings', {}) if host_config else {}
				container_restart_policy = host_config.get('RestartPolicy', {}) if host_config else {}
				container_devices = host_config.get('Devices', []) if host_config else []
				container_labels = container_attrs.get('Labels', {})
				container_user = container_attrs.get('User', 'root')

				privileged_mode = host_config.get('Privileged', False) if host_config else False
				tmpfs_mounts = {}
				if host_config and 'Mounts' in host_config:
					tmpfs_mounts = {
						mount.get('Target'): f"size={mount.get('TmpfsOptions', {}).get('SizeBytes', 0)}"
						for mount in host_config.get('Mounts', [])
						if mount.get('Type') == 'tmpfs' and mount.get('TmpfsOptions', {}).get('SizeBytes')
					}

				cap_add_list = host_config.get('CapAdd', []) if host_config else []
				runtime = host_config.get('Runtime', None) if host_config else None
				image_with_tag = container_attrs.get('Image', '')

				if tag:
					image_with_tag = f'{image_with_tag.split(":")[0]}:{tag}'

				container_is_running = container.status == 'running'

				network_settings = container.attrs.get('NetworkSettings', {})

				ipam_config = {}
				ipv4_address = None
				if network_settings and container_network_mode:
					ipam_config = network_settings.get('Networks', {}).get(container_network_mode, {}).get('IPAMConfig', {})
					if ipam_config:
						ipv4_address = ipam_config.get('IPv4Address', None)

				debug(get_text("debug_updating_container", container_name))
				try:
					debug(get_text("debug_pulling_image", image_with_tag))
					bot.edit_message_text(get_text("updating_downloading", container_name), TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
					local_image = container.image.id
					remote_image = client.images.pull(image_with_tag)
					debug(get_text("debug_pulled_image", image_with_tag))
					if container_is_running:
						bot.edit_message_text(get_text("updating_stopping", container_name), TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
						debug(get_text("debug_stopping_container", container_name))
						container.stop()

					try:
						debug(get_text("debug_renaming_old_container", container_name))
						container.rename(f'{container_name}_old')
					except docker.errors.APIError as e:
						error(get_text("error_renaming_container_with_error", container_name, e))
						return get_text("error_renaming_container", container_name)

					debug(get_text("debug_creating_new_container", remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
					bot.edit_message_text(get_text("updating_creating", container_name), TELEGRAM_GROUP, message.message_id, parse_mode="markdown")

					networking_config = None
					if ipv4_address:
						networking_config = client.api.create_networking_config({
							container_network_mode: client.api.create_endpoint_config(ipv4_address=ipv4_address)
						})

					new_container = client.containers.create(
						image_with_tag,
						name=container_name,
						command=container_command,
						environment=container_environment,
						volumes=container_volumes,
						network_mode=container_network_mode,
						ports=container_ports,
						restart_policy=container_restart_policy,
						devices=container_devices,
						labels=container_labels,
						privileged=privileged_mode,
						tmpfs=tmpfs_mounts,
						cap_add=cap_add_list,
						runtime=runtime,
						networking_config=networking_config,
						user=container_user,
						detach=True
					)
					debug(get_text("debug_updated_container", container_name))

					if container_is_running:
						debug(get_text("debug_container_need_to_be_started"))
						bot.edit_message_text(get_text("updating_starting", container_name), TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
						new_container.start()

					try:
						debug(get_text("debug_container_deleting_old_container", container.name))
						bot.edit_message_text(get_text("updating_deleting_old", container.name), TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
						container.remove()
					except docker.errors.APIError as e:
						error(get_text("error_deleting_container_with_error", container.name, e))
						return get_text("error_deleting_container", container.name)

					debug(get_text("debug_deleting_image", local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
					try:
						client.images.remove(local_image)
					except Exception as e:
						debug(get_text("debug_image_can_not_be_deleted", container_name, e))
					write_cache_item(image_with_tag, container_name, get_text("UPDATED_CONTAINER_TEXT"))
					return get_text("updated_container", container_name)
				except Exception as e:
					container.rename(container_name)
					container.start()
					error(get_text("error_creating_new_container_with_error", container_attrs))
					raise e
		except Exception as e:
			error(get_text("error_updating_container_with_error", container_name, e))
			return get_text("error_updating_container", container_name)

	def force_check_update(self, container_id):
		try:
			container = self.client.containers.get(container_id)
			container_attrs = container.attrs.get('Config', {})
			image_with_tag = container_attrs.get('Image', '')
			local_image = container.image.id
			remote_image = self.client.images.pull(image_with_tag)
			debug(get_text("debug_checking_update", container.name, image_with_tag, local_image.replace('sha256:', '')[:CONTAINER_ID_LENGTH], remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
			if local_image != remote_image.id:
				debug(get_text("debug_update_detected", container.name, remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
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
		write_cache_item(image_with_tag, container.name, image_status)

	def delete(self, container_id, container_name):
		try:
			if CONTAINER_NAME == container_name:
				return get_text("error_can_not_do_that")
			container = self.client.containers.get(container_id)
			container_is_running = container.status != 'stop'
			if container_is_running:
				debug(get_text("debug_stopping_container", container_name))
				container.stop()
			container.remove()
			return get_text("deleted_container", container_name)
		except Exception as e:
			error(get_text("error_deleting_container_with_error", container_name, e))
			return get_text("error_deleting_container", container_name)
	
	def has_label(self, container_id, container_name, label):
		try:
			container = self.client.containers.get(container_id)
			labels = container.labels
			if label in labels:
				return True
			return False
		except Exception as e:
			debug(get_text("error_checking_label_with_error", label,container_name, e))
			return False
		
	def prune_containers(self):
		try:
			pruned_containers = self.client.containers.prune()
			debug(get_text("debug_deleted", str(pruned_containers)))
			return get_text("prune_containers"), str(pruned_containers)
		except Exception as e:
			error(get_text("error_prune_containers_with_error", e))
			return get_text("error_prune_containers")
		
	def prune_images(self):
		try:
			pruned_images = self.client.images.prune(filters={'dangling': False})
			debug(get_text("debug_deleted", str(pruned_images)))
			return get_text("prune_images"), str(pruned_images)
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
			debug(get_text("debug_deleted", str(pruned_volumes)))
			return get_text("prune_volumes"), str(pruned_volumes)
		except Exception as e:
			debug(e)
			error(get_text("error_prune_volumes_with_error", e))
			return get_text("error_prune_volumes")	
		
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
			for container in containers:
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
					if local_image != remote_image.id: # Actualización detectada
						if LABEL_AUTO_UPDATE in labels:
							if EXTENDED_MESSAGES:
								send_message(message=get_text("auto_update", container.name))
							debug(get_text("debug_auto_update", container.name))
							x = send_message(message=get_text("updating", container.name))
							result = docker_manager.update(container_id=container.id, container_name=container.name, message=x, bot=bot)
							delete_message(x.message_id)
							send_message(message=result)
							continue
						old_image_status = read_cache_item(image_with_tag, container.name)
						image_status = get_text("NEED_UPDATE_CONTAINER_TEXT")
						debug(get_text("debug_update_detected", container.name, remote_image.id.replace('sha256:', '')[:CONTAINER_ID_LENGTH]))
						try:
							self.client.images.remove(remote_image.id)
						except:
							pass # Si no se puede borrar es porque esta siendo usada por otro contenedor
						if image_status == old_image_status:
							debug(get_text("debug_update_already_notified"))
							continue
						debug(get_text("debug_notifying_update"))
						markup = InlineKeyboardMarkup(row_width = 1)
						markup.add(InlineKeyboardButton(get_text("button_update"), callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}"))
						send_message(message=get_text("available_update", container.name), reply_markup=markup)
					else: # Contenedor actualizado
						image_status = get_text("UPDATED_CONTAINER_TEXT")
				except Exception as e:
					error(get_text("error_checking_update_with_error", e))
					image_status = ""
				write_cache_item(image_with_tag, container.name, image_status)
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
		self._ensure_cron_file_exists()

	def _ensure_cron_file_exists(self):
		if not os.path.exists(self.cron_file):
			with open(self.cron_file, "w") as file:
				pass  # Create an empty file

	def run(self):
		while True:
			try:
				with open(self.cron_file, "r") as file:
					lines = file.readlines()

				now = datetime.now()
				for line in lines:
					schedule, command, name = parse_cron_line(line)
					if schedule and command and name and self.should_run(schedule, now):
						if command == "run":
							containerId = get_container_id_by_name(name)
							run(containerId, name)
						elif command == "stop":
							containerId = get_container_id_by_name(name)
							stop(containerId, name)
						elif command == "restart":
							containerId = get_container_id_by_name(name)
							restart(containerId, name)
						elif command == "mute":
							minutes = int(name)
							mute(minutes)
			except Exception as e:
				error(get_text("error_reading_schedule_file", e))
			time.sleep(60)

	def should_run(self, schedule, now):
		cron = croniter(schedule, now)
		last_execution = cron.get_prev(datetime)
		should_run = last_execution.year == now.year and \
					last_execution.month == now.month and \
					last_execution.day == now.day and \
					last_execution.hour == now.hour and \
					last_execution.minute == now.minute
		return should_run

	def demonio_schedule(self):
		try:
			thread = threading.Thread(target=self.run, daemon=True)
			thread.start()
		except Exception as e:
			error(get_text("error_schedule_daemon", e))
			self.demonio_schedule()

@bot.message_handler(commands=["start", "list", "run", "stop", "restart", "delete", "checkupdate", "changetag", "logs", "logfile", "compose", "mute", "schedule", "info", "version", "donate", "prune"])
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
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'run|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("start_a_container"), reply_markup=markup)
	elif comando in ('/stop', f'/stop@{bot.get_me().username}'):
		if container_id:
			stop(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				if CONTAINER_NAME == container.name:
					continue
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'stop|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("stop_a_container"), reply_markup=markup)
	elif comando in ('/restart', f'/restart@{bot.get_me().username}'):
		if container_id:
			restart(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				if CONTAINER_NAME == container.name:
					continue
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'restart|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("restart_a_container"), reply_markup=markup)
	elif comando in ('/logs', f'/logs@{bot.get_me().username}'):
		if container_id:
			logs(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'logs|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

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
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'logfile|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

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
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'compose|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

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
				with open(FULL_SCHEDULE_PATH, "r") as file:
					lines = file.readlines()

				if len(lines) == 0:
					empty = True
				else:
					for line in lines:
						schedule, command, name = parse_cron_line(line)
						if schedule and command and name:
							botones.append(InlineKeyboardButton(f'{schedule} {command} {name}', callback_data=f'deleteSchedule|{schedule}|{command}|{name}'))
			except Exception as e:
				error(get_text("error_reading_schedule_file", e))

			if empty:
				send_message(message=get_text("empty_schedule"), parse_mode="html")
			else:
				markup.add(*botones)
				markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
				send_message(message=get_text("delete_schedule"), reply_markup=markup)
		else: # SAVE
			schedule, command, name = parse_cron_line(full_schedule)
			if not schedule or not is_valid_cron(schedule) or not command or command not in ('run', 'stop', 'restart', 'mute') or not name:
				send_message(message=get_text("error_adding_schedule", message.text), parse_mode="html")
				return
			if 'mute' not in command and not get_container_id_by_name(name):
				send_message(message=get_text("container_does_not_exist", name))
				return
			if 'mute' in command:
				try:
					int(name)
				except (IndexError, ValueError):
					send_message(message=get_text("error_use_mute_schedule"))
					return
			with open(FULL_SCHEDULE_PATH, "a") as file:
				file.write(f'{schedule} {command} {name}\n')
			send_message(message=get_text("schedule_saved", f'{schedule} {command} {name}'))
	elif comando in ('/info', f'/info@{bot.get_me().username}'):
		if container_id:
			info(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'info|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
			send_message(message=get_text("show_info"), reply_markup=markup)
	elif comando in ('/delete', f'/delete@{bot.get_me().username}'):
		if container_id:
			confirm_delete(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'confirmDelete|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

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
	elif comando in ('/changetag', f'/changetag@{bot.get_me().username}'):
		if container_id:
			change_tag_container(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status, container.name)} {container.name}', callback_data=f'changeTagContainer|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

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
		time.sleep(15)
		delete_message(x.message_id)

	elif comando in ('/donate', f'/donate@{bot.get_me().username}'):
		x = send_message(message=get_text("donate"))
		time.sleep(45)
		delete_message(x.message_id)


@bot.callback_query_handler(func=lambda mensaje: True)
def button_controller(call):
	messageId = call.message.id
	userId = call.from_user.id

	if not is_admin(userId):
		warning(get_text("warning_not_admin", userId, call.from_user.username))
		send_message(chat_id=userId, message=get_text("user_not_admin"))
		return

	delete_message(messageId)
	if call.data == "cerrar":
		return

	call_data_parts = call.data.split("|")
	if len(call_data_parts) == 4:
		if call_data_parts[0] == "deleteSchedule":
			comando, schedule, action, containerName = call_data_parts
		else:
			comando, containerId, containerName, tag = call_data_parts
	elif len(call_data_parts) == 3:
		comando, containerId, containerName = call_data_parts
		tag = None
	elif len(call_data_parts) == 2:
		comando, action = call_data_parts

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
		x = send_message(message=get_text("updating", containerName))
		result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot)
		delete_message(x.message_id)
		send_message(message=result)

	# CONFIRM DELETE
	elif comando == "confirmDelete":
		confirm_delete(containerId, containerName)

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
		delete_line_from_file(FULL_SCHEDULE_PATH, f'{schedule} {action} {containerName}')
		send_message(message=get_text("deleted_schedule", f'{schedule} {action} {containerName}'))

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

def run(containerId, containerName):
	debug(get_text("run_command_for_container", "run", containerName))
	x = send_message(message=get_text("starting", containerName))
	result = docker_manager.start_container(container_id=containerId, container_name=containerName)
	delete_message(x.message_id)
	if result:
		send_message(message=result)

def stop(containerId, containerName):
	debug(get_text("run_command_for_container", "stop", containerName))
	x = send_message(message=get_text("stopping", containerName))
	result = docker_manager.stop_container(container_id=containerId, container_name=containerName)
	delete_message(x.message_id)
	if result:
		send_message(message=result)

def restart(containerId, containerName):
	debug(get_text("run_command_for_container", "restart", containerName))
	x = send_message(message=get_text("restarting", containerName))
	result = docker_manager.restart_container(container_id=containerId, container_name=containerName)
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
	fichero_temporal = get_temporal_file(result, f'logs_{containerName}')
	x = send_message(message=get_text("loading_file"))
	send_document(document=fichero_temporal, reply_markup=markup, caption=get_text("logs", containerName))
	delete_message(x.message_id)

def get_temporal_file(data, fileName):
	fichero_temporal = io.BytesIO(data.encode('utf-8'))
	fecha_hora_actual = datetime.now()
	formato = "%Y.%m.%d_%H.%M.%S"
	fecha_hora_formateada = fecha_hora_actual.strftime(formato)
	fichero_temporal.name = f"{fileName}_{fecha_hora_formateada}.txt"
	return fichero_temporal

def mute(minutes):
	if minutes == 0:
		unmute()
		return
	with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
		mute_file.write(str(time.time() + minutes * 60))
	if minutes == 1:
		send_message(message=get_text("muted_singular"))
	else:
		send_message(message=get_text("muted", minutes))
	threading.Timer(minutes * 60, unmute).start()

def unmute():
	with open(FULL_MUTE_FILE_PATH, 'w') as mute_file:
		mute_file.write('0')
	send_message(message=get_text("unmuted"))

def is_muted():
	with open(FULL_MUTE_FILE_PATH, 'r') as fichero:
		mute_until = float(fichero.readline().strip())
		return time.time() < mute_until
	
def check_mute():
	with open(FULL_MUTE_FILE_PATH, 'r+') as fichero:
		mute_until = float(fichero.readline().strip())
		
		if mute_until != 0:
			if time.time() < mute_until:
				mute_until_seconds = mute_until - time.time()
				threading.Timer(mute_until_seconds, unmute).start()
			else:
				fichero.seek(0)
				fichero.write('0')
				fichero.truncate()

def compose(containerId, containerName):
	debug(get_text("run_command_for_container", "compose", containerName))
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_delete"), callback_data="cerrar"))
	result = docker_manager.get_docker_compose(container_id=containerId, container_name=containerName)
	fichero_temporal = io.BytesIO(result.encode('utf-8'))
	fichero_temporal.name = "docker-compose.txt"
	x = send_message(message=get_text("loading_file"))
	send_document(document=fichero_temporal, reply_markup=markup, caption=get_text("compose", containerName))
	delete_message(x.message_id)

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

def confirm_change_tag(containerId, containerName, tag):
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_change_tag", tag), callback_data=f"changeTag|{containerId}|{containerName}|{tag}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_change_tag", containerName, tag), reply_markup=markup)

def change_tag_container(containerId, containerName):
	markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
	client = docker.from_env()
	container = client.containers.get(containerId)
	repo = container.attrs['Config']['Image'].split(":")[0]
	tags = get_docker_tags(repo)
	if not tags:
		send_message(message=get_text("error_getting_tags", containerName))
		return

	botones = []
	for tag in tags:
		callback_data = f"confirmChangeTag|{containerId}|{containerName}|{tag}"
		if len(callback_data) <= 64:
			botones.append(InlineKeyboardButton(tag, callback_data=f"confirmChangeTag|{containerId}|{containerName}|{tag}"))
		else:
			warning(get_text("error_tag_name_too_long", containerName, tag))
		

	markup.add(*botones)
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("change_tag", containerName), reply_markup=markup)

def confirm_update(containerId, containerName):
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton(get_text("button_confirm_update"), callback_data=f"update|{containerId}|{containerName}"))
	markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
	send_message(message=get_text("confirm_update", containerName), reply_markup=markup)

def is_admin(userId):
	return str(userId) in str(TELEGRAM_ADMIN).split(',')

def display_containers(containers):
	result = "```\n"
	for container in containers:
		result += f"{get_status_emoji(container.status, container.name)} {container.name}"
		image_with_tag = container.attrs['Config']['Image']
		update = False
		if CHECK_UPDATES:
			try:
				image_status = read_cache_item(image_with_tag, container.name)
				if "⬆️" in image_status:
					update = True
			except:
				pass

		if update:
			result += " ⬆️"
		result += "\n"
	result += "```"
	return result

def get_status_emoji(statusStr, containerName):
	status = "🟢"
	if statusStr == "exited" or statusStr == "dead":
		status = "🔴"
	elif statusStr == "restarting" or statusStr == "removing":
		status = "🟡"
	elif statusStr == "paused":
		status = "🟠"
	elif statusStr == "created":
		status = "🔵"
	
	if CONTAINER_NAME == containerName:
		status = "👑"
	return status

def get_update_emoji(containerName):
	status = "✅"

	container_id = get_container_id_by_name(container_name=containerName)
	if not container_id:
		return
	
	client = docker.from_env()
	container = client.containers.get(container_id)
	image_with_tag = container.attrs['Config']['Image']
	image_status = read_cache_item(image_with_tag, container.name)
	if get_text("NEED_UPDATE_CONTAINER_TEXT") in image_status:
		status = "⬆️"

	return status

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

def write_cache_item(image_with_tag, container_name, value):
	try:
		pickle.dump(value, open(f'{DIR["cache"]}{sanitize_text_for_filename(image_with_tag)}_{sanitize_text_for_filename(container_name)}', 'wb'))
	except:
		error(get_text("error_writing_cache_with_error", key))

def read_cache_item(image_with_tag, container_name):
	try:
		return pickle.load(open(f'{DIR["cache"]}{sanitize_text_for_filename(image_with_tag)}_{sanitize_text_for_filename(container_name)}', 'rb'))
	except:
		return ""

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

def delete_message(message_id):
	try:
		bot.delete_message(TELEGRAM_GROUP, message_id)
	except:
		pass

def send_message(chat_id=TELEGRAM_GROUP, message=None, reply_markup=None, parse_mode="markdown", disable_web_page_preview=True):
	try:
		if TELEGRAM_THREAD == 1:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
		else:
			return bot.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview, message_thread_id=TELEGRAM_THREAD)
	except Exception as e:
		error(get_text("error_sending_message", chat_id, message, e))
		pass

def send_message_to_notification_channel(chat_id=TELEGRAM_NOTIFICATION_CHANNEL, message=None, reply_markup=None, parse_mode="markdown", disable_web_page_preview=True):
	if "abc" == TELEGRAM_NOTIFICATION_CHANNEL:
		return send_message(chat_id=TELEGRAM_GROUP, message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
	return send_message(chat_id=chat_id, message=message, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)

def send_document(chat_id=TELEGRAM_GROUP, document=None, reply_markup=None, caption=None, parse_mode="markdown"):
	try:
		if TELEGRAM_THREAD == 1:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, parse_mode=parse_mode)
		else:
			return bot.send_document(chat_id, document=document, reply_markup=reply_markup, caption=caption, message_thread_id=TELEGRAM_THREAD, parse_mode=parse_mode)
	except Exception as e:
		error(get_text("error_sending_document", chat_id, e))
		pass

def delete_updater():
	container_id = get_container_id_by_name(UPDATER_CONTAINER_NAME)
	if container_id:
		client = docker.from_env()
		container = client.containers.get(container_id)
		try:
			container.stop()
			container.remove()
		except Exception as e:
			error(get_text("error_deleting_container_with_error", UPDATER_CONTAINER_NAME, e))
		updater_image = container.image.id
		client.images.remove(updater_image)
		send_message(message=get_text("updated_container", CONTAINER_NAME))

def check_CONTAINER_NAME():
	container_id = get_container_id_by_name(CONTAINER_NAME)
	if not container_id:
		error(get_text("error_bot_container_name"))
		sys.exit(1)

def parse_cron_line(line):
		parts = line.strip().split()
		if len(parts) < 7:
			return None, None, None
		schedule = " ".join(parts[:5])
		command = parts[5]
		name = parts[6]
		return schedule, command, name

def is_valid_cron(cron_expression):
	try:
		croniter(cron_expression)
		return True
	except Exception:
		return False

def delete_line_from_file(file_path, line_to_delete):
	try:
		with open(file_path, "r") as file:
			lines = file.readlines()

		with open(file_path, "w") as file:
			for line in lines:
				if line.strip() != line_to_delete.strip():
					file.write(line)
	except Exception as e:
		error(get_text("error_deleting_from_file_with_error", e))

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
	architecture = get_my_architecture()
	if architecture is None:
		return None

	try:
		if repo_name.startswith("ghcr.io/"):
			return get_docker_tags_from_GitHub(repo_name.replace("ghcr.io/", ""))
		elif repo_name.startswith("lscr.io/"):
			return get_docker_tags_from_DockerHub(repo_name.replace("lscr.io/", ""))
		else:
			return get_docker_tags_from_DockerHub(repo_name)
	except Exception as e:
		error(get_text("error_getting_tags_with_error", repo_name, e))
		send_message(message=get_text("error_getting_tags", repo_name))
		return None

def get_docker_tags_from_DockerHub(repo_name):
	architecture = get_my_architecture()
	if architecture is None:
		return None

	url = f"https://hub.docker.com/v2/repositories/{repo_name}/tags?page_size=99"
	response = requests.get(url)
	if response.status_code == 200:
		data = response.json()
		tags = data.get('results', [])
		filtered_tags = []
		for tag in tags:
			images = tag.get('images', [])
			for image in images:
				if image['architecture'] == architecture:
					filtered_tags.append(tag['name'])
					break
		return filtered_tags
	raise Exception(f'Error calling to {url}: {response.status_code}')

def get_docker_tags_from_GitHub(repo_name):
	url = f"https://api.github.com/repos/{repo_name}/tags?per_page=99"
	response = requests.get(url)
	if response.status_code == 200:
		data = response.json()
		tags = [tag['name'] for tag in data]
		return tags
	else:
		raise Exception(f'Error calling to {url}: {response.status_code}')

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
	
	schedule = DockerScheduleMonitor()
	schedule.demonio_schedule()
	debug(get_text("debug_started_schedule_daemon"))

	bot.set_my_commands([
		telebot.types.BotCommand("/start", get_text("menu_start")),
		telebot.types.BotCommand("/list", get_text("menu_list")),
		telebot.types.BotCommand("/run", get_text("menu_run")),
		telebot.types.BotCommand("/stop", get_text("menu_stop")),
		telebot.types.BotCommand("/restart", get_text("menu_restart")),
		telebot.types.BotCommand("/delete", get_text("menu_delete")),
		telebot.types.BotCommand("/checkupdate", get_text("menu_update")),
		telebot.types.BotCommand("/changetag", get_text("menu_change_tag")),
		telebot.types.BotCommand("/logs", get_text("menu_logs")),
		telebot.types.BotCommand("/logfile", get_text("menu_logfile")),
		telebot.types.BotCommand("/schedule", get_text("menu_schedule")),
		telebot.types.BotCommand("/compose", get_text("menu_compose")),
		telebot.types.BotCommand("/prune", get_text("menu_prune")),		
		telebot.types.BotCommand("/mute", get_text("menu_mute")),
		telebot.types.BotCommand("/info", get_text("menu_info")),
		telebot.types.BotCommand("/version", get_text("menu_version")),
		telebot.types.BotCommand("/donate", get_text("menu_donate"))
		])
	delete_updater()
	check_CONTAINER_NAME()
	check_mute()
	starting_message = f"🫡 *{CONTAINER_NAME}\n{get_text('active')}*"
	if CHECK_UPDATES:
		starting_message += f"\n✅ {get_text('check_for_updates')}"
	else:
		starting_message += f"\n❌ {get_text('check_for_updates')}"
	starting_message += f"\n_⚙️ v{VERSION}_"
	starting_message += f"\n{get_text('channel')}"
	send_message(message=starting_message)
	bot.infinity_polling(timeout=60)
