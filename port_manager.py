#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Port Manager Module
Handles all port-related operations for Docker Controller Bot
"""

import socket
import random
from typing import Tuple, List, Dict, Set, Optional

import host_registry


def _host_port_range(host_port) -> Tuple[int, int]:
    """
    The ports a HostPort binding takes, as (first, last).

    `-p 8000-8010:80` is stored as the one HostPort "8000-8010", not as eleven
    bindings, so reading it as a number failed and the whole container was
    skipped: checking 8005 said nobody had it. Raises ValueError for anything
    that is neither a port nor a range of them.
    """
    first, dash, last = str(host_port).strip().partition('-')
    start = int(first)
    end = int(last) if dash else start
    if end < start:
        raise ValueError(f"not a port range: {host_port!r}")
    return start, end


class PortManager:
    """Manages port operations for Docker containers"""
    
    def __init__(self, default_manager):
        """
        Initialize PortManager

        Args:
            default_manager: Returns the DockerManager to use when a call names
                none. A function and not a manager, so it is the live one each
                time rather than whichever existed when the bot started.
        """
        self.default_manager = default_manager
    
    def _is_port_available(self, port: int) -> bool:
        """
        Check if a port is available by trying to bind to it
        
        Args:
            port: Port number to check
            
        Returns:
            True if port is available, False otherwise
        """
        # Check TCP
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', port))
                s.close()
        except OSError:
            return False
        
        # Check UDP
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(('', port))
                s.close()
        except OSError:
            return False
        
        return True

    def _is_this_machine(self, manager) -> bool:
        """
        Whether `manager` is the host the bot runs on: the only one a bind
        made here says anything about.

        The bind happens in the bot's own network, wherever the bindings were
        read from. On a remote host it answered about the wrong machine:
        "free" on the NAS for a port a service of the NAS itself holds, "used
        by the system" for one that is only taken here.
        """
        if manager is None:
            return True
        return getattr(manager, 'host_id', None) == host_registry.local_host_id()

    def _bound_port_ranges(self, container) -> List[Tuple[int, int]]:
        """
        The host ports a container's PortBindings hold, as (first, last).

        A binding that does not read as a port is skipped on its own. Skipping
        the container instead, as it used to be, hid every other port it had.
        """
        ranges = []
        port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings') or {}
        for host_bindings in port_bindings.values():
            for host_binding in host_bindings or []:
                host_port = host_binding.get('HostPort', '')
                if not host_port:
                    continue
                try:
                    ranges.append(_host_port_range(host_port))
                except ValueError:
                    continue
        return ranges
    
    def _get_bridge_network_ports(self, container) -> List[str]:
        """
        Get port bindings from a container with bridge/default network
        
        Args:
            container: Docker container object
            
        Returns:
            List of ports with protocol (e.g., ["8080/tcp", "5432/tcp"])
        """
        ports_with_proto = []
        
        port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
        if port_bindings:
            for container_port, host_bindings in port_bindings.items():
                if host_bindings:
                    for host_binding in host_bindings:
                        host_port = host_binding.get('HostPort', '')
                        if host_port:
                            # Extract protocol from container_port (e.g., "80/tcp" -> "tcp")
                            protocol = container_port.split('/')[-1] if '/' in container_port else 'tcp'
                            ports_with_proto.append(f"{host_port}/{protocol}")
        
        return ports_with_proto
    
    def get_container_ports(self, container) -> Tuple[List[str], bool]:
        """
        Get all ports used by a container

        Args:
            container: Docker container object

        Returns:
            Tuple of (list of ports with protocol, is_host_network)
        """
        network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')

        if network_mode == 'host':
            # Host network shares the host's network namespace, so listing
            # listening ports inside the container would expose every port
            # bound on the host. Skip enumeration and just signal host mode.
            return ([], True)
        else:
            ports = self._get_bridge_network_ports(container)
            return (ports, False)

    def check_port_availability(self, port_number: int, manager=None) -> Tuple[bool, str, Optional[str]]:
        """
        Check if a specific port is available

        Args:
            port_number: Port number to check

        Returns:
            Tuple of (is_available, message_key, container_name)
            - is_available: True if port is available
            - message_key: Translation key for the message
            - container_name: Name of container using the port (if any)

        `manager` says which host to ask. A port is taken or free on one
        machine and says nothing about any other, so answering from the local
        host while the user is looking at a remote one is simply a wrong
        answer. Defaults to the host the bot runs on.
        """
        # Inspected, not just listed: the bindings are HostConfig.PortBindings,
        # which only the inspect carries, and a stopped container holds its
        # port nowhere else.
        containers = (manager or self.default_manager()).list_containers(inspect=True)

        for container in containers:
            try:
                network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')

                if network_mode == 'host':
                    # For host network containers, check ports by executing commands inside
                    if container.status in ['running', 'restarting']:
                        try:
                            # Try ss first
                            result = container.exec_run(f"sh -c 'ss -tuln | grep \":{port_number} \"'", demux=False)
                            if result.exit_code == 0 and result.output:
                                return (False, "ports_used_by_container", container.name)

                            # If ss failed, try netstat
                            result = container.exec_run(f"sh -c 'netstat -tuln | grep \":{port_number} \"'", demux=False)
                            if result.exit_code == 0 and result.output:
                                return (False, "ports_used_by_container", container.name)
                        except Exception:
                            pass
                    continue

                # For bridge/default network, check PortBindings
                for first, last in self._bound_port_ranges(container):
                    if first <= port_number <= last:
                        return (False, "ports_used_by_container", container.name)
            except Exception:
                continue

        # No container has it. On another machine that is all there is to go
        # on: what else is listening there cannot be asked from here.
        if not self._is_this_machine(manager):
            return (True, "ports_available", None)

        # Check if port is available at system level
        if self._is_port_available(port_number):
            return (True, "ports_available", None)
        else:
            return (False, "ports_used_by_system", None)

    def get_random_available_port(self, min_port: int = 5000, max_port: int = 60000, max_attempts: int = 100, manager=None) -> Optional[int]:
        """
        Generate a random available port

        Args:
            min_port: Minimum port number (default: 5000)
            max_port: Maximum port number (default: 60000)
            max_attempts: Maximum number of attempts (default: 100)

        Returns:
            Available port number or None if no port found
        """
        # Get all ports used by containers, on the host being asked about:
        # a port free here says nothing about any other machine. Inspected,
        # because the bindings are only in the inspect.
        containers = (manager or self.default_manager()).list_containers(inspect=True)
        used_ranges = []

        for container in containers:
            try:
                # Skip containers with host network (can't reliably detect all ports)
                network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
                if network_mode == 'host':
                    continue

                # Get port bindings
                used_ranges.extend(self._bound_port_ranges(container))
            except Exception:
                continue

        # Same as check_port_availability: a bind only says anything about the
        # bot's own host, and on any other the bindings have the last word.
        this_machine = self._is_this_machine(manager)

        # Try to find an available port
        for _ in range(max_attempts):
            port = random.randint(min_port, max_port)

            # Skip if port is used by a container
            if any(first <= port <= last for first, last in used_ranges):
                continue

            # Check if port is available at system level
            if not this_machine or self._is_port_available(port):
                return port

        # If no port found after max_attempts, return None
        return None

