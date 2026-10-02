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
				if message_data is None:  # Stop signal
					break

				self._execute_message(message_data)
				time.sleep(self.delay_between_messages)
			except queue.Empty:
				continue
			except Exception as e:
				error(f"Error processing message queue: {str(e)}")

	def _execute_message(self, message_data):
		"""Executes a message with retries and exponential backoff"""
		func = message_data['func']
		args = message_data['args']
		kwargs = message_data['kwargs']
		result_queue = message_data.get('result_queue')
		idempotent = message_data.get('idempotent', True)

		try:
			for attempt in range(self.max_retries):
				try:
					result = func(*args, **kwargs)
					if result_queue:
						result_queue.put(result)
					return result
				except Exception as e:
					error_msg = str(e)
					# Nothing to gain from asking again, and the queue is
					# serial: give up now instead of sleeping through two more
					# attempts while every other message waits.
					if _is_permanent(error_msg, getattr(e, 'error_code', None)):
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
					# Detect Telegram rate limiting
					if "Too Many Requests" in error_msg or "429" in error_msg:
						if attempt < self.max_retries - 1:
							wait_time = (2 ** attempt) * 2  # Exponential backoff: 2, 4, 8 seconds
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
				return result_queue.get(timeout=60)  # Wait up to 60 seconds
			except queue.Empty:
				error("Error processing message queue: Timeout waiting for message result")
				return None
		return None

	def shutdown(self):
		"""Stops the message queue"""
		self.running = False
		self.queue.put(None)
