"""
The update, against a real Docker daemon.

Everything else in this directory runs against doubles, which can say whether
the rollback deletes the right container but not whether the container that
comes back is the one that went in. Only Docker can say that: whether it
accepts the configuration the bot hands it, and whether what it builds from
it matches what was there. So these create containers with as much
configuration as a docker-compose can carry, update them with the bot's own
code, and compare `docker inspect` before and after.

Every one of these found something the doubles could not: anonymous volumes
— an image's VOLUME nobody mapped, a database's data directory — came back
empty; read-only mounts came back writable; `stop_grace_period`, `-P` and
`expose:` were dropped; a container never started was started; a `--rm`
container was lost; and whatever shared a namespace with an updated
container outside Compose was left inside the dead one.

	python3 tests/run_all.py --docker

Needs a daemon on /var/run/docker.sock, and pulls `registry:2` and
`alpine:3.24.2` the first time. Updates need a registry to pull the "new"
image from, so one runs on 127.0.0.1:55000 for the duration.

Only what these tests create is touched: every container, network and volume
is called `dcbtest-…` and labelled `dcbtest=1`, and the clean-up deletes only
what has both.
"""

import io
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

if harness.REPO not in sys.path:
	sys.path.insert(0, harness.REPO)

import docker
import docker.errors
import docker.types

import docker_update

PREFIX = "dcbtest-"
LABEL = {"dcbtest": "1"}
REGISTRY_NAME = f"{PREFIX}registry"
REGISTRY = "localhost:55000"
IMAGE = f"{REGISTRY}/dcbtest/app"
BASE = "alpine:3.24.2"

# PID 1 ignores SIGTERM unless it says otherwise, and every stop would then
# wait out its whole grace period: minutes, across the file.
KEEP_ALIVE = '["sh", "-c", "trap \'exit 0\' TERM; sleep 3600 & wait"]'

client = docker.from_env()


# --- Fixtures ----------------------------------------------------------------

def _ensure_registry():
	try:
		registry = client.containers.get(REGISTRY_NAME)
		if registry.status != "running":
			registry.start()
	except docker.errors.NotFound:
		client.containers.run("registry:2", name=REGISTRY_NAME, detach=True, labels=LABEL,
								ports={"5000/tcp": ("127.0.0.1", 55000)})
	deadline = time.time() + 30
	while time.time() < deadline:
		try:
			import requests
			if requests.get(f"http://{REGISTRY}/v2/", timeout=2).ok:
				return
		except Exception:
			pass
		time.sleep(0.5)
	raise AssertionError("el registro local no arrancó")


def publish(version, extra="", tag="latest", cmd=KEEP_ALIVE, image=IMAGE):
	"""
	Builds and pushes a version of the test image, and returns its id.

	Pushing the same tag again is what an upstream release looks like from
	the host: the container still runs the old id, and a pull brings the new.
	"""
	dockerfile = (f"FROM {BASE}\n"
					f'LABEL app.version="{version}"\n'
					f"ENV APP_VERSION={version}\n"
					f"{extra}\n"
					f"CMD {cmd}\n")
	reference = f"{image}:{tag}"
	image, _ = client.images.build(fileobj=io.BytesIO(dockerfile.encode()), tag=reference, rm=True, labels=LABEL)
	for line in client.images.push(reference, stream=True, decode=True):
		if "error" in line:
			raise AssertionError(f"push falló: {line}")
	return image.id


# The tests that play the bot itself need a container by its name. Taken for
# ours only with the label as well: a real bot called the same never has it.
BOT_STAND_IN = harness.BOOTSTRAP_ENV["CONTAINER_NAME"]


def _ours(resource):
	labels = (resource.attrs.get("Labels") or (resource.attrs.get("Config") or {}).get("Labels") or {})
	return ((resource.name.startswith(PREFIX) or resource.name == BOT_STAND_IN)
			and labels.get("dcbtest") == "1")


def cleanup():
	"""Removes what these tests made, and only that."""
	subprocess.run(["docker", "compose", "-p", "dcbtest", "down", "-v", "--remove-orphans"],
					capture_output=True)
	for container in client.containers.list(all=True, filters={"label": "dcbtest=1"}):
		if container.name != REGISTRY_NAME and _ours(container):
			container.remove(force=True, v=True)
	for network in client.networks.list(filters={"label": "dcbtest=1"}):
		if _ours(network):
			network.remove()
	for volume in client.volumes.list(filters={"label": "dcbtest=1"}):
		if _ours(volume):
			volume.remove(force=True)
	client.volumes.prune(filters={"label": "dcbtest=1"})


def run(name, **kwargs):
	kwargs.setdefault("labels", {}).update(LABEL)
	return client.containers.run(f"{IMAGE}:latest", name=PREFIX + name, detach=True, **kwargs)


def update(container, tag=None):
	"""docker_update's update, exactly as DockerManager.update calls it."""
	container.reload()
	config = docker_update.extract_container_config(container, tag)
	errors = []
	result = docker_update.perform_update(
		client=client, container=container, config=config, container_name=container.name,
		message=None, edit_message_func=lambda *a, **k: None, debug_func=lambda m: None,
		error_func=errors.append, get_text_func=lambda key, *a: key,
		save_status_func=lambda *a: None, container_id_length=12, telegram_group=0)
	return result, errors


def fetch(name):
	return client.containers.get(PREFIX + name)


def _setup():
	_ensure_registry()
	cleanup()


# --- Comparing what came back --------------------------------------------------

def _flat(value, prefix=""):
	"""
	Every leaf by its dotted path. Empty is empty — Docker reports `[]`, `{}`
	and null for the same nothing depending on how a field was set — and a
	list of strings is a set: link order means nothing.
	"""
	out = {}
	if isinstance(value, dict) and value:
		for key, inner in value.items():
			out.update(_flat(inner, f"{prefix}.{key}" if prefix else key))
	elif value in ([], {}, ""):
		out[prefix] = None
	elif isinstance(value, list) and all(isinstance(item, str) for item in value):
		out[prefix] = sorted(value)
	else:
		out[prefix] = value
	return out


# What is the container's own and changes by being a new one, or what the new
# image is supposed to change.
VOLATILE = ("Id", "Created", "State", "Image", "ImageManifestDescriptor", "ResolvConfPath",
			"HostnamePath", "HostsPath", "LogPath", "RestartCount", "GraphDriver", "Mounts",
			"NetworkSettings.SandboxID", "NetworkSettings.SandboxKey", "NetworkSettings.Ports",
			"Config.Image", "Config.Env", "Config.Labels.app.version",
			# docker-py lists every bind's target as a volume too; harmless.
			"Config.Volumes")
VOLATILE_PARTS = ("EndpointID", "NetworkID", "DNSNames", ".Gateway", ".IPAddress", ".IPPrefixLen",
					".GlobalIPv6", ".IPv6Gateway")


def differences(before, after, allowed=()):
	fa, fb = _flat(before), _flat(after)
	found = {}
	for key in sorted(set(fa) | set(fb)):
		if any(key == v or key.startswith(v + ".") for v in VOLATILE + tuple(allowed)):
			continue
		if any(part in key for part in VOLATILE_PARTS):
			continue
		if fa.get(key) != fb.get(key):
			found[key] = (fa.get(key), fb.get(key))
	return found


# --- Tests -------------------------------------------------------------------

def _full_container():
	"""
	As much configuration as one container takes, running on version 1.

	Shared by the update test and the /compose round trip: both have to give
	back every bit of it.
	"""
	publish("1")
	net1 = client.networks.create(f"{PREFIX}net1", labels=LABEL, ipam=docker.types.IPAMConfig(
		pool_configs=[docker.types.IPAMPool(subnet="172.31.250.0/24")]))
	net2 = client.networks.create(f"{PREFIX}net2", labels=LABEL, ipam=docker.types.IPAMConfig(
		pool_configs=[docker.types.IPAMPool(subnet="172.31.251.0/24")]))
	client.volumes.create(f"{PREFIX}vol", labels=LABEL)
	api = client.api
	host_config = api.create_host_config(
		binds=[f"{PREFIX}vol:/data:rw"],
		port_bindings={80: ("127.0.0.1", 55001), "53/udp": 55002},
		publish_all_ports=True,
		restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
		cap_add=["NET_ADMIN"], cap_drop=["MKNOD"], security_opt=["no-new-privileges:true"],
		mem_limit="64m", mem_reservation="32m", nano_cpus=500000000, cpu_shares=512, pids_limit=100,
		ulimits=[docker.types.Ulimit(name="nofile", soft=1024, hard=2048)],
		sysctls={"net.ipv4.tcp_keepalive_time": "600"}, shm_size="32m", init=True,
		log_config=docker.types.LogConfig(type="json-file", config={"max-size": "1m", "max-file": "2"}),
		devices=["/dev/zero:/dev/myzero:rwm"], group_add=["audio"], oom_score_adj=100,
		tmpfs={"/scratch": "size=1m"}, dns=["1.1.1.1"], dns_search=["example.org"], dns_opt=["ndots:2"],
		extra_hosts={"foo": "10.0.0.1"}, network_mode=f"{PREFIX}net1",
		mounts=[docker.types.Mount(target="/ro", source=f"{PREFIX}vol", type="volume", read_only=True)],
	)
	networking = api.create_networking_config({f"{PREFIX}net1": api.create_endpoint_config(
		ipv4_address="172.31.250.10", aliases=["web"], mac_address="02:42:ac:1f:fa:0a")})
	created = api.create_container(
		f"{IMAGE}:latest", name=f"{PREFIX}full", command=["sh", "-c", "trap 'exit 0' TERM; sleep 999 & wait"],
		environment=["FOO=bar"], labels={**LABEL, "user.label": "x"}, working_dir="/tmp", user="1000:1000",
		hostname="fullhost", domainname="example.org", stop_signal="SIGINT", stop_timeout=42,
		healthcheck={"test": ["CMD", "true"], "interval": 5_000_000_000},
		volumes=["/anon"], ports=[80, (53, "udp"), 8080, 9090],
		host_config=host_config, networking_config=networking)
	container = client.containers.get(created["Id"])
	net2.connect(container, aliases=["second"], ipv4_address="172.31.251.20")
	container.start()
	container.exec_run(["sh", "-c", "echo keep > /anon/file"], user="root")
	container.reload()
	return container


def test_a_container_with_everything_comes_back_identical():
	"""
	As much configuration as one container takes, and every bit of it has to
	survive. Anything in the diff is something a user would find missing.
	"""
	_setup()
	try:
		container = _full_container()
		before = container.attrs

		publish("2")
		result, errors = update(container)
		assert result == "updated_container", (result, errors)

		after = fetch("full")
		assert after.status == "running", after.status
		# What the new image is meant to change, it changed.
		assert "APP_VERSION=2" in after.attrs["Config"]["Env"] and "FOO=bar" in after.attrs["Config"]["Env"]
		assert after.attrs["Config"]["Labels"]["app.version"] == "2"
		# The anonymous volume is the same volume, under its name now.
		assert after.exec_run(["cat", "/anon/file"]).output == b"keep\n"
		found = differences(before, after.attrs, allowed=(
			# The anonymous volume, carried over by name: compose does the same.
			"HostConfig.Binds",
			# Assigned by Docker on a network where none was asked for.
			f"NetworkSettings.Networks.{PREFIX}net2.MacAddress",
		))
		assert not found, "cambió al actualizar:\n" + "\n".join(f"  {k}: {a!r} -> {b!r}" for k, (a, b) in found.items())
		binds = after.attrs["HostConfig"]["Binds"]
		assert f"{PREFIX}vol:/data:rw" in binds and any(b.endswith(":/anon:rw") for b in binds), binds
	finally:
		cleanup()


def test_the_compose_of_a_container_rebuilds_it():
	"""
	/compose, round trip: generate the file, delete the container, `compose
	up` it, and compare. What differs is what the file failed to carry.
	"""
	from compose_generator import ComposeGenerator
	_setup()
	try:
		container = _full_container()
		before = container.attrs
		document = ComposeGenerator(container.name, docker_update.extract_container_config(container)).to_yaml()
		container.remove(force=True)
		directory = tempfile.mkdtemp()
		with open(os.path.join(directory, "compose.yaml"), "w") as handle:
			handle.write(document)
		done = subprocess.run(["docker", "compose", "-p", "dcbtest", "up", "-d"],
								cwd=directory, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr + "\n" + document
		found = differences(before, fetch("full").attrs, allowed=(
			# Compose's own bookkeeping.
			"Config.Labels.com.docker.compose",
			# The same mounts, written the way compose writes them: the
			# read-only volume as a bind, the anonymous one as a mount.
			"HostConfig.Binds", "HostConfig.Mounts",
			# Compose spells capabilities with their prefix.
			"HostConfig.CapAdd", "HostConfig.CapDrop",
			# `-P` has no compose equivalent; the ports are under `expose`.
			"HostConfig.PublishAllPorts",
			# Compose adds the service name as an alias on every network.
			f"NetworkSettings.Networks.{PREFIX}net1.Aliases", f"NetworkSettings.Networks.{PREFIX}net2.Aliases",
			# The inspect output cannot tell a MAC that was set from one that
			# was assigned, and writing it would pin the assigned ones.
			f"NetworkSettings.Networks.{PREFIX}net1.MacAddress", f"NetworkSettings.Networks.{PREFIX}net2.MacAddress",
		))
		assert not found, "no sobrevivió al compose:\n" + "\n".join(
			f"  {k}: {a!r} -> {b!r}" for k, (a, b) in found.items()) + "\n" + document
	finally:
		cleanup()


def test_the_data_in_a_volume_the_image_declares_survives():
	"""postgres, mariadb, mongo: a VOLUME nobody mapped holds the database."""
	_setup()
	try:
		publish("1", "VOLUME /db")
		container = run("db")
		container.exec_run(["sh", "-c", "echo precious > /db/data"])
		publish("2", "VOLUME /db")
		result, errors = update(container)
		assert result == "updated_container", (result, errors)
		assert fetch("db").exec_run(["cat", "/db/data"]).output == b"precious\n"
	finally:
		cleanup()


def test_the_new_images_defaults_replace_the_old_ones():
	"""
	CMD, HEALTHCHECK, ENV and LABEL that came from the image are the image's:
	pinning the old ones on the new container is how updates boot-loop.
	"""
	_setup()
	try:
		publish("1", "HEALTHCHECK --interval=30s CMD true")
		container = run("defaults")
		publish("2", "HEALTHCHECK --interval=7s CMD true",
				cmd='["sh", "-c", "trap \'exit 0\' TERM; sleep 4242 & wait"]')
		result, errors = update(container)
		assert result == "updated_container", (result, errors)
		config = fetch("defaults").attrs["Config"]
		assert "4242" in " ".join(config["Cmd"]), config["Cmd"]
		assert config["Healthcheck"]["Interval"] == 7_000_000_000, config["Healthcheck"]
		assert "APP_VERSION=2" in config["Env"] and "APP_VERSION=1" not in config["Env"]
	finally:
		cleanup()


def test_a_stopped_container_stays_stopped_and_a_created_one_unstarted():
	_setup()
	try:
		publish("1")
		stopped = run("stopped")
		stopped.stop(timeout=0)
		created = client.containers.create(f"{IMAGE}:latest", name=f"{PREFIX}created", labels=LABEL)
		publish("2")
		for container, expected in ((stopped, "exited"), (created, "created")):
			result, errors = update(container)
			assert result == "updated_container", (container.name, result, errors)
		# A stopped container comes back created: it was never started, which
		# is what matters.
		assert fetch("stopped").status in ("created", "exited"), fetch("stopped").status
		assert fetch("created").status == "created", fetch("created").status
	finally:
		cleanup()


def test_a_new_image_that_dies_on_start_is_rolled_back():
	_setup()
	try:
		publish("1")
		container = run("dies")
		original = container.id
		publish("2", cmd='["sh", "-c", "exit 3"]')
		result, errors = update(container)
		assert result == "error_updating_container", result
		back = fetch("dies")
		assert back.id == original and back.status == "running", (back.id[:12], back.status)
		names = [c.name for c in client.containers.list(all=True, filters={"label": "dcbtest=1"})]
		assert f"{PREFIX}dies_old" not in names, names
	finally:
		cleanup()


def test_a_failed_pull_touches_nothing():
	_setup()
	try:
		publish("1")
		container = run("pull")
		result, errors = update(container, tag="does-not-exist")
		assert result == "error_updating_container", result
		back = fetch("pull")
		assert back.id == container.id and back.status == "running"
	finally:
		cleanup()


def test_a_tag_change_moves_to_that_tag():
	_setup()
	try:
		publish("1")
		container = run("tag")
		publish("2", tag="v2")
		result, errors = update(container, tag="v2")
		assert result == "updated_container", (result, errors)
		back = fetch("tag")
		assert back.attrs["Config"]["Image"] == f"{IMAGE}:v2", back.attrs["Config"]["Image"]
		assert "APP_VERSION=2" in back.attrs["Config"]["Env"]
	finally:
		cleanup()


def test_host_none_and_container_network_modes_survive():
	_setup()
	try:
		publish("1")
		host = run("host", network_mode="host")
		none = run("none", network_mode="none")
		parent = run("parent")
		sidecar = run("sidecar", network_mode=f"container:{parent.id}")
		publish("2")
		for container in (host, none, sidecar):
			result, errors = update(container)
			assert result == "updated_container", (container.name, result, errors)
		assert fetch("host").attrs["HostConfig"]["NetworkMode"] == "host"
		assert fetch("none").attrs["HostConfig"]["NetworkMode"] == "none"
		assert fetch("sidecar").attrs["HostConfig"]["NetworkMode"] == f"container:{parent.id}"
		assert fetch("sidecar").status == "running"
	finally:
		cleanup()


def test_a_container_started_with_rm_is_refused_not_lost():
	"""Stopping it deletes it, and with it any chance of a rollback."""
	_setup()
	try:
		publish("1")
		container = run("rm", auto_remove=True)
		publish("2")
		result, errors = update(container)
		assert result == "error_update_auto_remove", result
		back = fetch("rm")
		assert back.id == container.id and back.status == "running"
	finally:
		cleanup()


# --- Through the bot ----------------------------------------------------------
#
# The same update, from the entry point a button reaches, with Telegram
# replaced by a list: what is around docker_update — Compose dependents,
# namespace sharers, the messages — only exists up there.

_bot = None


def _load():
	global _bot
	if _bot is None:
		core, _, _ = harness.load_bot(real_docker=True)
		_bot = core
	core = _bot
	sent = []

	class Sent:
		def __init__(self, n):
			self.message_id = n
			self.chat = type("Chat", (), {"id": 1})()

	def send(message=None, **kwargs):
		sent.append(message)
		return Sent(len(sent))

	core.send_message = send
	core.send_message_to_notification_channel = send
	core.edit_message_text = lambda *a, **k: None
	core.delete_message = lambda *a, **k: None
	return core, sent


def _ref(core, container):
	return core.make_ref(core.host_registry.local_host_id(), container.id[:core.CONTAINER_ID_LENGTH])


COMPOSE = f"""
name: dcbtest
services:
  db:
    image: {IMAGE}:latest
    container_name: {PREFIX}c-db
    labels: [dcbtest=1]
    healthcheck:
      test: ["CMD", "true"]
      interval: 1s
  web:
    image: {IMAGE}:latest
    container_name: {PREFIX}c-web
    labels: [dcbtest=1]
    depends_on:
      db:
        condition: service_healthy
  vpn:
    image: {IMAGE}:latest
    container_name: {PREFIX}c-vpn
    labels: [dcbtest=1]
  app:
    image: {IMAGE}:latest
    container_name: {PREFIX}c-app
    labels: [dcbtest=1]
    network_mode: "service:vpn"
"""


def _compose_up():
	directory = tempfile.mkdtemp()
	with open(os.path.join(directory, "compose.yaml"), "w") as handle:
		handle.write(COMPOSE)
	done = subprocess.run(["docker", "compose", "-p", "dcbtest", "up", "-d", "--wait"],
							cwd=directory, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


def test_a_compose_dependent_is_restarted_after_its_parent_is_healthy():
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_up()
		web_started = fetch("c-web").attrs["State"]["StartedAt"]
		publish("2")
		db = fetch("c-db")
		ok, result = core.perform_container_update(_ref(core, db), db.name)
		assert ok, (result, sent)
		web = fetch("c-web")
		assert web.status == "running" and web.attrs["State"]["StartedAt"] != web_started, web.status
		assert fetch("c-db").id != db.id
	finally:
		cleanup()


def test_a_compose_service_on_anothers_network_follows_it():
	"""`network_mode: service:vpn`: updating vpn must take app with it."""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_up()
		publish("2")
		vpn = fetch("c-vpn")
		ok, result = core.perform_container_update(_ref(core, vpn), vpn.name)
		assert ok, (result, sent)
		new_vpn, app = fetch("c-vpn"), fetch("c-app")
		assert app.attrs["HostConfig"]["NetworkMode"] == f"container:{new_vpn.id}", app.attrs["HostConfig"]["NetworkMode"]
		assert app.status == "running", app.status
	finally:
		cleanup()


def test_a_container_on_anothers_network_outside_compose_follows_it():
	"""`--network container:vpn` by hand, gluetun-style: no Compose to say so."""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		vpn = run("vpn")
		app = run("app", network_mode=f"container:{vpn.id}")
		publish("2")
		ok, result = core.perform_container_update(_ref(core, vpn), vpn.name)
		assert ok, (result, sent)
		new_vpn, app = fetch("vpn"), fetch("app")
		assert new_vpn.id != vpn.id
		assert app.attrs["HostConfig"]["NetworkMode"] == f"container:{new_vpn.id}", app.attrs["HostConfig"]["NetworkMode"]
		assert app.status == "running", app.status
	finally:
		cleanup()


def test_a_single_update_through_the_bot_ends_in_its_summary():
	_setup()
	core, sent = _load()
	try:
		core.store.set("bot.extended_messages", False)
		publish("1")
		container = run("single")
		publish("2")
		core.update_container(_ref(core, container), container.name)
		# The test image is called `app` and sets APP_VERSION: the variable
		# named after the image, which is how official images say it.
		assert sent[-1] == (core.get_text("updated_one") + f"\n🐳 <b>{container.name}</b>"
							"\n   <code>1</code> → <b><code>2</code></b>"), sent
		assert fetch("single").id != container.id
	finally:
		cleanup()


def test_the_tags_of_a_private_registry_are_listed_newest_first():
	"""A registry on the user's own machine, the kind Docker Hub's API cannot see."""
	_setup()
	core, _ = _load()
	try:
		# A repository of its own: the registry outlives each test, and the
		# others push tags of their own to IMAGE.
		repository = f"{IMAGE}-tags"
		for tag in ("1.9", "1.10", "latest"):
			publish("1", tag=tag, image=repository)
		tags = core.get_docker_tags(repository)
		assert tags == ["1.10", "1.9", "latest"], tags
	finally:
		cleanup()


# --- The strangest compose files we could write ------------------------------

MONSTER = f"""
name: dcbtest
x-base: &base
  image: {IMAGE}:latest
  labels: [dcbtest=1]

networks:
  v6:
    enable_ipv6: true
    labels: [dcbtest=1]
    ipam:
      config:
        - subnet: 172.31.240.0/24
        - subnet: fd00:dcb::/64
  back:
    labels: [dcbtest=1]
    internal: true

volumes:
  sub:
    external: true
    name: {PREFIX}sub

secrets:
  s1:
    file: ./secret.txt

services:
  volsrc:
    <<: *base
    container_name: {PREFIX}volsrc
    volumes: ["/shared"]
  volfrom:
    <<: *base
    container_name: {PREFIX}volfrom
    volumes_from: [volsrc]
  subpath:
    <<: *base
    container_name: {PREFIX}subpath
    volumes:
      - type: volume
        source: sub
        target: /sub
        volume: {{subpath: inner, nocopy: true}}
  tmpfsmode:
    <<: *base
    container_name: {PREFIX}tmpfsmode
    read_only: true
    volumes:
      - type: tmpfs
        target: /cache
        tmpfs: {{size: 1048576, mode: 0700}}
  bindprop:
    <<: *base
    container_name: {PREFIX}bindprop
    volumes:
      - type: bind
        source: ./bindsrc
        target: /bind
        read_only: true
        bind: {{propagation: rprivate}}
  ports:
    <<: *base
    container_name: {PREFIX}ports
    ports:
      - "127.0.0.1:55101-55103:8001-8003"
      - "55104:80"
      - "55105:80"
      - "55106:53/udp"
      - "55106:53/tcp"
  v6:
    <<: *base
    container_name: {PREFIX}v6
    networks:
      v6:
        ipv4_address: 172.31.240.50
        ipv6_address: fd00:dcb::50
        aliases: [six]
      back:
        aliases: [backend]
  nohealth:
    <<: *base
    container_name: {PREFIX}nohealth
    healthcheck: {{disable: true}}
  shareipc:
    <<: *base
    container_name: {PREFIX}shareipc
    ipc: shareable
  ipcchild:
    <<: *base
    container_name: {PREFIX}ipcchild
    ipc: "service:shareipc"
    pid: "service:shareipc"
  linked:
    <<: *base
    container_name: {PREFIX}linked
    links: ["volsrc:legacyalias"]
  misc:
    <<: *base
    container_name: {PREFIX}misc
    annotations: {{com.example.note: "hola"}}
    cgroup: host
    extra_hosts: ["gw:host-gateway"]
    logging: {{driver: local, options: {{max-size: 2m}}}}
    stop_signal: SIGUSR1
    stop_grace_period: 1m30s
    entrypoint: ["sh", "-c"]
    command: ["trap 'exit 0' TERM USR1; sleep 3600 & wait"]
    init: true
    domainname: example.test
    mac_address: "02:42:ac:11:00:99"
    secrets: [s1]
    cpuset: "0"
    memswap_limit: 256m
    mem_limit: 128m
    cap_add: [ALL]
    security_opt: ["seccomp=unconfined", "apparmor=unconfined"]
    device_cgroup_rules: ["c 1:3 mr"]
    environment:
      MULTI: "line1\\nline2"
      EQUALS: "a=b=c"
      EMPTY: ""
  scaled:
    <<: *base
    deploy: {{replicas: 2}}
"""


def _compose_project(document, extra_files=None):
	"""Writes a compose project to a temporary directory and brings it up."""
	directory = tempfile.mkdtemp()
	for name, content in (extra_files or {}).items():
		path = os.path.join(directory, name)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w") as handle:
			handle.write(content)
	with open(os.path.join(directory, "compose.yaml"), "w") as handle:
		handle.write(document)
	done = subprocess.run(["docker", "compose", "-p", "dcbtest", "up", "-d", "--wait"],
							cwd=directory, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	return directory


def _project_containers():
	return {c.name: c for c in client.containers.list(
		all=True, filters={"label": "com.docker.compose.project=dcbtest"})}


def test_every_service_of_a_monstrous_compose_survives_its_update():
	"""
	One compose file with everything strange we could find: volumes_from, a
	volume subpath, a tmpfs with a mode, read-only binds with propagation,
	port ranges and a port published twice, IPv4+IPv6 static addresses on two
	networks, a disabled healthcheck, shared IPC and PID namespaces, legacy
	links, annotations, the host's cgroup namespace, host-gateway, the local
	log driver, a 90 s grace period, secrets, cpusets, a service scaled to
	two. Each is updated through the bot, and each has to come back the way
	it was. Found here: links failed every update; subpath, tmpfs mode,
	create_host_path and annotations were dropped.

	Not covered on purpose: compose's `configs:` with inline `content:`.
	Compose copies that file into the container after creating it and keeps
	no record of having done so; a recreation cannot know it is there.
	"""
	_setup()
	core, sent = _load()
	try:
		client.volumes.create(f"{PREFIX}sub", labels=LABEL)
		client.containers.run(BASE, ["mkdir", "-p", "/v/inner"], remove=True,
								volumes={f"{PREFIX}sub": {"bind": "/v", "mode": "rw"}})
		publish("1")
		_compose_project(MONSTER, {"secret.txt": "s3cr3t\n", "bindsrc/f": "hi\n"})
		before = {name: c.attrs for name, c in _project_containers().items()}
		assert len(before) == 14, sorted(before)
		publish("2")
		failed = []
		for name in sorted(before):
			container = client.containers.get(name)
			ok, result = core.perform_container_update(_ref(core, container), name, send_fn=sent.append)
			if not ok:
				failed.append((name, result))
		assert not failed, failed
		found = {}
		for name, attrs in before.items():
			after = client.containers.get(name)
			assert after.status == "running", (name, after.status)
			diff = differences(attrs, after.attrs, allowed=(
				"Config.Labels.com.docker.compose", "HostConfig.Binds",
				# Compose leaves the hostname to Docker: the short id, new.
				"Config.Hostname",
				# Follow the new shareipc, as they must.
				"HostConfig.IpcMode", "HostConfig.PidMode",
				# By name now, which survives the donor being recreated.
				"HostConfig.VolumesFrom",
			))
			diff = {k: v for k, v in diff.items() if not (k.startswith("NetworkSettings.Networks.") and k.endswith(".MacAddress"))
					or name == f"{PREFIX}misc"}
			if diff:
				found[name] = diff
		assert not found, "\n".join(f"{n}:\n" + "\n".join(f"  {k}: {a!r} -> {b!r}" for k, (a, b) in d.items())
									for n, d in found.items())
		# And the namespaces were followed to the new container, not left
		# pointing at the one that is gone.
		share = client.containers.get(f"{PREFIX}shareipc")
		child = client.containers.get(f"{PREFIX}ipcchild").attrs["HostConfig"]
		assert child["IpcMode"] == child["PidMode"] == f"container:{share.id}", child["IpcMode"]
		volfrom = client.containers.get(f"{PREFIX}volfrom").attrs["HostConfig"]["VolumesFrom"]
		assert volfrom == [f"{PREFIX}volsrc"], volfrom
	finally:
		cleanup()


def test_a_container_forced_to_another_platform_keeps_it():
	"""
	`platform: linux/amd64` on an ARM machine — or a 32-bit image on a 64-bit
	Pi. The pull asked for the host's platform, and the update turned an
	x86_64 container into an aarch64 one without a word.
	"""
	_setup()
	try:
		architecture = client.info()["Architecture"]
		foreign = "linux/arm64" if architecture in ("x86_64", "amd64") else "linux/amd64"
		expected = b"aarch64\n" if foreign == "linux/arm64" else b"x86_64\n"
		client.images.pull(BASE, platform=foreign)
		container = client.containers.run(BASE, ["sleep", "300"], name=f"{PREFIX}foreign", detach=True,
											platform=foreign, labels=LABEL)
		assert container.exec_run(["uname", "-m"]).output == expected
		result, errors = update(container)
		assert result == "updated_container", (result, errors)
		assert fetch("foreign").exec_run(["uname", "-m"]).output == expected, "cambió de arquitectura"
	finally:
		cleanup()


def test_a_one_shot_job_runs_again_and_its_dependents_do_not_wait_for_nothing():
	"""
	A migration with `service_completed_successfully` dependents. Its update
	left it created and not started, and the bot waited three minutes for it
	to finish, with the dependents down, while the migration never ran on
	its new image.
	"""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_project(f"""
name: dcbtest
services:
  migrate:
    image: {IMAGE}:latest
    container_name: {PREFIX}migrate
    labels: [dcbtest=1]
    command: ["sh", "-c", "echo migrated $$APP_VERSION"]
  api:
    image: {IMAGE}:latest
    container_name: {PREFIX}api
    labels: [dcbtest=1]
    depends_on:
      migrate: {{condition: service_completed_successfully}}
""")
		publish("2")
		migrate = fetch("migrate")
		started = time.time()
		ok, result = core.perform_container_update(_ref(core, migrate), migrate.name, send_fn=sent.append)
		assert ok, result
		assert time.time() - started < 60, f"tardó {time.time() - started:.0f}s"
		after = fetch("migrate")
		assert after.attrs["State"]["ExitCode"] == 0 and b"migrated 2" in after.logs(), after.logs()
		assert fetch("api").status == "running"
	finally:
		cleanup()


def test_a_dependent_the_user_stopped_stays_stopped():
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_project(f"""
name: dcbtest
services:
  db: {{image: "{IMAGE}:latest", container_name: {PREFIX}db, labels: [dcbtest=1]}}
  web: {{image: "{IMAGE}:latest", container_name: {PREFIX}web, labels: [dcbtest=1], depends_on: [db]}}
  worker: {{image: "{IMAGE}:latest", container_name: {PREFIX}worker, labels: [dcbtest=1], depends_on: [db]}}
""")
		fetch("web").stop()
		worker_started = fetch("worker").attrs["State"]["StartedAt"]
		publish("2")
		db = fetch("db")
		ok, result = core.perform_container_update(_ref(core, db), db.name, send_fn=sent.append)
		assert ok, result
		assert fetch("web").status == "exited", "se arrancó un contenedor que estaba parado"
		worker = fetch("worker")
		assert worker.status == "running" and worker.attrs["State"]["StartedAt"] != worker_started
	finally:
		cleanup()


def test_the_bot_is_never_restarted_as_a_dependent_of_its_proxy():
	"""
	docker-socket-proxy with the bot depending on it. Restarting dependents
	stopped the bot — which, for real, is the bot ending its own process,
	with nothing left to start it again.
	"""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_project(f"""
name: dcbtest
services:
  proxy: {{image: "{IMAGE}:latest", container_name: {PREFIX}proxy, labels: [dcbtest=1]}}
  bot:
    image: "{IMAGE}:latest"
    container_name: {core.CONTAINER_NAME}
    labels: [dcbtest=1]
    depends_on: [proxy]
""")
		bot_started = client.containers.get(core.CONTAINER_NAME).attrs["State"]["StartedAt"]
		publish("2")
		proxy = fetch("proxy")
		ok, result = core.perform_container_update(_ref(core, proxy), proxy.name, send_fn=sent.append)
		assert ok, result
		assert client.containers.get(core.CONTAINER_NAME).attrs["State"]["StartedAt"] == bot_started, \
			"el bot se reinició a sí mismo"
	finally:
		cleanup()


def test_what_the_bot_shares_a_network_with_is_not_updated():
	"""The bot behind gluetun: following the VPN into its new namespace would kill it."""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		vpn = run("vpn")
		client.containers.run(f"{IMAGE}:latest", name=core.CONTAINER_NAME, detach=True, labels=LABEL,
								network_mode=f"container:{vpn.id}")
		publish("2")
		ok, result = core.perform_container_update(_ref(core, vpn), vpn.name, send_fn=sent.append)
		assert not ok and result == core.get_text("error_update_bot_shares_namespace", vpn.name), result
		assert fetch("vpn").id == vpn.id, "se actualizó igualmente"
	finally:
		cleanup()


def test_a_batch_with_a_vpn_and_what_runs_behind_it_updates_both():
	"""
	Updating `vpn` recreates `app`, so `app`'s reference in the batch points
	at a container that is gone: it was reported failed, though it was not.
	"""
	_setup()
	core, sent = _load()
	try:
		core.store.set("bot.extended_messages", False)
		publish("1")
		vpn = run("vpn")
		app = run("app", network_mode=f"container:{vpn.id}")
		publish("2")
		core.update_containers([(_ref(core, vpn), vpn.name), (_ref(core, app), app.name)])
		assert sent[-1].startswith(core.get_text("updated_batch", 2, 2)), sent[-1]
		assert "APP_VERSION=2" in fetch("app").attrs["Config"]["Env"]
	finally:
		cleanup()


def test_docker_run_oddities_survive_their_update():
	"""
	`--link` on the default bridge, macvlan with a fixed IP and MAC, two
	networks with gateway priorities and a fixed MAC on the second, a paused
	container, `systempaths=unconfined`, a volume with driver options and an
	image mount. Found here: the gateway priorities came back 0, the second
	network's MAC was lost, the paused container came back running and /proc
	came back masked.
	"""
	_setup()
	try:
		publish("1")
		image = f"{IMAGE}:latest"
		label = ["--label", "dcbtest=1"]
		commands = [
			["network", "create", *label, "-d", "macvlan", "--subnet", "192.168.250.0/24",
				"--gateway", "192.168.250.1", "-o", "parent=eth0", f"{PREFIX}mac"],
			["network", "create", *label, "--subnet", "172.31.230.0/24", f"{PREFIX}gwa"],
			["network", "create", *label, "--subnet", "172.31.231.0/24", f"{PREFIX}gwb"],
			["run", "-d", *label, "--name", f"{PREFIX}target", image],
			["run", "-d", *label, "--name", f"{PREFIX}link", "--link", f"{PREFIX}target:tgt", image],
			["run", "-d", *label, "--name", f"{PREFIX}macvlan", "--network", f"{PREFIX}mac",
				"--ip", "192.168.250.10", "--mac-address", "02:42:c0:a8:fa:0a", image],
			["run", "-d", *label, "--name", f"{PREFIX}gw", "--network", f"name={PREFIX}gwa,gw-priority=1",
				"--network", f"name={PREFIX}gwb,gw-priority=10,ip=172.31.231.9,mac-address=02:42:ac:1f:e7:09", image],
			["run", "-d", *label, "--name", f"{PREFIX}paused", image],
			["pause", f"{PREFIX}paused"],
			["run", "-d", *label, "--name", f"{PREFIX}sysp", "--security-opt", "systempaths=unconfined", image],
			["run", "-d", *label, "--name", f"{PREFIX}volopt", "--mount",
				"type=volume,dst=/opt/t,volume-driver=local,volume-opt=type=tmpfs,volume-opt=device=tmpfs,volume-opt=o=size=1m", image],
			["run", "-d", *label, "--name", f"{PREFIX}imgmount", "--mount", f"type=image,source={BASE},target=/img", image],
		]
		for command in commands:
			done = subprocess.run(["docker", *command], capture_output=True, text=True)
			assert done.returncode == 0, (command, done.stderr)
		names = ["link", "macvlan", "gw", "paused", "sysp", "volopt", "imgmount"]
		before = {name: fetch(name).attrs for name in names}
		publish("2")
		found = {}
		for name in names:
			result, errors = update(fetch(name))
			assert result == "updated_container", (name, result, errors)
			after = fetch(name)
			diff = differences(before[name], after.attrs, allowed=(
				"Config.AttachStdout", "Config.AttachStderr", "Config.Hostname",
				# Docker's own MAC on the default bridge.
				"NetworkSettings.Networks.bridge.MacAddress",
				f"NetworkSettings.Networks.{PREFIX}gwa.MacAddress",
				# The CLI consumes systempaths=unconfined; MaskedPaths carries it.
				"HostConfig.SecurityOpt"))
			if diff:
				found[name] = diff
		assert not found, "\n".join(f"{n}:\n" + "\n".join(f"  {k}: {a!r} -> {b!r}" for k, (a, b) in d.items())
									for n, d in found.items())
		assert fetch("paused").status == "paused"
	finally:
		cleanup()


def test_volumes_from_survives_its_donor_being_updated_first():
	"""
	Compose records `volumes_from` by the donor's id. Updating the donor gave
	it a new one, and every update of the borrower after that failed on a
	container that no longer existed.
	"""
	_setup()
	core, sent = _load()
	try:
		publish("1")
		_compose_project(f"""
name: dcbtest
services:
  donor: {{image: "{IMAGE}:latest", container_name: {PREFIX}donor, labels: [dcbtest=1], volumes: ["/shared"]}}
  borrower: {{image: "{IMAGE}:latest", container_name: {PREFIX}borrower, labels: [dcbtest=1], volumes_from: [donor]}}
""")
		fetch("donor").exec_run(["sh", "-c", "echo lent > /shared/f"])
		publish("2")
		for name in ("donor", "borrower"):
			container = fetch(name)
			ok, result = core.perform_container_update(_ref(core, container), container.name, send_fn=sent.append)
			assert ok, (name, result)
		assert fetch("borrower").exec_run(["cat", "/shared/f"]).output == b"lent\n"
		# And once more, now that it refers to the donor by name.
		publish("3")
		borrower = fetch("borrower")
		ok, result = core.perform_container_update(_ref(core, borrower), borrower.name, send_fn=sent.append)
		assert ok, result
	finally:
		cleanup()


def test_the_compose_of_every_monstrous_service_is_a_valid_file():
	"""/compose of each, checked by compose itself."""
	from compose_generator import ComposeGenerator
	_setup()
	try:
		client.volumes.create(f"{PREFIX}sub", labels=LABEL)
		client.containers.run(BASE, ["mkdir", "-p", "/v/inner"], remove=True,
								volumes={f"{PREFIX}sub": {"bind": "/v", "mode": "rw"}})
		publish("1")
		_compose_project(MONSTER, {"secret.txt": "s3cr3t\n", "bindsrc/f": "hi\n"})
		invalid = {}
		for name, container in sorted(_project_containers().items()):
			document = ComposeGenerator(name, docker_update.extract_container_config(container)).to_yaml()
			directory = tempfile.mkdtemp()
			with open(os.path.join(directory, "compose.yaml"), "w") as handle:
				handle.write(document)
			done = subprocess.run(["docker", "compose", "config", "-q"], cwd=directory, capture_output=True, text=True)
			if done.returncode:
				invalid[name] = done.stderr.strip() + "\n" + document
		assert not invalid, "\n\n".join(f"{n}: {e}" for n, e in invalid.items())
	finally:
		cleanup()


def _versioned(version):
	return (f'LABEL org.opencontainers.image.version="{version}" '
			f'org.opencontainers.image.source="https://github.com/dgongut/docker-controller-bot"')


def test_an_update_says_which_version_it_goes_from_and_to():
	"""
	The check, the comparison before confirming, and the summary after: all
	three by the number the image declares, read from what was pulled anyway.
	"""
	_setup()
	core, sent = _load()
	try:
		core.store.set("bot.extended_messages", False)
		publish("1", _versioned("1.4.2"))
		container = run("versioned")
		ref = _ref(core, container)
		publish("2", _versioned("2.0.0"))

		core.manager_for(ref).force_check_update(core.ref_id(ref))
		assert core.store.update_versions(core.ref_host(ref), container.name) == ("1.4.2", "2.0.0")
		announced = [m for m in sent if m and core.get_text("available_update", container.name) in m]
		assert announced and "<code>1.4.2</code> → <b><code>2.0.0</code></b>" in announced[-1], sent

		comparison = core.get_image_comparison(ref, container.name)
		assert (comparison["current_version"], comparison["new_version"]) == ("1.4.2", "2.0.0"), comparison
		notes = core.comparison_version_notes(comparison)
		assert core.get_text("update_major_warning") in notes, notes
		assert comparison["release_notes_url"].startswith("https://github.com/dgongut/docker-controller-bot/releases")

		assert "1.4.2" in core.available_updates_text([(ref, container.name)])

		sent.clear()
		core.update_container(ref, container.name)
		assert sent[-1] == (core.get_text("updated_one") + f"\n🐳 <b>{container.name}</b>"
							"\n   <code>1.4.2</code> → <b><code>2.0.0</code></b>"), sent[-1]
	finally:
		cleanup()


def test_with_extended_messages_the_result_says_the_versions_too():
	_setup()
	core, sent = _load()
	try:
		core.store.set("bot.extended_messages", True)
		publish("1", _versioned("3.1"))
		container = run("versioned-ext")
		publish("2", _versioned("3.2"))
		core.update_container(_ref(core, container), container.name)
		expected = core.get_text("updated_container_versions", container.name,
									"<code>3.1</code> → <b><code>3.2</code></b>")
		assert expected in sent, sent
	finally:
		core.store.set("bot.extended_messages", False)
		cleanup()


def test_the_event_monitor_says_why_a_container_stopped_and_when_it_is_unhealthy():
	"""
	The monitor on the daemon's real event stream, while containers finish,
	fail, run out of memory, are stopped, and fail their healthcheck and
	recover. What it announces is what the user would read.
	"""
	_setup()
	core, _ = _load()
	monitor = core.DockerEventMonitor(core.host_registry.local_host_id())
	announced = []
	monitor._announce = lambda message, detail=None: announced.append((message, detail))
	reader = threading.Thread(target=monitor.detectar_eventos_contenedores, daemon=True)
	try:
		reader.start()
		deadline = time.time() + 10
		while not monitor.listening and time.time() < deadline:
			time.sleep(0.1)
		def go(name, command, **kwargs):
			return client.containers.run(BASE, command, name=PREFIX + name, detach=True, labels=LABEL, **kwargs)
		go("finishes", ["sh", "-c", "sleep 1; exit 0"])
		go("fails", ["sh", "-c", "echo 'ERROR: <sin base de datos>' >&2; sleep 1; exit 3"])
		go("oom", ["sh", "-c", "sleep 1; tail /dev/zero"], mem_limit="8m", memswap_limit="8m")
		stopped = go("stopped", ["sleep", "300"])
		sick = go("sick", ["sh", "-c", "touch /ok; sleep 300"],
					healthcheck={"test": ["CMD", "test", "-f", "/ok"], "interval": 1_000_000_000, "retries": 2})
		time.sleep(4)
		stopped.stop(timeout=1)
		sick.exec_run(["rm", "/ok"])
		time.sleep(6)
		sick.exec_run(["touch", "/ok"])
		time.sleep(4)

		said = {}
		for message, detail in announced:
			for name in ("finishes", "fails", "oom", "stopped", "sick"):
				if f"<b>{PREFIX}{name}</b>" in message:
					said.setdefault(name, []).append((message, detail))
		name = lambda n: PREFIX + n
		assert (core.get_text("container_finished", name("finishes")), None) in said["finishes"], said
		failed = [m for m in said["fails"] if m[0] == core.get_text("container_failed", name("fails"), "3")]
		assert failed and "ERROR: &lt;sin base de datos&gt;" in failed[0][1], said["fails"]
		assert (core.get_text("container_oom", name("oom")), None) in said["oom"], said["oom"]
		assert (core.get_text("stopped_container", name("stopped")), None) in said["stopped"], said["stopped"]
		health = [m for m, _ in said["sick"] if "sick" in m and "🟢" not in m]
		assert health == [core.get_text("container_unhealthy", name("sick")),
							core.get_text("container_healthy_again", name("sick"))], said["sick"]
	finally:
		monitor.stop()
		cleanup()


def test_a_version_label_left_on_the_container_by_4x_is_neither_read_nor_kept():
	"""
	Seen on a real host: 4.x copied every label of the image onto the
	containers it recreated. rosario-duckdns, on d860cc34-ls92, still carried
	org.opencontainers.image.version=c1012ade-ls54 from then, and the RC8
	summary said "c1012ade-ls54" on both sides of an update.
	"""
	_setup()
	core, sent = _load()
	try:
		core.store.set("bot.extended_messages", False)
		publish("1", 'LABEL org.opencontainers.image.version="d860cc34-ls91"')
		container = run("duck", labels={"org.opencontainers.image.version": "c1012ade-ls54"})
		assert core.running_version(core.host_registry.local_host_id(), container) == "d860cc34-ls91"
		publish("2", 'LABEL org.opencontainers.image.version="d860cc34-ls92"')
		core.update_container(_ref(core, container), container.name)
		# Long enough to go one under the other.
		assert sent[-1].endswith("<code>d860cc34-ls91</code>\n    ↓\n   <b><code>d860cc34-ls92</code></b>"), sent[-1]
		labels = fetch("duck").attrs["Config"]["Labels"]
		assert labels.get("org.opencontainers.image.version") == "d860cc34-ls92", "la etiqueta vieja sigue pegada"
	finally:
		cleanup()


def test_zz_the_registry_goes_when_the_tests_are_done():
	"""Last by name, so the registry is there for all the others."""
	cleanup()
	try:
		client.containers.get(REGISTRY_NAME).remove(force=True, v=True)
	except docker.errors.NotFound:
		pass
