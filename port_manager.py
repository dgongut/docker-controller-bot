#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Port Manager Module
Handles all port-related operations for Docker Controller Bot
"""

import socket
import random
from typing import Tuple, List, Dict, Set, Optional


class PortManager:
    """Manages port operations for Docker containers"""
    
    def __init__(self, docker_manager):
        """
        Initialize PortManager
        
        Args:
            docker_manager: Instance of DockerManager to interact with containers
        """
        self.docker_manager = docker_manager
    
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

    def check_port_availability(self, port_number: int) -> Tuple[bool, str, Optional[str]]:
        """
        Check if a specific port is available

        Args:
            port_number: Port number to check

        Returns:
            Tuple of (is_available, message_key, container_name)
            - is_available: True if port is available
            - message_key: Translation key for the message
            - container_name: Name of container using the port (if any)
        """
        containers = self.docker_manager.list_containers()

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
                port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
                if port_bindings:
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            for host_binding in host_bindings:
                                host_port = host_binding.get('HostPort', '')
                                if host_port and int(host_port) == port_number:
                                    return (False, "ports_used_by_container", container.name)
            except Exception:
                continue

        # Check if port is available at system level
        if self._is_port_available(port_number):
            return (True, "ports_available", None)
        else:
            return (False, "ports_used_by_system", None)

    def get_random_available_port(self, min_port: int = 5000, max_port: int = 60000, max_attempts: int = 100) -> Optional[int]:
        """
        Generate a random available port

        Args:
            min_port: Minimum port number (default: 5000)
            max_port: Maximum port number (default: 60000)
            max_attempts: Maximum number of attempts (default: 100)

        Returns:
            Available port number or None if no port found
        """
        # Get all ports used by containers
        containers = self.docker_manager.list_containers()
        used_ports = set()

        for container in containers:
            try:
                # Skip containers with host network (can't reliably detect all ports)
                network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
                if network_mode == 'host':
                    continue

                # Get port bindings
                port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
                if port_bindings:
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            for host_binding in host_bindings:
                                host_port = host_binding.get('HostPort', '')
                                if host_port:
                                    try:
                                        used_ports.add(int(host_port))
                                    except ValueError:
                                        pass
            except Exception:
                continue

        # Try to find an available port
        for _ in range(max_attempts):
            port = random.randint(min_port, max_port)

            # Skip if port is used by a container
            if port in used_ports:
                continue

            # Check if port is available at system level
            if self._is_port_available(port):
                return port

        # If no port found after max_attempts, return None
        return None

