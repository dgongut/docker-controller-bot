"""
Message queue system with rate limiting to avoid saturating Telegram.
Implements:
- Message queue with configurable delays
- Retries with exponential backoff
- Rate limiting error handling
"""

import queue
import time
from threading import Thread

from logger import debug, error, warning


# Lo que Telegram va a contestar igual por muchas veces que se le pregunte.
# Reintentar esto cuesta la espera y la llamada y no cambia nada, y la cola es
# serie: cada segundo que se pasa aquí lo esperan todos los demás mensajes.
#
# El caso corriente es un mensaje que ya no está —el usuario cerró el menú, o
# lo borró el propio bot— y ahí el borrado o la edición no se pueden hacer por
# definición.
PERMANENT_FAILURES = (
	"message is not modified",
	"message to edit not found",
	"message to delete not found",
	"message can't be edited",
	"message can't be deleted",
	"message identifier is not specified",
	"message thread not found",
	"chat not found",
	"bot was blocked by the user",
	"user is deactivated",
	"bot is not a member",
	"not enough rights",
)


# Lo que Telegram rechaza por la petición en sí, no por cómo está él: un 400 es
# un mensaje mal formado o demasiado largo, un 403 un usuario que nunca abrió
# el chat con el bot. Ninguno cambia al repetirlo. La lista de arriba se queda
# para los que llegan sin código.
PERMANENT_CODES = (400, 401, 403, 404)


def _is_permanent(error_message, error_code=None):
	"""Whether asking again would get the same answer."""
	if error_code in PERMANENT_CODES:
		return True
	lowered = error_message.lower()
	if any(f"error code: {code}." in lowered for code in PERMANENT_CODES):
		return True
	return any(marker in lowered for marker in PERMANENT_FAILURES)


# Lo más que un mensaje puede tener parada la cola esperando lo que pide
# Telegram en un 429. En un grupo pide de 30 a 60 segundos, así que caben dos
# esperas; más allá el mensaje se da por perdido, porque la cola es serie y
# todos los demás esperan con él.
RATE_LIMIT_BUDGET_SECONDS = 120

# Lo que espera quien envía y necesita la respuesta —el message_id para sus
# botones—. Cubre una espera de las de un grupo y el envío; si se pasa, recibe
# None como siempre, pero el mensaje no se retira: sigue en la cola y sale
# igual. Esperar el tope entero de arriba dejaría un hilo de telebot parado dos
# minutos, y lo que hay delante en la cola tampoco tiene tope.
RESULT_TIMEOUT_SECONDS = 90


def _is_rate_limited(error_message, error_code=None):
	"""Whether Telegram asked to slow down."""
	return error_code == 429 or "Too Many Requests" in error_message or "429" in error_message


def _retry_after(exception):
	"""Seconds Telegram asked to wait in a 429, or None when it did not say."""
	# Lo trae ApiTelegramException en el JSON de la respuesta; un fallo de
	# conexión, o un 429 que no viene de la API, no lo lleva.
	result = getattr(exception, 'result_json', None)
	parameters = result.get('parameters') if isinstance(result, dict) else None
	if not isinstance(parameters, dict):
		return None
	try:
		seconds = float(parameters.get('retry_after'))
	except (TypeError, ValueError):
		return None
	# NaN no pasa de aquí: ninguna comparación con él es cierta.
	return seconds if seconds >= 0 else None


def _is_ambiguous(error_message):
	"""Whether the request may have been carried out even though it failed."""
	# Un tiempo de lectura agotado quiere decir que la petición salió y lo que
	# no llegó fue la respuesta: Telegram pudo haberla atendido igual. Para una
	# edición o un borrado da lo mismo repetirla, pero un envío repetido es un
	# mensaje duplicado —y el primero, además, sin botones que funcionen,
	# porque su message_id no se llegó a saber—. Un fallo al conectar no entra
	# aquí: la petición no salió y reintentarla es seguro.
	return "read timed out" in error_message.lower()


class MessageQueue:
	def __init__(self, delay_between_messages=0.5, max_retries=3):
		self.queue = queue.Queue()
		self.delay_between_messages = delay_between_messages
		self.max_retries = max_retries
		self.running = True
		self.worker_thread = Thread(target=self._process_queue, daemon=True)
		self.worker_thread.start()
		debug("Message queue started")

	def _process_queue(self):
		"""Continuously processes the message queue"""
		while self.running:
			try:
				# Get the next message from the queue (timeout to allow shutdown)
				message_data = self.queue.get(timeout=1)
			except queue.Empty:
				continue
			try:
				if message_data is None:  # Stop signal
					break

				self._execute_message(message_data)
				time.sleep(self.delay_between_messages)
			except Exception as e:
				error(f"Error processing message queue: {str(e)}")
			finally:
				self.queue.task_done()

	def _execute_message(self, message_data):
		"""Executes a message with retries and exponential backoff"""
		func = message_data['func']
		args = message_data['args']
		kwargs = message_data['kwargs']
		result_queue = message_data.get('result_queue')
		idempotent = message_data.get('idempotent', True)

		try:
			attempt = 0
			# Lo esperado por los 429 que dijeron cuánto: ver
			# RATE_LIMIT_BUDGET_SECONDS.
			waited = 0
			while True:
				try:
					result = func(*args, **kwargs)
					if result_queue:
						result_queue.put(result)
					return result
				except Exception as e:
					error_msg = str(e)
					error_code = getattr(e, 'error_code', None)
					# Nothing to gain from asking again, and the queue is
					# serial: give up now instead of sleeping through two more
					# attempts while every other message waits.
					if _is_permanent(error_msg, error_code):
						debug(f"Not retrying, Telegram will answer the same: {error_msg}")
						if result_queue:
							result_queue.put(None)
						return None
					# Repetirlo podría publicarlo dos veces: mejor un aviso
					# perdido, que queda en el log, que uno duplicado.
					if not idempotent and _is_ambiguous(error_msg):
						warning(f"Not retrying, it may have been delivered already: {error_msg}")
						if result_queue:
							result_queue.put(None)
						return None
					if _is_rate_limited(error_msg, error_code):
						# Telegram dice cuánto esperar, y antes de eso contesta
						# 429 a todo: con 2, 4, 8 y 16 segundos fijos los
						# intentos se gastaban dentro de los 30 a 60 que pide
						# en un grupo y el mensaje se perdía. Esperar lo que
						# pide no gasta intento, porque no ha fallado nada.
						retry_after = _retry_after(e)
						if retry_after is not None:
							wait_time = retry_after + 1
							if waited + wait_time <= RATE_LIMIT_BUDGET_SECONDS:
								warning(f"Rate limit detected. Telegram asks for {retry_after:g}s, waiting {wait_time:g}s before retrying...")
								time.sleep(wait_time)
								waited += wait_time
								continue
							error(f"Giving up on a message: Telegram asks to wait {retry_after:g}s more, and it has already waited {waited:g}s: {error_msg}")
							if result_queue:
								result_queue.put(None)
							return None
						if attempt < self.max_retries - 1:
							wait_time = (2 ** attempt) * 2  # Exponential backoff: 2, 4, 8 seconds
							warning(f"Rate limit detected. Waiting {wait_time}s before retrying...")
							time.sleep(wait_time)
							attempt += 1
							continue
					elif attempt < self.max_retries - 1:
						wait_time = 1 * (attempt + 1)
						debug(f"Error sending message (attempt {attempt + 1}/{self.max_retries}). Retrying in {wait_time}s...")
						time.sleep(wait_time)
						attempt += 1
						continue

					error(f"Final error sending message after {self.max_retries} attempts: {str(e)}")
					if result_queue:
						result_queue.put(None)
					break
		except Exception as e:
			error(f"Error processing message queue: {str(e)}")
			if result_queue:
				result_queue.put(None)

	def add_message(self, func, *args, wait_for_result=False, idempotent=True, **kwargs):
		"""
		Adds a message to the queue. If wait_for_result=True, waits for the result.

		idempotent=False marks what publishes something new (a message, a
		document): it is not retried after a timeout that leaves it unknown
		whether Telegram already did it.
		"""
		result_queue = queue.Queue() if wait_for_result else None
		self.queue.put({
			'func': func,
			'args': args,
			'kwargs': kwargs,
			'result_queue': result_queue,
			'idempotent': idempotent,
		})
		if wait_for_result:
			try:
				return result_queue.get(timeout=RESULT_TIMEOUT_SECONDS)
			except queue.Empty:
				# El mensaje sigue en la cola y se seguirá intentando: lo que
				# se pierde es su respuesta, no el envío.
				error(f"Error processing message queue: no result after {RESULT_TIMEOUT_SECONDS}s, the message stays queued")
				return None
		return None

	def drain(self, timeout):
		"""
		Waits for everything queued so far to go out, up to `timeout` seconds.
		True when it did.

		For stopping: the last thing the bot says — that it is updating itself,
		say — is usually still in the queue when the stop arrives.
		"""
		deadline = time.monotonic() + timeout
		while self.queue.unfinished_tasks:
			if time.monotonic() >= deadline:
				return False
			time.sleep(0.05)
		return True

	def shutdown(self):
		"""Stops the message queue"""
		self.running = False
		self.queue.put(None)
