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

VERSION = "0.9.0"

# Comprobación inicial de variables
if "abc" == TELEGRAM_TOKEN:
	print("Se necesita configurar el token del bot con la variable TELEGRAM_TOKEN")
	sys.exit(1)

if "abc" == TELEGRAM_CHAT_ID:
	print("Se necesita configurar el chatId del usuario que interactuará con el bot con la variable TELEGRAM_CHAT_ID")
	sys.exit(1)

class DockerManager:
	def __init__(self):
		self.client = docker.from_env()

	def list_containers(self, comando):
		if comando == "/list" or comando == "/logs" or comando == "/logfile" or comando == "/compose":
			containers = self.client.containers.list(all=True)
		elif comando == "/run":
			status = ['paused', 'exited']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		elif comando == "/stop":
			status = ['running', 'restarting']
			filters = {'status': status}
			containers = self.client.containers.list(filters=filters)
		return containers

	def stop_container(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			container.stop()
			return f"He *detenido* el contenedor `{container_name}`."
		except docker.errors.NotFound:
			return f"❌ No se ha encontrado el contenedor `{container_name}`."

	def start_container(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			container.start()
			return f"He *iniciado* el contenedor `{container_name}`."
		except docker.errors.NotFound:
			return f"❌ No se ha encontrado el contenedor `{container_name}`."

	def show_logs(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			logs = container.logs().decode("utf-8")
			return f"Estos son los últimos logs de `{container_name}`:\n\n```{container_name}\n{logs[-3500:]}```"
		except docker.errors.NotFound:
			return f"❌ No se ha encontrado el contenedor `{container_name}`."
		
	def show_logs_raw(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return container.logs().decode("utf-8")
		except docker.errors.NotFound:
			return f"❌ No se ha encontrado el contenedor `{container_name}`."
		
	def get_docker_compose(self, container_id, container_name):
		try:
			container = self.client.containers.get(container_id)
			return f"El docker-compose de `{container_name}`:\n\n```docker-compose.yaml\n{generar_docker_compose(container)}```"
		except docker.errors.NotFound:
			return f"❌ No se ha encontrado el contenedor `{container_name}`."

# Instanciamos el bot y el enlace con el docker
bot = telebot.TeleBot(TELEGRAM_TOKEN)
docker_manager = DockerManager()

@bot.message_handler(commands=["start", "list", "run", "stop", "logs", "logfile", "compose", "version"])
def command_controller(message):
	chatId = message.chat.id
	comando = message.text.split()[0]
	messageId = message.id

	if comando not in ('/start'):
		bot.delete_message(chatId, messageId)

	if not is_admin(chatId):
		bot.send_message(chatId, '❌ Este bot no te pertenece.\n\nSi quieres controlar tus contenedores docker a través de telegram despliégame en tu servidor.\n\nEcha un vistazo en [DockerHub](https://hub.docker.com/r/dgongut/docker-controller-bot) donde encontrarás un docker-compose. \n\n¿Eres curioso? El código se encuentra publicado en [GitHub](https://github.com/dgongut/docker-controller-bot).\n\nSi tienes dudas, pregúntame, soy @dgongut', parse_mode="markdown", disable_web_page_preview=True)
		return

	# Listar contenedores
	if comando in ('/start'):
		texto_inicial = f'*🫡 Docker Controller Bot a su servicio*\n\n'
		texto_inicial += f'Puedes utilizar diversos comandos:\n\n'
		texto_inicial += f' · /list Listado completo de los contenedores\n'
		texto_inicial += f' · /run Arranca contenedores detenidos\n'
		texto_inicial += f' · /stop Detener los contenedores\n'
		texto_inicial += f' · /logs Mira los ultimos logs de tus contenedores\n'
		texto_inicial += f' · /logfile Mira los ultimos logs de tus contenedores en formato fichero\n'
		texto_inicial += f' · /compose Extrae el docker-compose de un contenedor. Esta función se encuentra en fase _experimental_.\n'
		texto_inicial += f' · /version Muestra la versión actual.\n'
		bot.send_message(chatId, texto_inicial, parse_mode="markdown")
	elif comando in ('/list'):
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		containers = docker_manager.list_containers(comando=comando)
		bot.send_message(chatId, display_containers(containers), reply_markup=markup, parse_mode="markdown")
	elif comando in ('/run'):
		markup = InlineKeyboardMarkup(row_width = 1)
		botones = []
		containers = docker_manager.list_containers(comando=comando)
		textoMensaje = "🟢 Pulsa en un contenedor para iniciarlo"
		for container in containers:
			botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'run|{container.id[:5]}|{container.name}'))

		markup.add(*botones)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		bot.send_message(chatId, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="html")
	elif comando in ('/stop'):
		markup = InlineKeyboardMarkup(row_width = 1)
		botones = []
		containers = docker_manager.list_containers(comando=comando)
		textoMensaje = "🔴 Pulsa en un contenedor para detenerlo"
		for container in containers:
			botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'stop|{container.id[:5]}|{container.name}'))

		markup.add(*botones)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		bot.send_message(chatId, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="html")
	elif comando in ('/logs'):
		markup = InlineKeyboardMarkup(row_width = 1)
		botones = []
		containers = docker_manager.list_containers(comando=comando)
		textoMensaje = "📃 Pulsa en un contenedor para ver sus últimos logs"
		for container in containers:
			botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'logs|{container.id[:5]}|{container.name}'))

		markup.add(*botones)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		bot.send_message(chatId, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="html")
	elif comando in ('/logfile'):
		markup = InlineKeyboardMarkup(row_width = 1)
		botones = []
		containers = docker_manager.list_containers(comando=comando)
		textoMensaje = "📃 Pulsa en un contenedor para ver sus logs en modo fichero"
		for container in containers:
			botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'logfile|{container.id[:5]}|{container.name}'))

		markup.add(*botones)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		bot.send_message(chatId, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="html")
	elif comando in ('/compose'):
		markup = InlineKeyboardMarkup(row_width = 1)
		botones = []
		containers = docker_manager.list_containers(comando=comando)
		textoMensaje = "📃 Pulsa en un contenedor para ver su docker-compose.\n\nEsta función se encuentra en fase <b>experimental</b> y puede contener errores, se recomienda verificar el docker-compose."
		for container in containers:
			botones.append(InlineKeyboardButton(f'{get_status_emoji(container.status)} {container.name}', callback_data=f'compose|{container.id[:5]}|{container.name}'))

		markup.add(*botones)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		bot.send_message(chatId, textoMensaje, reply_markup=markup, disable_web_page_preview=True, parse_mode="html")
	elif comando in ('/version'):
		x = bot.send_message(chatId, f'<i>Versión: {VERSION}</i>\nDesarrollado con ❤️ por @dgongut\nSi encuentras un bug estaré encantado de saberlo.', parse_mode="HTML")
		time.sleep(10)
		bot.delete_message(chatId, x.message_id)


@bot.callback_query_handler(func=lambda mensaje: True)
def button_controller(call):
	"""Se ha pulsado un boton"""
	chatId = call.from_user.id
	messageId = call.message.id

	if call.data == "cerrar":
		bot.delete_message(chatId, messageId)
		return

	# RUN
	comando, containerId, containerName = call.data.split("|")
	if comando == "run":
		result = docker_manager.start_container(container_id=containerId, container_name=containerName)
		bot.delete_message(chatId, messageId)
		bot.send_message(chatId, result, parse_mode="markdown")

	# STOP
	elif comando == "stop":
		result = docker_manager.stop_container(container_id=containerId, container_name=containerName)
		bot.delete_message(chatId, messageId)
		bot.send_message(chatId, result, parse_mode="markdown")
	
	# LOGS
	elif comando == "logs":
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		result = docker_manager.show_logs(container_id=containerId, container_name=containerName)
		bot.delete_message(chatId, messageId)
		bot.send_message(chatId, result, reply_markup=markup, parse_mode="markdown")

	# LOGS EN FICHERO
	elif comando == "logfile":
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("❌ - Eliminar", callback_data="cerrar"))
		result = docker_manager.show_logs_raw(container_id=containerId, container_name=containerName)
		bot.delete_message(chatId, messageId)
		fichero_temporal = io.BytesIO(result.encode('utf-8'))
		fecha_hora_actual = datetime.now()
		formato = "%Y.%m.%d_%H.%M.%S"
		fecha_hora_formateada = fecha_hora_actual.strftime(formato)
		fichero_temporal.name = f"logs_{containerName}_{fecha_hora_formateada}.txt"
		x = bot.send_message(chatId, "_Cargando archivo... Espera por favor_", parse_mode="markdown")
		bot.send_document(chat_id=chatId, document=fichero_temporal, reply_markup=markup, caption=f'📃 Logs de {containerName}')
		bot.delete_message(chatId, x.message_id)
	
	# COMPOSE
	elif comando == "compose":
		markup = InlineKeyboardMarkup(row_width = 1)
		markup.add(InlineKeyboardButton("❌ - Cerrar", callback_data="cerrar"))
		result = docker_manager.get_docker_compose(container_id=containerId, container_name=containerName)
		bot.delete_message(chatId, messageId)
		bot.send_message(chatId, result, reply_markup=markup, parse_mode="markdown")

def is_admin(chatId):
    return str(chatId) == str(TELEGRAM_CHAT_ID)

def display_containers(containers):
	result = "```\n"
	for container in containers:
		result += f"{get_status_emoji(container.status)} {container.name}\n"
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

def debug(message, html=False):
    print(message)
    if html:
        bot.send_message(TELEGRAM_CHAT_ID, message, disable_web_page_preview=True, parse_mode="html")
    else:
        bot.send_message(TELEGRAM_CHAT_ID, message, disable_web_page_preview=True)

def generar_docker_compose(contenedor):
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
	bot.set_my_commands([ # Comandos a mostrar en el menú de Telegram
		telebot.types.BotCommand("/start", "Mensaje de bienvenida"),
		telebot.types.BotCommand("/list", "Listado completo de los contenedores"),
		telebot.types.BotCommand("/run", "Arranca contenedores detenidos"),
		telebot.types.BotCommand("/stop", "Detener los contenedores"),
		telebot.types.BotCommand("/logs", "Muestra los últimos logs de un contenedor"),
		telebot.types.BotCommand("/logfile", "Muestra los logs completos de un contenedor en formato fichero"),
		telebot.types.BotCommand("/compose", "Extrae el docker-compose de un contenedor"),
		telebot.types.BotCommand("/version", "Consulta la versión de bot")
		])
	bot.infinity_polling(timeout=60)
