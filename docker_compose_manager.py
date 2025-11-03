#!/usr/bin/env python3
"""
Docker Compose Manager
Gestión de stacks de Docker Compose para docker-controller-bot
"""
import os
import subprocess
import yaml
import docker
from fnmatch import fnmatch
from typing import Dict, List, Optional

# Import de config para labels y variables
try:
    from config import (
        COMPOSE_STACKS_FORCE_RECREATE,
        LABEL_STACK_NO_FORCE_RECREATE,
        COMPOSE_FILE_PATTERNS,
        CONTAINER_NAME
    )
except ImportError:
    # Defaults si no se puede importar config
    COMPOSE_STACKS_FORCE_RECREATE = True
    LABEL_STACK_NO_FORCE_RECREATE = "DCB-Stack-No-Force-Recreate"
    COMPOSE_FILE_PATTERNS = ["*compose*.yml", "*compose*.yaml"]
    CONTAINER_NAME = None


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

    def _extract_stack_name_from_filename(self, filename: str) -> str:
        """
        Extrae el nombre del stack del nombre de archivo.

        Ejemplos:
        - docker-compose-pihole.yml -> pihole
        - docker-compose.yml -> docker-compose
        - compose-redis.yml -> redis
        - my-compose-grafana.yaml -> grafana
        - stack-compose-prod.yml -> prod

        Args:
            filename: Nombre del archivo

        Returns:
            Nombre extraído del stack
        """
        # Remover extensiones
        name = filename
        for ext in ['.yml', '.yaml']:
            if name.endswith(ext):
                name = name[:-len(ext)]
                break

        # Casos especiales: si el nombre termina en -compose o empieza con compose-
        # my-compose-grafana -> grafana
        # compose-redis -> redis
        if '-compose-' in name:
            # Hay texto antes y después de "compose"
            parts = name.split('-compose-')
            # Preferir la parte después de compose
            if len(parts) > 1 and parts[1]:
                return parts[1]
            # Si no hay nada después, usar lo que hay antes
            if parts[0]:
                return parts[0]

        # Casos especiales: archivos base sin sufijo (compose.yml, docker-compose.yml)
        if name in ['compose', 'docker-compose']:
            return name

        # Remover prefijos comunes (orden importante: más específicos primero)
        prefixes = ['docker-compose-', 'compose-']
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break

        # Remover sufijos comunes
        suffixes = ['-compose', '-docker-compose']
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break

        return name if name else 'stack'

    def _find_compose_file(self, directory: str) -> Optional[str]:
        """
        Busca un archivo compose en un directorio usando los patrones configurados.

        Args:
            directory: Directorio donde buscar

        Returns:
            Ruta al archivo encontrado o None
        """
        if not os.path.exists(directory):
            return None

        try:
            # Si es un archivo directo, verificar si coincide con algún patrón
            if os.path.isfile(directory):
                filename = os.path.basename(directory)
                for pattern in COMPOSE_FILE_PATTERNS:
                    if fnmatch(filename, pattern):
                        return directory
                return None

            # Si es directorio, buscar archivos que coincidan con los patrones
            for item in os.listdir(directory):
                for pattern in COMPOSE_FILE_PATTERNS:
                    if fnmatch(item, pattern):
                        candidate = os.path.join(directory, item)
                        if os.path.isfile(candidate):
                            return candidate

        except Exception:
            pass

        return None

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

                # Opción 1: Subdirectorios con archivos compose
                if os.path.isdir(stack_path):
                    compose_file = self._find_compose_file(stack_path)

                    if compose_file and self.validate_compose_file(compose_file):
                        stack_info = self._parse_compose_file(compose_file)
                        if stack_info:
                            # Normalizar nombre a lowercase para evitar duplicados (Docker Compose usa lowercase)
                            stack_info['name'] = item.lower()
                            stack_info['path'] = stack_path
                            stack_info['compose_file'] = compose_file
                            stack_info['source'] = 'directory'
                            stacks.append(stack_info)

                # Opción 2: Archivos compose en el directorio raíz
                elif os.path.isfile(stack_path):
                    # Verificar si coincide con algún patrón
                    matches_pattern = False
                    for pattern in COMPOSE_FILE_PATTERNS:
                        if fnmatch(item, pattern):
                            matches_pattern = True
                            break

                    if matches_pattern and self.validate_compose_file(stack_path):
                        stack_info = self._parse_compose_file(stack_path)
                        if stack_info:
                            # Extraer nombre del stack del nombre de archivo
                            name = self._extract_stack_name_from_filename(item)

                            # Normalizar nombre a lowercase para evitar duplicados (Docker Compose usa lowercase)
                            stack_info['name'] = (name if name else 'stack').lower()
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

        # Solo añadir stacks que tienen archivo compose en el directorio
        # NO mostrar stacks que solo están corriendo sin archivo compose
        for name, stack in dir_stacks.items():
            if name in running_stacks:
                stack['running'] = True
                stack['containers'] = running_stacks[name]
            else:
                stack['running'] = False
                stack['containers'] = []
            all_stacks.append(stack)

        return sorted(all_stacks, key=lambda x: x['name'])

    def get_stack_info(self, stack_name: str) -> Optional[Dict]:
        """
        Obtiene información detallada de un stack específico.

        Args:
            stack_name: Nombre del stack

        Returns:
            Diccionario con información del stack o None si no existe
        """
        # Buscar en directorio como subdirectorio
        stack_path = os.path.join(self.stacks_dir, stack_name)
        compose_file = None

        if os.path.isdir(stack_path):
            compose_file = self._find_compose_file(stack_path)

        # Si no se encuentra como subdirectorio, buscar en archivos flat del directorio
        if not compose_file:
            # Buscar archivos que coincidan con los patrones y contengan el nombre del stack
            try:
                for item in os.listdir(self.stacks_dir):
                    item_path = os.path.join(self.stacks_dir, item)
                    if os.path.isfile(item_path):
                        # Verificar si coincide con patrones
                        for pattern in COMPOSE_FILE_PATTERNS:
                            if fnmatch(item, pattern):
                                # Extraer nombre del stack del archivo
                                extracted_name = self._extract_stack_name_from_filename(item)
                                if extracted_name == stack_name:
                                    compose_file = item_path
                                    stack_path = item_path
                                    break
                        if compose_file:
                            break
            except Exception:
                pass

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
        # Obtener información del stack (soporta subdirectorios y archivos flat)
        stack_info = self.get_stack_info(stack_name)

        if not stack_info or 'compose_file' not in stack_info:
            return {
                'success': False,
                'stdout': '',
                'stderr': f'Stack {stack_name} no encontrado'
            }

        compose_file = stack_info['compose_file']

        # Determinar el directorio de trabajo correcto
        # Si path es un archivo (flat structure), usar su directorio
        # Si path es un directorio (subdirectory structure), usarlo directamente
        stack_path = stack_info.get('path', os.path.dirname(compose_file))
        if os.path.isfile(stack_path):
            working_dir = os.path.dirname(stack_path)
        else:
            working_dir = stack_path

        try:
            # Incluir -p <project_name> para asegurar consistencia del nombre del stack
            cmd = ['docker', 'compose', '-f', compose_file, '-p', stack_name] + command

            result = subprocess.run(
                cmd,
                cwd=working_dir,
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
        Inicia un stack (docker compose up -d --remove-orphans)

        Args:
            stack_name: Nombre del stack

        Returns:
            Resultado de la operación
        """
        return self._run_compose_command(stack_name, ['up', '-d', '--remove-orphans'])

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
        up_command = ['up', '-d', '--remove-orphans']
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

    def check_stack_updates(self, stack_name: str, update_checker_func=None) -> Dict:
        """
        Verifica si un stack tiene actualizaciones disponibles.

        Args:
            stack_name: Nombre del stack
            update_checker_func: Función para verificar si un contenedor tiene actualizaciones.
                                 Debe aceptar un contenedor y devolver bool.
                                 Si es None, se verifica solo si los contenedores están corriendo.

        Returns:
            Diccionario con información de actualizaciones:
            {
                'stack_name': str,
                'has_updates': bool,
                'total_services': int,
                'services_with_updates': [
                    {
                        'service_name': str,
                        'container_name': str,
                        'container_id': str,
                        'image': str
                    }
                ],
                'running_services': int
            }
        """
        result = {
            'stack_name': stack_name,
            'has_updates': False,
            'total_services': 0,
            'services_with_updates': [],
            'running_services': 0
        }

        try:
            # Obtener información del stack
            stack_info = self.get_stack_info(stack_name)
            if not stack_info:
                return result

            result['total_services'] = len(stack_info.get('services', []))

            # Verificar si hay contenedores corriendo
            if not stack_info.get('running', False):
                return result

            containers = stack_info.get('containers', [])
            result['running_services'] = len(containers)

            # Si no hay función de verificación, solo contamos contenedores corriendo
            if update_checker_func is None:
                return result

            # Verificar cada contenedor del stack
            for container_info in containers:
                try:
                    container = self.client.containers.get(container_info['id'])

                    # Usar la función de verificación provista
                    if update_checker_func(container):
                        result['services_with_updates'].append({
                            'service_name': container_info.get('service', 'unknown'),
                            'container_name': container_info['name'],
                            'container_id': container_info['id'],
                            'image': container_info.get('image', 'unknown')
                        })
                except Exception as e:
                    print(f"Error verificando actualizaciones para {container_info['name']}: {e}")
                    continue

            result['has_updates'] = len(result['services_with_updates']) > 0

        except Exception as e:
            print(f"Error verificando actualizaciones del stack {stack_name}: {e}")

        return result

    def get_all_stacks_with_updates(self, update_checker_func=None) -> List[Dict]:
        """
        Obtiene lista de todos los stacks que tienen actualizaciones disponibles.

        Args:
            update_checker_func: Función para verificar si un contenedor tiene actualizaciones

        Returns:
            Lista de diccionarios con información de stacks que tienen actualizaciones
        """
        stacks_with_updates = []

        try:
            all_stacks = self.list_all_stacks()

            for stack in all_stacks:
                # Solo verificar stacks que están corriendo
                if not stack.get('running', False):
                    continue

                update_info = self.check_stack_updates(
                    stack['name'],
                    update_checker_func
                )

                if update_info['has_updates']:
                    stacks_with_updates.append(update_info)

        except Exception as e:
            print(f"Error obteniendo stacks con actualizaciones: {e}")

        return stacks_with_updates
