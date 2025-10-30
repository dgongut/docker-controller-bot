#!/usr/bin/env python3
"""
Docker Compose Manager
Gestión de stacks de Docker Compose para docker-controller-bot
"""
import os
import subprocess
import yaml
import docker
from typing import Dict, List, Optional

# Import de config para labels y variables
try:
    from config import (
        COMPOSE_STACKS_FORCE_RECREATE,
        LABEL_STACK_NO_FORCE_RECREATE
    )
except ImportError:
    # Defaults si no se puede importar config
    COMPOSE_STACKS_FORCE_RECREATE = True
    LABEL_STACK_NO_FORCE_RECREATE = "DCB-Stack-No-Force-Recreate"


class DockerComposeManager:
    """
    Gestor de stacks de Docker Compose.

    Soporta dos modos:
    1. Detección de stacks corriendo por labels (com.docker.compose.project)
    2. Escaneo de directorio con archivos docker-compose.yml
    """

    def __init__(self, stacks_dir: str = "/srv/stacks"):
        """
        Inicializa el gestor de stacks.

        Args:
            stacks_dir: Directorio que contiene los stacks (subdirectorios con docker-compose.yml)
        """
        self.stacks_dir = stacks_dir
        self.client = docker.from_env()

    def scan_stacks_directory(self) -> List[Dict]:
        """
        Escanea el directorio de stacks buscando docker-compose.yml

        Soporta dos estructuras:

        Opción 1 - Subdirectorios (recomendado):
        /srv/stacks/
        ├── pihole/
        │   └── docker-compose.yml
        ├── nginx/
        │   └── docker-compose.yml

        Opción 2 - Archivos en mismo directorio:
        /srv/stacks/
        ├── docker-compose-pihole.yml
        ├── docker-compose-nginx.yml
        └── docker-compose-grafana.yml

        Returns:
            Lista de diccionarios con info de cada stack encontrado
        """
        stacks = []

        if not os.path.exists(self.stacks_dir):
            return stacks

        try:
            for item in os.listdir(self.stacks_dir):
                stack_path = os.path.join(self.stacks_dir, item)

                # Opción 1: Subdirectorios con docker-compose.yml
                if os.path.isdir(stack_path):
                    compose_file = None
                    for filename in ['docker-compose.yml', 'docker-compose.yaml']:
                        candidate = os.path.join(stack_path, filename)
                        if os.path.isfile(candidate):
                            compose_file = candidate
                            break

                    if compose_file and self.validate_compose_file(compose_file):
                        stack_info = self._parse_compose_file(compose_file)
                        if stack_info:
                            stack_info['name'] = item
                            stack_info['path'] = stack_path
                            stack_info['compose_file'] = compose_file
                            stack_info['source'] = 'directory'
                            stacks.append(stack_info)

                # Opción 2: Archivos docker-compose-*.yml en el directorio raíz
                elif os.path.isfile(stack_path):
                    # Buscar archivos que empiecen con docker-compose y terminen en .yml/.yaml
                    if (item.startswith('docker-compose') and
                        (item.endswith('.yml') or item.endswith('.yaml'))):

                        if self.validate_compose_file(stack_path):
                            stack_info = self._parse_compose_file(stack_path)
                            if stack_info:
                                # Extraer nombre del stack del nombre de archivo
                                # docker-compose-pihole.yml -> pihole
                                # docker-compose.yml -> compose (fallback)
                                name = item.replace('docker-compose-', '').replace('docker-compose', 'compose')
                                name = name.replace('.yml', '').replace('.yaml', '')

                                stack_info['name'] = name if name else 'compose'
                                stack_info['path'] = self.stacks_dir
                                stack_info['compose_file'] = stack_path
                                stack_info['source'] = 'file'
                                stacks.append(stack_info)

        except Exception as e:
            print(f"Error escaneando directorio de stacks: {e}")

        return stacks

    def detect_running_stacks(self) -> Dict[str, List]:
        """
        Detecta stacks corriendo basándose en labels de Docker Compose.

        Docker Compose añade estos labels automáticamente:
        - com.docker.compose.project: nombre del stack/proyecto
        - com.docker.compose.service: nombre del servicio

        Returns:
            Diccionario donde la key es el nombre del stack y el value es lista de contenedores
        """
        stacks = {}

        try:
            containers = self.client.containers.list(all=True)

            for container in containers:
                project_name = container.labels.get('com.docker.compose.project')
                service_name = container.labels.get('com.docker.compose.service')

                if project_name:
                    if project_name not in stacks:
                        stacks[project_name] = []

                    container_info = {
                        'name': container.name,
                        'service': service_name,
                        'status': container.status,
                        'image': container.attrs['Config']['Image'],
                        'id': container.id[:12]
                    }
                    stacks[project_name].append(container_info)

        except Exception as e:
            print(f"Error detectando stacks corriendo: {e}")

        return stacks

    def list_all_stacks(self) -> List[Dict]:
        """
        Lista todos los stacks: tanto del directorio como los corriendo.
        Combina información de ambas fuentes.

        Returns:
            Lista unificada de stacks con toda la información disponible
        """
        # Stacks del directorio
        dir_stacks = {s['name']: s for s in self.scan_stacks_directory()}

        # Stacks corriendo
        running_stacks = self.detect_running_stacks()

        # Combinar información
        all_stacks = []

        # Añadir stacks del directorio
        for name, stack in dir_stacks.items():
            if name in running_stacks:
                stack['running'] = True
                stack['containers'] = running_stacks[name]
            else:
                stack['running'] = False
                stack['containers'] = []
            all_stacks.append(stack)

        # Añadir stacks corriendo que no están en el directorio
        for name, containers in running_stacks.items():
            if name not in dir_stacks:
                all_stacks.append({
                    'name': name,
                    'source': 'running',
                    'running': True,
                    'containers': containers,
                    'services': [{'name': c['service']} for c in containers]
                })

        return sorted(all_stacks, key=lambda x: x['name'])

    def get_stack_info(self, stack_name: str) -> Optional[Dict]:
        """
        Obtiene información detallada de un stack específico.

        Args:
            stack_name: Nombre del stack

        Returns:
            Diccionario con información del stack o None si no existe
        """
        # Buscar en directorio
        stack_path = os.path.join(self.stacks_dir, stack_name)
        compose_file = None

        if os.path.isdir(stack_path):
            for filename in ['docker-compose.yml', 'docker-compose.yaml']:
                candidate = os.path.join(stack_path, filename)
                if os.path.isfile(candidate):
                    compose_file = candidate
                    break

        if not compose_file:
            # No está en directorio, buscar en corriendo
            running_stacks = self.detect_running_stacks()
            if stack_name in running_stacks:
                return {
                    'name': stack_name,
                    'source': 'running',
                    'running': True,
                    'containers': running_stacks[stack_name],
                    'services': [{'name': c['service']} for c in running_stacks[stack_name]]
                }
            return None

        # Parsear archivo compose
        stack_info = self._parse_compose_file(compose_file)
        if not stack_info:
            return None

        stack_info['name'] = stack_name
        stack_info['path'] = stack_path
        stack_info['compose_file'] = compose_file
        stack_info['source'] = 'directory'

        # Añadir info de contenedores corriendo
        running_stacks = self.detect_running_stacks()
        if stack_name in running_stacks:
            stack_info['running'] = True
            stack_info['containers'] = running_stacks[stack_name]
        else:
            stack_info['running'] = False
            stack_info['containers'] = []

        return stack_info

    def validate_compose_file(self, compose_file: str) -> bool:
        """
        Valida que un archivo docker-compose.yml sea válido.

        Args:
            compose_file: Ruta al archivo docker-compose.yml

        Returns:
            True si es válido, False si no
        """
        try:
            with open(compose_file, 'r') as f:
                data = yaml.safe_load(f)
                # Validación básica
                if not isinstance(data, dict):
                    return False
                if 'services' not in data:
                    return False
                return True
        except Exception:
            return False

    def _parse_compose_file(self, compose_file: str) -> Optional[Dict]:
        """
        Parsea un archivo docker-compose.yml y extrae información.

        Args:
            compose_file: Ruta al archivo

        Returns:
            Diccionario con información del stack
        """
        try:
            with open(compose_file, 'r') as f:
                data = yaml.safe_load(f)

            services = []
            if 'services' in data:
                for service_name, service_config in data['services'].items():
                    services.append({
                        'name': service_name,
                        'image': service_config.get('image', 'N/A')
                    })

            return {
                'version': data.get('version', 'N/A'),
                'services': services,
                'service_count': len(services)
            }

        except Exception as e:
            print(f"Error parseando compose file {compose_file}: {e}")
            return None

    def _run_compose_command(self, stack_name: str, command: List[str]) -> Dict:
        """
        Ejecuta un comando de docker compose en un stack.

        Args:
            stack_name: Nombre del stack
            command: Lista con el comando (ej: ['up', '-d'])

        Returns:
            Diccionario con resultado: {'success': bool, 'stdout': str, 'stderr': str}
        """
        stack_path = os.path.join(self.stacks_dir, stack_name)
        compose_file = None

        for filename in ['docker-compose.yml', 'docker-compose.yaml']:
            candidate = os.path.join(stack_path, filename)
            if os.path.isfile(candidate):
                compose_file = candidate
                break

        if not compose_file:
            return {
                'success': False,
                'stdout': '',
                'stderr': f'Stack {stack_name} no encontrado'
            }

        try:
            cmd = ['docker', 'compose', '-f', compose_file] + command

            result = subprocess.run(
                cmd,
                cwd=stack_path,
                capture_output=True,
                text=True,
                timeout=300  # 5 minutos timeout
            )

            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr
            }

        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'stdout': '',
                'stderr': 'Timeout ejecutando comando'
            }
        except Exception as e:
            return {
                'success': False,
                'stdout': '',
                'stderr': str(e)
            }

    def stack_start(self, stack_name: str) -> Dict:
        """
        Inicia un stack (docker compose up -d)

        Args:
            stack_name: Nombre del stack

        Returns:
            Resultado de la operación
        """
        return self._run_compose_command(stack_name, ['up', '-d'])

    def stack_stop(self, stack_name: str) -> Dict:
        """
        Detiene un stack (docker compose down)

        Args:
            stack_name: Nombre del stack

        Returns:
            Resultado de la operación
        """
        return self._run_compose_command(stack_name, ['down'])

    def stack_restart(self, stack_name: str) -> Dict:
        """
        Reinicia un stack (down + up)

        Args:
            stack_name: Nombre del stack

        Returns:
            Resultado de la operación
        """
        # Primero stop
        stop_result = self.stack_stop(stack_name)
        if not stop_result['success']:
            return stop_result

        # Luego start
        return self.stack_start(stack_name)

    def stack_update(self, stack_name: str, force_recreate: Optional[bool] = None) -> Dict:
        """
        Actualiza un stack (pull + up)

        El comportamiento de --force-recreate se determina en este orden:
        1. Si force_recreate se especifica explícitamente, se usa ese valor
        2. Si el stack tiene label DCB-Stack-No-Force-Recreate, NO se fuerza
        3. Si no, se usa COMPOSE_STACKS_FORCE_RECREATE (variable global, default: True)

        Args:
            stack_name: Nombre del stack
            force_recreate: Si especificado, fuerza o no el --force-recreate.
                          Si None, se determina según labels/config.

        Returns:
            Resultado de la operación
        """
        # Primero pull
        pull_result = self._run_compose_command(stack_name, ['pull'])
        if not pull_result['success']:
            return pull_result

        # Determinar si usar --force-recreate
        should_force_recreate = self._should_force_recreate(stack_name, force_recreate)

        # Construir comando
        up_command = ['up', '-d']
        if should_force_recreate:
            up_command.append('--force-recreate')

        # Ejecutar up
        return self._run_compose_command(stack_name, up_command)

    def _should_force_recreate(self, stack_name: str, explicit_value: Optional[bool] = None) -> bool:
        """
        Determina si se debe usar --force-recreate para un stack.

        Prioridad:
        1. Valor explícito pasado como parámetro
        2. Label DCB-Stack-No-Force-Recreate en algún contenedor del stack
        3. Variable global COMPOSE_STACKS_FORCE_RECREATE (default: True)

        Args:
            stack_name: Nombre del stack
            explicit_value: Si no es None, se retorna este valor directamente

        Returns:
            True si se debe usar --force-recreate, False si no
        """
        # 1. Valor explícito tiene máxima prioridad
        if explicit_value is not None:
            return explicit_value

        # 2. Buscar label para deshabilitar en contenedores del stack
        try:
            running_stacks = self.detect_running_stacks()
            if stack_name in running_stacks:
                containers = running_stacks[stack_name]

                # Buscar label para NO forzar recreación
                for container_info in containers:
                    # Obtener contenedor completo para acceder a labels
                    try:
                        container = self.client.containers.get(container_info['id'])
                        labels = container.labels

                        # Label para NO forzar
                        if LABEL_STACK_NO_FORCE_RECREATE in labels:
                            return False

                    except Exception:
                        continue

        except Exception:
            pass

        # 3. Valor por defecto de la configuración global
        return COMPOSE_STACKS_FORCE_RECREATE

    def stack_logs(self, stack_name: str, tail: int = 100) -> Dict:
        """
        Obtiene los logs de un stack

        Args:
            stack_name: Nombre del stack
            tail: Número de líneas a mostrar

        Returns:
            Resultado con logs
        """
        return self._run_compose_command(stack_name, ['logs', '--tail', str(tail)])
