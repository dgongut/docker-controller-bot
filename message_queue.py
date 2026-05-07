"""
Message queue system with rate limiting to avoid saturating Telegram.
Implements:
- Message queue with configurable delays
- Retries with exponential backoff
- Rate limiting error handling
"""

import queue
import time
from threading import Thread, Lock

from logger import debug, error, warning


class MessageQueue:
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

		try:
			for attempt in range(self.max_retries):
				try:
					result = func(*args, **kwargs)
					if result_queue:
						result_queue.put(result)
					return result
				except Exception as e:
					error_msg = str(e)
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

	def add_message(self, func, *args, wait_for_result=False, **kwargs):
		"""Adds a message to the queue. If wait_for_result=True, waits for the result"""
		result_queue = queue.Queue() if wait_for_result else None
		self.queue.put({
			'func': func,
			'args': args,
			'kwargs': kwargs,
			'result_queue': result_queue
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
