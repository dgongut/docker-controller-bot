"""
The small modules the bot leans on: translations, the logger, the message
queue, the compose generator and the port checks.

None of them needs the bot loaded, so they are tested on their own, each with
the case that once went wrong.
"""

import io
import os
import queue
import shutil
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402  (adds the repo to sys.path)

import yaml

import compose_generator
import i18n
import logger
import message_queue
import port_manager
import store


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------

def test_what_goes_into_a_text_is_not_filled_in_again():
	"""
	The placeholders were replaced one argument at a time over the text that
	already held the earlier ones. `awk '{print $3}'` run with /exec came back
	with the whole output where the `$3` was, twice the output in one message,
	and past 4096 characters no message at all.
	"""
	root = harness.temp_storage()[1]
	try:
		command = "ps aux | awk '{print $3}' && echo $2 $1"
		text = i18n.get_text("executed_command", "web", command, "SALIDA")
		assert command in text, text
		assert text.count("SALIDA") == 1, text
		assert text.count("web") == 1, text

		# Lo mismo con el orden al revés: el comando va en $1 y un $2 suyo no
		# es el nombre del contenedor.
		text = i18n.get_text("error_executing_command_container", "echo $2", "web")
		assert "echo $2" in text and text.count("web") == 1, text
	finally:
		shutil.rmtree(root, ignore_errors=True)


def test_a_placeholder_with_no_value_is_left_as_it_is():
	root = harness.temp_storage()[1]
	try:
		text = i18n.get_text("executed_command", "web")
		assert "<b>web</b>" in text and "$2" in text and "$3" in text, text
	finally:
		shutil.rmtree(root, ignore_errors=True)


def test_the_first_placeholder_does_not_bite_the_tenth():
	root = harness.temp_storage()[1]
	messages = i18n.load_locale(i18n.language())
	messages["test_support_ten"] = "$1 $10 $2"
	try:
		assert i18n.get_text("test_support_ten", "a", "b") == "a $10 b"
		values = [chr(ord("a") + i) for i in range(10)]
		assert i18n.get_text("test_support_ten", *values) == "a j b"
	finally:
		del messages["test_support_ten"]
		shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# logger
# ---------------------------------------------------------------------------

def _logged(message, token):
	"""What the logger writes for `message` with `token` in the environment."""
	original_stdout, original_token = sys.stdout, os.environ.get("TELEGRAM_TOKEN")
	captured = io.StringIO()
	sys.stdout = captured
	if token is None:
		os.environ.pop("TELEGRAM_TOKEN", None)
	else:
		os.environ["TELEGRAM_TOKEN"] = token
	try:
		logger.error(message)
	finally:
		sys.stdout = original_stdout
		if original_token is None:
			os.environ.pop("TELEGRAM_TOKEN", None)
		else:
			os.environ["TELEGRAM_TOKEN"] = original_token
	return captured.getvalue()


def test_the_token_never_reaches_the_log():
	"""
	telebot puts the token in the path of every request, and a failed
	connection quotes the URL whole: every send while the Internet was down
	wrote it out, for /logfile to hand to anyone who asked.
	"""
	token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
	failure = ("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
				f"/bot{token}/sendMessage?chat_id=1&text=hola")
	line = _logged(failure, token)
	assert token not in line and "AAHdq" not in line, line
	assert "/bot123456789:***/sendMessage" in line, line
	assert "chat_id=1&text=hola" in line, line
	# Lo que llega como excepción y no como texto también pasa por aquí.
	line = _logged(ConnectionError(failure), token)
	assert token not in line and "/bot123456789:***/" in line, line


def test_a_token_with_no_id_is_hidden_whole():
	"""What comes before a ':' that is not there would be the token itself."""
	line = _logged("/botABCDEFsecret/getMe", "ABCDEFsecret")
	assert "ABCDEF" not in line and "/bot***/getMe" in line, line


def test_without_a_token_the_line_is_left_alone():
	for token in (None, ""):
		line = _logged("nada que ocultar: 123:abc", token)
		assert line.endswith("ERROR: nada que ocultar: 123:abc\n"), line


# ---------------------------------------------------------------------------
# message_queue
# ---------------------------------------------------------------------------

class _Clock:
	"""Stands in for `time` in message_queue: it notes the waits instead of waiting."""

	def __init__(self):
		self.slept = []

	def sleep(self, seconds):
		self.slept.append(seconds)

	def monotonic(self):
		return sum(self.slept)


def _too_many_requests(retry_after=None):
	"""The exception telebot raises for a 429, as it comes from the API."""
	from telebot.apihelper import ApiTelegramException

	result_json = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
	if retry_after is not None:
		result_json["description"] += f": retry after {retry_after}"
		result_json["parameters"] = {"retry_after": retry_after}
	return ApiTelegramException("sendMessage", None, result_json)


def _run_through_queue(func, max_retries):
	"""
	Runs one message as the queue would, with time stood in for. Returns
	(result, what the waiting caller got, the waits).
	"""
	sender = message_queue.MessageQueue(delay_between_messages=0, max_retries=max_retries)
	clock = _Clock()
	original_time = message_queue.time
	message_queue.time = clock
	answer = queue.Queue()
	try:
		result = sender._execute_message({"func": func, "args": (), "kwargs": {},
											"result_queue": answer, "idempotent": False})
	finally:
		message_queue.time = original_time
		sender.shutdown()
	return result, answer.get_nowait(), clock.slept


def test_a_429_waits_what_telegram_asks_without_spending_attempts():
	"""
	In a group Telegram asks for 30 to 60 seconds, and the fixed 2, 4, 8 and
	16 went by inside that window: every attempt got another 429 and the
	message was lost.
	"""
	calls = []

	def send():
		calls.append(1)
		if len(calls) <= 2:
			raise _too_many_requests(40)
		return "enviado"

	result, answered, slept = _run_through_queue(send, max_retries=2)
	assert result == "enviado" and answered == "enviado", (result, answered)
	assert slept == [41, 41], slept
	assert len(calls) == 3, calls


def test_a_429_does_not_hold_the_queue_past_its_budget():
	"""The queue is serial: past the budget the message is dropped, not waited for."""
	calls = []

	def send():
		calls.append(1)
		raise _too_many_requests(70)

	result, answered, slept = _run_through_queue(send, max_retries=5)
	assert result is None and answered is None, (result, answered)
	assert slept == [71], slept
	assert sum(slept) <= message_queue.RATE_LIMIT_BUDGET_SECONDS
	assert len(calls) == 2, calls


def test_a_429_that_does_not_say_how_long_backs_off_as_before():
	calls = []

	def send():
		calls.append(1)
		raise _too_many_requests()

	result, answered, slept = _run_through_queue(send, max_retries=3)
	assert result is None and answered is None, (result, answered)
	assert slept == [2, 4], slept
	assert len(calls) == 3, calls


def test_a_caller_that_stops_waiting_does_not_take_the_message_with_it():
	"""
	Someone waiting for the message_id gets None at their limit, as before; the
	message stays in the queue and still goes out.
	"""
	sender = message_queue.MessageQueue(delay_between_messages=0)
	original_timeout = message_queue.RESULT_TIMEOUT_SECONDS
	message_queue.RESULT_TIMEOUT_SECONDS = 0.2
	release = threading.Event()
	sent = []

	def slow_send():
		release.wait(5)
		sent.append("enviado")
		return "mensaje"

	try:
		assert sender.add_message(slow_send, wait_for_result=True) is None
		release.set()
		assert sender.drain(5), "la cola no se ha vaciado"
		assert sent == ["enviado"], sent
	finally:
		message_queue.RESULT_TIMEOUT_SECONDS = original_timeout
		release.set()
		sender.shutdown()


# ---------------------------------------------------------------------------
# compose_generator
# ---------------------------------------------------------------------------

def test_a_dollar_in_the_container_survives_compose():
	"""
	Compose interpolates `$VAR` everywhere in the file. A Traefik basicauth
	label and `p$ss$word` written as they are came back with the "variables"
	replaced by nothing: the login broke and a new database got the password
	"p".
	"""
	basicauth = "admin:$apr1$H6uskkkW$IgXLP6ewTrSuBkTrqE8wj/"
	config = {
		"image": "traefik:v3",
		"environment": ["DB_PASSWORD=p$ss$word", "PLAIN=nada"],
		"labels": {"traefik.http.middlewares.auth.basicauth.users": basicauth, "con$dolar": "x"},
		"command": ["sh", "-c", "echo $HOME"],
		"ports": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
		"healthcheck": {"Test": ["CMD-SHELL", "test \"$(cat /ok)\" = 1"], "Interval": 30000000000},
	}
	rendered = compose_generator.ComposeGenerator("web", config).to_yaml()
	service = yaml.safe_load(rendered)["services"]["web"]

	assert service["environment"] == ["DB_PASSWORD=p$$ss$$word", "PLAIN=nada"], service["environment"]
	assert service["labels"]["traefik.http.middlewares.auth.basicauth.users"] == basicauth.replace("$", "$$")
	assert service["command"] == ["sh", "-c", "echo $$HOME"], service["command"]
	assert service["healthcheck"]["test"] == ["CMD-SHELL", "test \"$$(cat /ok)\" = 1"], service["healthcheck"]
	# Compose no interpola las claves: el nombre de la label se queda como está.
	assert "con$dolar" in service["labels"], service["labels"]
	# Y lo que Compose leerá, quitando el escape, es lo que tenía el contenedor.
	assert service["labels"]["traefik.http.middlewares.auth.basicauth.users"].replace("$$", "$") == basicauth
	# Los puertos siguen entre comillas: sin ellas 22:22 es un número en base 60.
	assert "'8080:80'" in rendered, rendered


# ---------------------------------------------------------------------------
# port_manager
# ---------------------------------------------------------------------------

def _bound(name, bindings, network_mode="bridge"):
	"""A container as the inspect returns it, with these PortBindings."""
	return SimpleNamespace(name=name, status="running",
							attrs={"HostConfig": {"NetworkMode": network_mode, "PortBindings": bindings}})


def _host(host_id, containers):
	return SimpleNamespace(host_id=host_id, list_containers=lambda comando="", inspect=False: containers)


def _two_hosts():
	"""Storage with a local host and a remote one, as the settings hold them."""
	root = harness.temp_storage()[1]
	store.set("hosts", [
		{"id": "h_local", "alias": "casa", "url": "unix:///var/run/docker.sock", "local": True},
		{"id": "h_nas", "alias": "nas", "url": "tcp://nas:2375"},
	])
	return root


def test_a_port_range_is_read_as_the_ports_it_takes():
	assert port_manager._host_port_range("8080") == (8080, 8080)
	assert port_manager._host_port_range("8000-8010") == (8000, 8010)
	for broken in ("", "abc", "10-5", "-5", "80-"):
		try:
			port_manager._host_port_range(broken)
		except ValueError:
			continue
		raise AssertionError(f"{broken!r} se ha leído como un puerto")


def test_a_container_that_publishes_a_range_is_not_skipped():
	"""
	`-p 8000-8010:80` is the one HostPort "8000-8010". Reading it as a number
	failed and the whole container was skipped: checking 8005 found nobody on
	it, and the container's other ports went unseen too.
	"""
	root = _two_hosts()
	try:
		web = _bound("web", {
			"80/tcp": [{"HostIp": "", "HostPort": "8000-8010"}],
			"81/tcp": [{"HostIp": "", "HostPort": "basura"}],
			"443/tcp": [{"HostIp": "", "HostPort": "8443"}],
		})
		local = _host("h_local", [web])
		ports = port_manager.PortManager(lambda: local)
		ports._is_port_available = lambda port: True
		for port in (8000, 8005, 8010, 8443):
			assert ports.check_port_availability(port) == (False, "ports_used_by_container", "web"), port
		assert ports.check_port_availability(8011) == (True, "ports_available", None)
		# El generador tampoco da uno del rango.
		assert ports.get_random_available_port(8000, 8011, max_attempts=200) == 8011
	finally:
		shutil.rmtree(root, ignore_errors=True)


def test_a_remote_host_is_answered_from_its_own_bindings():
	"""
	The bind is made in the bot's own network. Asked about the NAS it answered
	about this machine: "used by the system" for a port only taken here, or
	"free" for one a service of the NAS holds.
	"""
	root = _two_hosts()
	try:
		nas = _host("h_nas", [_bound("plex", {"32400/tcp": [{"HostIp": "", "HostPort": "8080"}]})])
		local = _host("h_local", [])
		ports = port_manager.PortManager(lambda: local)
		# Aquí todo está ocupado: lo que conteste del nas no puede salir de aquí.
		bound_here = []
		ports._is_port_available = lambda port: bound_here.append(port) or False

		assert ports.check_port_availability(9000, manager=nas) == (True, "ports_available", None)
		assert ports.check_port_availability(8080, manager=nas) == (False, "ports_used_by_container", "plex")
		assert ports.get_random_available_port(8080, 8081, max_attempts=200, manager=nas) == 8081
		assert bound_here == [], f"se ha mirado esta máquina para el nas: {bound_here}"

		# El host local sí se mira aquí, se pase su manager o ninguno.
		assert ports.check_port_availability(9000, manager=local) == (False, "ports_used_by_system", None)
		assert ports.check_port_availability(9000) == (False, "ports_used_by_system", None)
		assert ports.get_random_available_port(9000, 9001, max_attempts=10, manager=local) is None
		assert bound_here, "el host local no se ha mirado"
	finally:
		shutil.rmtree(root, ignore_errors=True)
