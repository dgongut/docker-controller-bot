"""
Working out which container the bot is, without being told.

The fixtures in tests/data are real `/proc/self/mountinfo` and
`/proc/self/cgroup` files, captured from containers started on Docker 29 with
cgroup v2 — including one started with `--network host`, which is what the
project's own compose example uses and what breaks every hostname-based
approach.
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

import own_container

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
NOWHERE = os.devnull


def _fixture(name):
    return os.path.join(DATA, name)


def test_the_id_comes_out_of_a_real_mountinfo():
    found = own_container.candidates(_fixture("mountinfo_normal.txt"), NOWHERE)
    assert len(found) == 1, found
    assert len(found[0]) == 64 and all(c in "0123456789abcdef" for c in found[0]), found


def test_host_networking_does_not_break_it():
    """
    The one that matters. With `network_mode: host` the container inherits the
    machine's hostname, so `/etc/hostname` and `socket.gethostname()` both
    return the wrong thing — and the project's own compose example uses host
    networking. The mounts are unaffected by the network mode, which is why
    this is the method and the hostname is not.
    """
    found = own_container.candidates(_fixture("mountinfo_host_network.txt"), NOWHERE)
    assert len(found) == 1, found
    hostname_in_fixture = "docker-desktop"
    assert hostname_in_fixture not in found[0], found


def test_cgroup_v2_says_nothing_and_that_is_expected():
    """
    `/proc/self/cgroup` is the method every old answer on the internet gives.
    Under cgroup v2 the whole file reads `0::/`, so it finds nothing — which
    is exactly why the mount table is what gets read first.
    """
    assert own_container.candidates(NOWHERE, _fixture("cgroup_v2.txt")) == []


def test_cgroup_v1_still_works_as_a_fallback():
    """Old kernels put the id in there, and it costs four lines to still read."""
    path = os.path.join(harness.tempdir(), "cgroup_v1")
    identifier = "b" * 64
    io.open(path, "w", encoding="utf-8").write(
        f"11:name=systemd:/docker/{identifier}\n10:cpuset:/docker/{identifier}\n")
    assert own_container.candidates(NOWHERE, path) == [identifier]


def test_a_mount_that_merely_looks_like_a_container_is_ignored():
    """
    Nothing stops a bind mount whose host-side path contains 64 hex
    characters; it was tried, and it does produce a candidate. What it cannot
    do is land on `/etc/hostname`, which is where Docker puts the container's
    own — so the mount point is what decides, not the path.
    """
    real = "a1" * 32
    decoy = "f0" * 32
    path = os.path.join(harness.tempdir(), "mountinfo_decoy")
    io.open(path, "w", encoding="utf-8").write(
        f"192 183 0:47 /data/containers/{decoy}/hostname /data/hostname rw - virtiofs x rw\n"
        f"194 183 254:1 /docker/containers/{real}/hostname /etc/hostname rw - ext4 /dev/vda1 rw\n")
    assert own_container.candidates(path, NOWHERE) == [real]


def test_nothing_to_find_is_answered_with_nothing():
    """Outside a container there is no id, and that is not an error."""
    assert own_container.candidates(NOWHERE, NOWHERE) == []
    assert own_container.candidates("/does/not/exist", "/nor/this") == []


def test_a_mangled_mount_table_does_not_raise():
    """It is a file read at start-up; it has to fail quietly or not at all."""
    path = os.path.join(harness.tempdir(), "mountinfo_junk")
    io.open(path, "w", encoding="utf-8").write(
        "\n\nsolo dos campos\n1 2 3\n\x00\xef\xbf\xbd raro\n"
        "194 183 254:1 /docker/containers/short/hostname /etc/hostname rw - ext4 x rw\n")
    assert own_container.candidates(path, NOWHERE) == []
