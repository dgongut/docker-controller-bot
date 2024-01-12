import re
import os
import telebot
from telebot import util
from telebot.types import InlineKeyboardMarkup
from telebot.types import InlineKeyboardButton
from datetime import datetime
from config import *
import docker
import io
import yaml
import time
import threading
import pickle

VERSION = "0.97.0 (RC3)"

BUTTON_COLUMNS = 2
CONTAINER_ID_LENGTH = 5
UPDATED_CONTAINER_TEXT = "Contenedor actualizado ✅"
NEED_UPDATE_CONTAINER_TEXT = "Actualización disponible ⬆️"

# Comprobación inicial de variables
if "abc" == TELEGRAM_TOKEN:
	print("ERROR: Se necesita configurar el token del bot con la variable TELEGRAM_TOKEN")
	sys.exit(1)

if "abc" == TELEGRAM_ADMIN:
	print("ERROR: Se necesita configurar el chatId del usuario que interactuará con el bot con la variable TELEGRAM_ADMIN")
	sys.exit(1)

if "abc" == TELEGRAM_GROUP:
	TELEGRAM_GROUP = TELEGRAM_ADMIN

try:
	CHECK_UPDATE_EVERY_HOURS = float(CHECK_UPDATE_EVERY_HOURS)
except ValueError:
	print("ERROR: La variable CHECK_UPDATE_EVERY_HOURS indica al programa cada cuantas horas debe comprobar actualizaciones. Por defecto son 4h.")
	sys.exit(1)

DIR = {"cache": "./cache/"}
for key in DIR:
	try:
		os.mkdir(DIR[key])
	except:
		pass

class DockerManager:
	def __init__(self):
		self.client = docker.from_env()

	def list_containers(self, comando=None):
		if comando == "/run":
			status = ['paused', 'exited']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		elif comando == "/stop":
			status = ['running', 'restarting']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		else:
			containers = self.client.containers.list(all=True)
		return containers

	def stop_container(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			container.stop()
			return None
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."

	def start_container(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			container.start()
			return None
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			logs = container.logs().decode("utf-8")
			return f"📃 Estos son los últimos logs de `{container_name}`:\n\n```{container_name}\n{logs[-3500:]}```"
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
		
	def show_logs_raw(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return container.logs().decode("utf-8")
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
		
	def get_docker_compose(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return f"📃 El docker-compose de `{container_name}`:\n\n```docker-compose.yaml\n{generate_docker_compose(container)}```"
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
		
	def get_info(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			if container.status == "running":
				stats = container.stats(stream=False)
				cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
				system_cpu_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
				online_cpus = stats["cpu_stats"]["online_cpus"]
				cpu_usage_percentage = (cpu_delta / system_cpu_delta) * online_cpus * 100 if system_cpu_delta > 0 and cpu_delta > 0 else 0.0
				used_cpu = round(cpu_usage_percentage, 2)
				memory_stats = stats["memory_stats"]
				stats = memory_stats["stats"]
				active_anon = stats["active_anon"]
				active_file = stats["active_file"]
				inactive_anon = stats["inactive_anon"]
				inactive_file = stats["inactive_file"]
				memory_used = active_anon + active_file + inactive_anon + inactive_file
				used_ram_mb = memory_used / (1024 * 1024)
				limit_mb = memory_stats["limit"] / (1024 * 1024)
				memory_usage_percentage = round((used_ram_mb / limit_mb) * 100, 2)
				if used_ram_mb > 1024:
					used_ram_gb = used_ram_mb / 1024
					limit_mb_gb = limit_mb / 1024
					ram = f"{used_ram_gb:.2f}/{limit_mb_gb:.2f} GB ({memory_usage_percentage}%)"
				else:
					ram = f"{used_ram_mb:.2f}/{limit_mb:.2f} MB ({memory_usage_percentage}%)"
			
			container_attrs = container.attrs['Config']
			used_image, used_tag = container_attrs['Image'].split(":") if ":" in container_attrs['Image'] else (container_attrs['Image'], 'latest')
			try:
				image_status = read_cache_item(used_image)
			except Exception as e:
				debug(f"DEBUG: Se ha consultado por la actualización de {container.name} y no está disponible: [{e}]")
				image_status = ""

			possible_update = False
			if NEED_UPDATE_CONTAINER_TEXT in image_status:
				possible_update = True

			text = '```\n'
			text += f"Estado: {get_status_emoji(container.status)} ({container.status})\n\n"
			if container.status == "running":
				text += f"CPU: {used_cpu}%\n"
				text += f"RAM: {ram}\n"
			text += f"Imagen usada:\n{used_image}:{used_tag}\n{image_status}```"
			return f"📜 Información de `{container_name}`:\n{text}", possible_update
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
		
	def update(self, container_id, container_name, message, bot):
		try:
			container = self.client.containers.get(container_id)
			container_attrs = container.attrs['Config']
			container_command = container.attrs['Config']['Cmd']
			container_environment = container.attrs['Config']['Env']
			container_volumes = container.attrs['HostConfig']['Binds']
			container_network_mode = container.attrs['HostConfig']['NetworkMode']
			container_ports = container.attrs['HostConfig']['PortBindings']
			container_restart_policy = container.attrs['HostConfig']['RestartPolicy']
			container_devices = container.attrs['HostConfig']['Devices']
			used_image, used_tag = container_attrs['Image'].split(":") if ":" in container_attrs['Image'] else (container_attrs['Image'], 'latest')
			image_with_tag = f"{used_image}:{used_tag}"
			container_is_running = container.status != 'stop'

			debug(f"DEBUG: Comprobando actualizaciones de {container.name} ({image_with_tag})")
			try:
				bot.edit_message_text(f"_Actualizando_ `{container_name}`...\nDescargando y verificando actualización...", TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
				local_image = self.client.images.get(used_image)
				debug(f"DEBUG: Haciendo pull de la imagen [{image_with_tag}]")
				remote_image = self.client.images.pull(f'{image_with_tag}')
				debug("DEBUG: Pull completado")
				if local_image.id == remote_image.id:
					write_cache_item(used_image, UPDATED_CONTAINER_TEXT)
					return f"✅ Finalmente el contenedor `{container_name}` ya se encontraba actualizado. No es necesario actualizarlo."
			except Exception as e:
				debug(f"ERROR: No se pudo comprobar la actualización. Error: [{e}]")
				image_status = ""
				write_cache_item(used_image, image_status)
				return f"❌ Lamentablemente ha ocurrido un error al verificar la actualización de `{container_name}`. Consulta los logs del bot para mayor información."

			debug(f"DEBUG: Actualizando contenedor {container_name}, actualmente se encuentra activo: [{container_is_running}]")
			try:
				if container_is_running:
					bot.edit_message_text(f"_Actualizando_ `{container_name}`...\nDeteniendo contenedor...", TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
					debug(f"DEBUG: El contenedor {container_name} está en ejecución. Se detendrá.")
					container.stop()

				try:
					debug("DEBUG: Eliminando contenedor antiguo")
					bot.edit_message_text(f"_Actualizando_ `{container_name}`...\nEliminando contenedor...", TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
					container.remove()
					debug("DEBUG: Contenedor antiguo eliminado")
				except docker.errors.APIError as e:
					debug(f"ERROR: Error al eliminar el contenedor. Error: {e}")
					return f"❌ Lamentablemente ha ocurrido un error al eliminar el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
				
				bot.edit_message_text(f"_Actualizando_ `{container_name}`...\nCreando contenedor...", TELEGRAM_GROUP, message.message_id, parse_mode="markdown")
				new_container = self.client.containers.create(
					image_with_tag,
					name=container_name,
					command=container_command,
					environment=container_environment,
					volumes=container_volumes,
					network_mode=container_network_mode,
					ports=container_ports,
					restart_policy=container_restart_policy,
					devices=container_devices,
					detach=True
				)
				debug("DEBUG: El contenedor nuevo se ha creado exitosamente.")
				if container_is_running:
					debug("DEBUG: El contenedor estaba iniciado anteriormente, lo inicio.")
					new_container.start()

				debug(f"DEBUG: Contenedor {container_name} actualizado.")
			except Exception as e:
				debug(f"ERROR: Error al crear y/o ejecutar el nuevo contenedor. Error: [{e}]\n\n\nLa información del contenedor es: [{container_attrs}]")
				return f"❌ Lamentablemente ha ocurrido un error al crear y/o ejecutar el nuevo contenedor de {container_name}. Consulta los logs del bot para mayor información."
			write_cache_item(used_image, UPDATED_CONTAINER_TEXT)
			return f"✅ Contenedor `{container_name}` actualizado con éxito."
		except docker.errors.NotFound:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name} para actualizarlo.")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."

	def delete(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			container_is_running = container.status != 'stop'
			if container_is_running:
				debug(f"DEBUG: El contenedor {container_name} está en ejecución. Deteniendo antes de su eliminación.")
				container.stop()
			container.remove()
			return f"✅ El contenedor `{container_name}` se ha *eliminado* correctamente"
		except docker.errors.NotFound as e:
			debug(f"ERROR: No se ha encontrado el contenedor {container_name}. Error: [{e}]")
			return f"❌ No se ha encontrado el contenedor `{container_name}`. Consulta los logs del bot para mayor información."
		
class DockerEventMonitor:
	def __init__(self, bot, chat_id):
		self.client = docker.from_env()
		self.bot = bot
		self.chat_id = chat_id

	def detectar_eventos_contenedores(self):
		for event in self.client.events(decode=True):
			if 'status' in event and 'Actor' in event and 'Attributes' in event['Actor']:
				container_name = event['Actor']['Attributes'].get('name', '')
				status = event['status']

				message = None
				if status == "start":
					message = f"🟢 El contenedor `{container_name}` se ha *iniciado*"
				elif status == "stop":
					message = f"🔴 El contenedor `{container_name}` se ha *detenido*"
				
				if message:
					self.bot.send_message(self.chat_id, message, parse_mode="markdown")

	def demonio_event(self):
		try:
			thread = threading.Thread(target=self.detectar_eventos_contenedores, daemon=True)
			thread.start()
		except Exception as e:
			debug(f"ERROR: Se ha producido un error en el demonio de eventos. Relanzando... Error: [{e}]")
			self.demonio_event()


class DockerUpdateMonitor:
	def __init__(self, bot, chat_id):
		self.client = docker.from_env()
		self.bot = bot
		self.chat_id = chat_id

	def detectar_actualizaciones(self):
		containers = self.client.containers.list(all=True)
		while True:
			for container in containers:
				container_attrs = container.attrs['Config']
				used_image, used_tag = container_attrs['Image'].split(":") if ":" in container_attrs['Image'] else (container_attrs['Image'], 'latest')
				debug(f"DEBUG: Comprobando actualizaciones de {container.name} ({used_image}:{used_tag})")
				try:
					local_image = self.client.images.get(used_image)
					remote_image = self.client.images.pull(f'{used_image}:{used_tag}')
					if local_image.id != remote_image.id:
						old_image_status = read_cache_item(used_image)
						image_status = NEED_UPDATE_CONTAINER_TEXT
						self.client.images.remove(remote_image.id) # Borramos la imagen para no ocupar espacio en disco
						if image_status == old_image_status:
							continue
						markup = InlineKeyboardMarkup(row_width = 1)
						markup.add(InlineKeyboardButton("⬆️ - Actualizar", callback_data=f"confirmUpdate|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}"))
						self.bot.send_message(self.chat_id, f"⬆️ *Actualización disponible*: `{container.name}`", reply_markup=markup, parse_mode="markdown")
					else:
						image_status = UPDATED_CONTAINER_TEXT
				except Exception as e:
					debug(f"ERROR: No se pudo comprobar la actualización: [{e}]")
					image_status = ""
				write_cache_item(used_image, image_status)
			debug(f"DEBUG: Comprobaciones de actualizaciones completadas, esperando {CHECK_UPDATE_EVERY_HOURS} horas.")
			time.sleep(CHECK_UPDATE_EVERY_HOURS * 3600)

	def demonio_update(self):
		try:
			thread = threading.Thread(target=self.detectar_actualizaciones, daemon=True)
			thread.start()
		except Exception as e:
			debug(f"ERROR: Se ha producido un error en el demonio de actualizaciones. Relanzando... Error: [{e}]")
			self.demonio_update()
		

# Instanciamos el bot y el enlace con el docker
bot = telebot.TeleBot(TELEGRAM_TOKEN)
docker_manager = DockerManager()

@bot.message_handler(commands=["start", "list", "run", "stop", "delete", "logs", "logfile", "compose", "info", "version"])
def command_controller(message):
	userId = message.from_user.id
	comando = message.text.split(' ', 1)[0].split('@', 1)[0]
	messageId = message.id
	container_name = " ".join(message.text.split()[1:])
	container_id = None
	if container_name:
		container_id = get_container_id_by_name(container_name)
	debug(f"DEBUG: Comando introducido: {comando}")
	debug(f"DEBUG: USUARIO: {userId}")
	debug(f"DEBUG: CHAT/GRUPO: {message.chat.id}")
		
	if not is_admin(userId):
		debug(f"ATENCION: El usuario {userId} (@{message.from_user.username}) no es un administrador y ha tratado de usar el bot.")
		bot.send_message(userId, '❌ Este bot no te pertenece.\n\nSi quieres controlar tus contenedores docker a través de telegram despliégame en tu servidor.\n\nEcha un vistazo en [DockerHub](https://hub.docker.com/r/dgongut/docker-controller-bot) donde encontrarás un docker-compose. \n\n¿Eres curioso? El código se encuentra publicado en [GitHub](https://github.com/dgongut/docker-controller-bot).\n\nSi tienes dudas, pregúntame, soy @dgongut', parse_mode="markdown", disable_web_page_preview=True)
		return
	
	if comando not in ('/start'):
		bot.delete_message(TELEGRAM_GROUP, messageId)

	# Listar contenedores
	if comando in ('/start'):
		texto_inicial = f'*🫡 Docker Controller Bot a su servicio*\n\n'
		texto_inicial += f'Comandos disponibles:\n\n'
		texto_inicial += f' · /list Listado completo de los contenedores.\n'
		texto_inicial += f' · /run Inicia un contenedor.\n'
		texto_inicial += f' · /stop Detiene un contenedor.\n'
		texto_inicial += f' · /delete Elimina un contenedor.\n'
		texto_inicial += f' · /logs Muestra los últimos logs de un contenedor.\n'
		texto_inicial += f' · /logfile Muestra los últimos logs de un contenedor en formato fichero.\n'
		texto_inicial += f' · /compose Extrae el docker-compose de un contenedor. Esta función se encuentra en fase _experimental_.\n'
		texto_inicial += f' · /info Muestra información de un contenedor.\n'
		texto_inicial += f' · /version Muestra la versión actual.\n'
		bot.send_message(TELEGRAM_GROUP, texto_inicial, parse_mode="markdown")
	elif comando in ('/list'):
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		containers = docker_manager.list_containers(comando=comando)
		bot.send_message(TELEGRAM_GROUP, display_containers(containers), reply_markup=markup, parse_mode="markdown")
	elif comando in ('/run'):
		if container_id:
			run(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "🟢 Pulsa en un contenedor para iniciarlo"
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'run|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")
	elif comando in ('/stop'):
		if container_id:
			stop(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "🔴 Pulsa en un contenedor para detenerlo"
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'stop|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")
	elif comando in ('/logs'):
		if container_id:
			logs(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "📃 Pulsa en un contenedor para ver sus últimos logs"
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'logs|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")
	elif comando in ('/logfile'):
		if container_id:
			log_file(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "📃 Pulsa en un contenedor para ver sus logs en modo fichero"
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'logfile|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")
	elif comando in ('/compose'):
		if container_id:
			compose(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "📃 Pulsa en un contenedor para ver su docker-compose.\n\nEsta función se encuentra en fase *experimental* y puede contener errores, se recomienda verificar el docker-compose."
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'compose|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")

	elif comando in ('/info'):
		if container_id:
			info(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "📜 Pulsa en un contenedor para ver su información."
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'info|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")
	elif comando in ('/delete'):
		if container_id:
			confirm_delete(container_id, container_name)
		else:
			markup = InlineKeyboardMarkup(row_width = BUTTON_COLUMNS)
			botones = []
			containers = docker_manager.list_containers(comando=comando)
			textoMensaje = "⚠️ Pulsa en un contenedor para eliminarlo.\nEsta acción no puede deshacerse."
			for container in containers:
				botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'confirmDelete|{container.id[:CONTAINER_ID_LENGTH]}|{container.name}'))

			markup.add(*botones)
			markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
			bot.send_message(TELEGRAM_GROUP, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="markdown")

	elif comando in ('/version'):
		x = bot.send_message(TELEGRAM_GROUP, f'⚙️ _Versión: {VERSION}_\nDesarrollado con ❤️ por @dgongut\n\nSi encuentras cualquier fallo o sugerencia contáctame.\n\nPuedes encontrar todo lo relacionado con este bot en [DockerHub](https://hub.docker.com/r/dgongut/docker-controller-bot) o en [GitHub](https://github.com/dgongut/docker-controller-bot)', parse_mode="markdown")
		time.sleep(15)
		bot.delete_message(TELEGRAM_GROUP, x.message_id)


@bot.callback_query_handler(func=lambda mensaje: True)
def button_controller(call):
	"""Se ha pulsado un boton"""
	messageId = call.message.id
	bot.delete_message(TELEGRAM_GROUP, messageId)

	if call.data == "cerrar":
		return

	# RUN
	comando, containerId, containerName = call.data.split("|")
	if comando == "run":
		run(containerId, containerName)

	# STOP
	elif comando == "stop":
		stop(containerId, containerName)
	
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
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("⬆️ - Sí, actualizar", callback_data=f"update|{containerId}|{containerName}"))
		markup.add(InlineKeyboardButton("❌ - Cancelar", callback_data="cerrar"))
		text = f"⚠️ ¿Estás seguro de que quieres actualizar el contenedor `{containerName}` con la nueva imagen disponible?\n\nSiempre se recomienda comprobar si la configuración actual es compatible con la nueva versión del contenedor.\n\nEsta acción no se puede deshacer desde el bot."
		bot.send_message(TELEGRAM_GROUP, text, reply_markup=markup, parse_mode="markdown")
	
	# UPDATE
	elif comando == "update":
		x = bot.send_message(TELEGRAM_GROUP, f"_Actualizando_ `{containerName}`...", parse_mode="markdown")
		result = docker_manager.update(container_id=containerId, container_name=containerName, message=x, bot=bot)
		bot.delete_message(TELEGRAM_GROUP, x.message_id)
		bot.send_message(TELEGRAM_GROUP, result, parse_mode="markdown")

	# CONFIRM DELETE
	elif comando == "confirmDelete":
		confirm_delete(containerId, containerName)
	
	# DELETE
	elif comando == "delete":
		x = bot.send_message(TELEGRAM_GROUP, f"_Eliminando_ `{containerName}`...", parse_mode="markdown")
		result = docker_manager.delete(container_id=containerId, container_name=containerName)
		bot.delete_message(TELEGRAM_GROUP, x.message_id)
		bot.send_message(TELEGRAM_GROUP, result, parse_mode="markdown")

def run(containerId, containerName):
	debug(f"DEBUG: Ejecutando [run] de [{containerName}]")
	x = bot.send_message(TELEGRAM_GROUP, f"_Iniciando_ `{containerName}`...", parse_mode="markdown")
	result = docker_manager.start_container(container_id=containerId, container_name=containerName)
	bot.delete_message(TELEGRAM_GROUP, x.message_id)
	if result:
		bot.send_message(TELEGRAM_GROUP, result, parse_mode="markdown")

def stop(containerId, containerName):
	debug(f"DEBUG: Ejecutando [stop] de [{containerName}]")
	x = bot.send_message(TELEGRAM_GROUP, f"_Deteniendo_ `{containerName}`...", parse_mode="markdown")
	result = docker_manager.stop_container(container_id=containerId, container_name=containerName)
	bot.delete_message(TELEGRAM_GROUP, x.message_id)
	if result:
		bot.send_message(TELEGRAM_GROUP, result, parse_mode="markdown")

def logs(containerId, containerName):
	debug(f"DEBUG: Ejecutando [logs] de [{containerName}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
	result = docker_manager.show_logs(container_id=containerId, container_name=containerName)
	bot.send_message(TELEGRAM_GROUP, result, reply_markup=markup, parse_mode="markdown")

def log_file(containerId, containerName):
	debug(f"DEBUG: Ejecutando [log_file] de [{containerName}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton("❌ - Eliminar", callback_data="cerrar"))
	result = docker_manager.show_logs_raw(container_id=containerId, container_name=containerName)
	fichero_temporal = io.BytesIO(result.encode('utf-8'))
	fecha_hora_actual = datetime.now()
	formato = "%Y.%m.%d_%H.%M.%S"
	fecha_hora_formateada = fecha_hora_actual.strftime(formato)
	fichero_temporal.name = f"logs_{containerName}_{fecha_hora_formateada}.txt"
	x = bot.send_message(TELEGRAM_GROUP, "_Cargando archivo... Espera por favor_", parse_mode="markdown")
	bot.send_document(chat_id=TELEGRAM_GROUP, document=fichero_temporal, reply_markup=markup, caption=f'📃 Logs de {containerName}')
	bot.delete_message(TELEGRAM_GROUP, x.message_id)

def compose(containerId, containerName):
	debug(f"DEBUG: Ejecutando [compose] de [{containerName}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
	result = docker_manager.get_docker_compose(container_id=containerId, container_name=containerName)
	bot.send_message(TELEGRAM_GROUP, result, reply_markup=markup, parse_mode="markdown")

def info(containerId, containerName):
	debug(f"DEBUG: Ejecutando [info] de [{containerName}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	x = bot.send_message(TELEGRAM_GROUP, f"_Obteniendo información de_ `{containerName}`...", parse_mode="markdown")
	result, possible_update = docker_manager.get_info(container_id=containerId, container_name=containerName)
	bot.delete_message(TELEGRAM_GROUP, x.message_id)
	if possible_update:
		markup.add(InlineKeyboardButton("⬆️ - Actualizar", callback_data=f"confirmUpdate|{containerId}|{containerName}"))
	markup.add(InlineKeyboardButton("❌ - Cancelar", callback_data="cerrar"))
	bot.send_message(TELEGRAM_GROUP, result, reply_markup=markup, parse_mode="markdown")

def confirm_delete(containerId, containerName):
	debug(f"DEBUG: Ejecutando [confirmDelete] de [{containerName}]")
	markup = InlineKeyboardMarkup(row_width = 1)
	markup.add(InlineKeyboardButton("⚠️ - Eliminar contenedor", callback_data=f"delete|{containerId}|{containerName}"))
	markup.add(InlineKeyboardButton("❌ - Cancelar", callback_data="cerrar"))
	text = f"⚠️ ¿Estás seguro de que quieres eliminar el contenedor `{containerName}`?\n\nEsta acción no se puede deshacer."
	bot.send_message(TELEGRAM_GROUP, text, reply_markup=markup, parse_mode="markdown")

def is_admin(userId):
	return str(userId) == str(TELEGRAM_ADMIN)

def display_containers(containers):
	result = "```\n"
	for container in containers:
		result += f"{get_status_emoji(container.status)} {container.name}"
		used_image = container.attrs['Config']['Image'].split(":")[0]
		update = False
		try:
			image_status = read_cache_item(used_image)
			if "⬆️" in image_status:
				update = True
		except:
			update = False

		if update:
			result += " ⬆️"
		result += "\n"
	result += "```"
	return result

def get_status_emoji(statusStr):
	status = "🟢"
	if statusStr == "exited":
		status = "🔴"
	elif statusStr == "restarting":
		status = "🟡"
	elif statusStr == "paused":
		status = "🟠"
	return status

def debug(message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - {message}')

def get_container_id_by_name(container_name):
	debug(f"DEBUG: buscando id del contenedor [{container_name}]")
	containers = docker_manager.list_containers()
	for container in containers:
		if container.name == container_name:
			debug(f"DEBUG: Contenedor [{container_name}] encontrado")
			return container.id[:CONTAINER_ID_LENGTH]
	debug(f"DEBUG: Contenedor [{container_name}] no encontrado")
	return None

def sanitize_text_for_filename(text):
	sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', text)
	sanitized = re.sub(r'_+', '_', sanitized)
	return sanitized

def write_cache_item(key, value):
	pickle.dump(value, open(f'{DIR["cache"]}{sanitize_text_for_filename(key)}', 'wb'))

def read_cache_item(key):
	return pickle.load(open(f'{DIR["cache"]}{sanitize_text_for_filename(key)}', 'rb'))

def generate_docker_compose(contenedor):
	nombre_contenedor = contenedor.name
	imagen_contenedor = contenedor.image.tags[0] if contenedor.image.tags else 'imagen_desconocida'
	
	puertos_mapeados = {}
	for puerto_externo, puertos_internos in contenedor.attrs['NetworkSettings']['Ports'].items():
		if puertos_internos:
			puerto_interno = puertos_internos[0]['HostPort']
			protocolo = puertos_internos[0]['HostIp']
			if protocolo != '0.0.0.0':
				puertos_mapeados[f"{puerto_interno}/{protocolo}"] = puerto_externo
			else:
				puertos_mapeados[f"{puerto_interno}"] = puerto_externo

	variables_entorno = contenedor.attrs['Config']['Env']
	variables_entorno = [var for var in variables_entorno if '=' in var]

	volumenes = []
	for volumen in contenedor.attrs['Mounts']:
		origen = volumen['Source']
		destino = volumen['Destination']
		modo = volumen['Mode']

		if '/var/lib/docker/volumes' not in origen:
			volumenes.append(f"{origen}:{destino}:{modo}")

	compose_data = {
		'version': '3',
		'services': {
			nombre_contenedor: {
				'container_name': nombre_contenedor,
				'image': imagen_contenedor,
				**({'environment': variables_entorno} if variables_entorno else {}),
				**({'ports': puertos_mapeados} if puertos_mapeados else {}),
				**({'volumes': volumenes} if volumenes else {}),
			}
		}
	}

	yaml_data = yaml.safe_dump(compose_data, default_flow_style=False, sort_keys=False)
	return yaml_data

if __name__ == '__main__':
	debug("DEBUG: Arrancando Docker-Controler-Bot")
	eventMonitor = DockerEventMonitor(bot, TELEGRAM_GROUP)
	eventMonitor.demonio_event()
	debug("DEBUG: Demonio monitor activo")
	updateMonitor = DockerUpdateMonitor(bot, TELEGRAM_GROUP)
	updateMonitor.demonio_update()
	debug("DEBUG: Demonio update activo")
	bot.set_my_commands([
		telebot.types.BotCommand("/start", "Menú principal"),
		telebot.types.BotCommand("/list", "Listado completo de los contenedores"),
		telebot.types.BotCommand("/run", "Inicia un contenedor"),
		telebot.types.BotCommand("/stop", "Detiene un contenedor"),
		telebot.types.BotCommand("/delete", "Elimina un contenedor"),
		telebot.types.BotCommand("/logs", "Muestra los últimos logs de un contenedor"),
		telebot.types.BotCommand("/logfile", "Muestra los logs completos de un contenedor en formato fichero"),
		telebot.types.BotCommand("/compose", "Extrae el docker-compose de un contenedor"),
		telebot.types.BotCommand("/info", "Muestra información de un contenedor"),
		telebot.types.BotCommand("/version", "Muestra la versión actual")
		])
	debug("DEBUG: Iniciando interconexión con Telegram")
	bot.infinity_polling(timeout=60)
