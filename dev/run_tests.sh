#!/bin/sh
# Runs the tests inside Docker, so the result does not depend on the machine:
# the Python of the image, and a Linux engine of its own for the ones that
# need a real daemon. The same two things the CI does, without its runners.
#
#   dev/run_tests.sh                 both
#   dev/run_tests.sh unit [filter]   the unit tests, on the base of the image
#   dev/run_tests.sh docker [tag]    against a real Docker, in a dind of that
#                                    tag (29-dind by default; 28-dind is the
#                                    other one the CI checks)
#
# On a Mac the host's own run differs in both: an older Python reads no
# nanoseconds in a date, and Docker Desktop's daemon does not report the MAC
# of a stopped container the way a Linux engine does.
#
# The dind is privileged and gone when this ends, with every image, container
# and volume the tests made: nothing of it touches the Docker that runs it.
# The test of another platform needs QEMU registered on the host, which
# Docker Desktop brings.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BASE=$(sed -n 's/^ARG BASE_IMAGE=//p' "$ROOT/Dockerfile")
RUNNER=dcb-tests-dind
INSTALL='PIP_BREAK_SYSTEM_PACKAGES=1 PIP_ROOT_USER_ACTION=ignore pip3 install --no-cache-dir -q -r requirements.txt'

unit() {
	echo "== Unit tests on $BASE"
	docker run --rm -v "$ROOT":/app:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 "$BASE" sh -euc "
		apk add --no-cache -q python3 py3-pip py3-paramiko openssh-client tzdata
		$INSTALL
		python3 tests/run_all.py $*"
}

real() {
	tag=${1:-29-dind}
	echo "== Tests against a real Docker, in docker:$tag"
	docker rm -f -v "$RUNNER" >/dev/null 2>&1 || true
	trap 'docker rm -f -v "$RUNNER" >/dev/null 2>&1 || true' EXIT INT TERM
	docker run -d --privileged --name "$RUNNER" -e DOCKER_TLS_CERTDIR= \
		-v "$ROOT":/app:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 "docker:$tag" >/dev/null
	tries=0
	until docker exec "$RUNNER" docker info >/dev/null 2>&1; do
		tries=$((tries + 1))
		[ "$tries" -lt 60 ] || { echo "The daemon in docker:$tag did not start" >&2; exit 1; }
		sleep 1
	done
	docker exec "$RUNNER" sh -euc "
		apk add --no-cache -q python3 py3-pip
		$INSTALL
		echo \"Docker \$(docker version --format '{{.Server.Version}}') · Compose \$(docker compose version --short) · \$(python3 --version)\"
		python3 tests/run_all.py --docker"
}

case "${1:-all}" in
	unit) shift; unit "$@" ;;
	docker) shift; real "$@" ;;
	all) unit; real ;;
	*) echo "Usage: $0 [unit [filter] | docker [tag]]" >&2; exit 2 ;;
esac
