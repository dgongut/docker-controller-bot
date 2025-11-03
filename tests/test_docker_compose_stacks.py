#!/usr/bin/env python3
"""
Tests para la funcionalidad de Docker Compose Stacks
"""
import os
import sys
import tempfile
# Añadir el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import Mock, MagicMock, patch, mock_open
import yaml

# Mock de las variables de entorno necesarias
os.environ['TELEGRAM_TOKEN'] = 'test_token'
os.environ['TELEGRAM_ADMIN'] = '12345'
os.environ['CONTAINER_NAME'] = 'test-bot'
os.environ['TZ'] = 'UTC'
os.environ['CHECK_UPDATES'] = '0'
os.environ['CHECK_UPDATE_EVERY_HOURS'] = '4'
os.environ['CHECK_UPDATE_STOPPED_CONTAINERS'] = '0'
os.environ['LANGUAGE'] = 'EN'
os.environ['EXTENDED_MESSAGES'] = '0'
os.environ['BUTTON_COLUMNS'] = '2'
os.environ['COMPOSE_STACKS_ENABLED'] = '1'
os.environ['COMPOSE_STACKS_DIR'] = '/tmp/test_stacks'

class TestDockerComposeManager(unittest.TestCase):
    """Tests para la clase DockerComposeManager"""

    @patch('docker.from_env')
    def setUp(self, mock_docker):
        """Setup para cada test"""
        self.mock_docker_client = MagicMock()
        mock_docker.return_value = self.mock_docker_client

        # Crear directorio temporal para stacks
        self.test_stacks_dir = tempfile.mkdtemp()
        os.environ['COMPOSE_STACKS_DIR'] = self.test_stacks_dir

    def tearDown(self):
        """Cleanup después de cada test"""
        import shutil
        if os.path.exists(self.test_stacks_dir):
            shutil.rmtree(self.test_stacks_dir)

    def create_test_stack(self, stack_name, services):
        """Helper para crear un stack de test"""
        stack_dir = os.path.join(self.test_stacks_dir, stack_name)
        os.makedirs(stack_dir, exist_ok=True)

        compose_content = {
            'version': '3',
            'services': services
        }

        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        with open(compose_file, 'w') as f:
            yaml.dump(compose_content, f)

        return compose_file

    def test_01_scan_stacks_directory_empty(self):
        """Test: Escanear directorio vacío de stacks"""
        from docker_compose_manager import DockerComposeManager

        manager = DockerComposeManager(self.test_stacks_dir)
        stacks = manager.scan_stacks_directory()

        self.assertEqual(len(stacks), 0, "Directorio vacío debe retornar 0 stacks")
        print("✓ Test 01: Directorio vacío OK")

    def test_02_scan_stacks_directory_with_stacks(self):
        """Test: Escanear directorio con stacks"""
        from docker_compose_manager import DockerComposeManager

        # Crear stacks de prueba
        self.create_test_stack('pihole', {
            'pihole': {
                'image': 'pihole/pihole:latest',
                'container_name': 'pihole'
            }
        })

        self.create_test_stack('nginx', {
            'nginx': {
                'image': 'nginx:latest',
                'container_name': 'nginx'
            }
        })

        manager = DockerComposeManager(self.test_stacks_dir)
        stacks = manager.scan_stacks_directory()

        self.assertEqual(len(stacks), 2, "Debe detectar 2 stacks")
        stack_names = [s['name'] for s in stacks]
        self.assertIn('pihole', stack_names)
        self.assertIn('nginx', stack_names)
        print("✓ Test 02: Detección de stacks en directorio OK")

    @patch('docker_compose_manager.docker.from_env')
    def test_03_detect_running_stacks_by_labels(self, mock_docker_from_env):
        """Test: Detectar stacks corriendo por labels de compose"""
        from docker_compose_manager import DockerComposeManager

        # Mock de contenedores con labels de compose
        mock_container_1 = MagicMock()
        mock_container_1.name = 'pihole'
        mock_container_1.status = 'running'
        mock_container_1.labels = {
            'com.docker.compose.project': 'pihole',
            'com.docker.compose.service': 'pihole'
        }
        mock_container_1.attrs = {'Config': {'Image': 'pihole/pihole:latest'}}
        mock_container_1.id = '1234567890ab'

        mock_container_2 = MagicMock()
        mock_container_2.name = 'pihole_db'
        mock_container_2.status = 'running'
        mock_container_2.labels = {
            'com.docker.compose.project': 'pihole',
            'com.docker.compose.service': 'db'
        }
        mock_container_2.attrs = {'Config': {'Image': 'postgres:14'}}
        mock_container_2.id = 'abcdef123456'

        # Mock específico para este test
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container_1, mock_container_2]
        mock_docker_from_env.return_value = mock_client

        manager = DockerComposeManager(self.test_stacks_dir)
        stacks = manager.detect_running_stacks()

        self.assertEqual(len(stacks), 1, "Debe detectar 1 stack")
        self.assertIn('pihole', stacks)
        self.assertEqual(len(stacks['pihole']), 2, "Stack pihole debe tener 2 servicios")
        print("✓ Test 03: Detección de stacks corriendo por labels OK")

    def test_04_get_stack_info(self):
        """Test: Obtener información de un stack"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'web': {
                'image': 'nginx:latest',
                'container_name': 'test-web'
            },
            'db': {
                'image': 'postgres:14',
                'container_name': 'test-db'
            }
        })

        manager = DockerComposeManager(self.test_stacks_dir)
        stack_info = manager.get_stack_info('test-stack')

        self.assertIsNotNone(stack_info)
        self.assertEqual(stack_info['name'], 'test-stack')
        self.assertEqual(len(stack_info['services']), 2)
        print("✓ Test 04: Obtener info de stack OK")

    def test_05_validate_compose_file(self):
        """Test: Validar archivo docker-compose.yml"""
        from docker_compose_manager import DockerComposeManager

        manager = DockerComposeManager(self.test_stacks_dir)

        # Test con archivo válido
        valid_compose = self.create_test_stack('valid', {
            'app': {'image': 'nginx:latest'}
        })
        self.assertTrue(manager.validate_compose_file(valid_compose))

        # Test con archivo inválido
        invalid_file = os.path.join(self.test_stacks_dir, 'invalid.yml')
        with open(invalid_file, 'w') as f:
            f.write("invalid: yaml: content: [")
        self.assertFalse(manager.validate_compose_file(invalid_file))

        print("✓ Test 05: Validación de archivos compose OK")

    @patch('subprocess.run')
    def test_06_stack_start(self, mock_subprocess):
        """Test: Iniciar un stack"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        compose_file = self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Started", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        result = manager.stack_start('test-stack')

        self.assertTrue(result['success'])
        mock_subprocess.assert_called_once()

        # Verificar que se llamó con docker compose up
        call_args = mock_subprocess.call_args
        self.assertIn('docker', call_args[0][0])
        self.assertIn('compose', call_args[0][0])
        self.assertIn('up', call_args[0][0])

        print("✓ Test 06: Iniciar stack OK")

    @patch('subprocess.run')
    def test_07_stack_stop(self, mock_subprocess):
        """Test: Detener un stack"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Stopped", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        result = manager.stack_stop('test-stack')

        self.assertTrue(result['success'])
        mock_subprocess.assert_called_once()

        # Verificar que se llamó con docker compose down
        call_args = mock_subprocess.call_args
        self.assertIn('docker', call_args[0][0])
        self.assertIn('compose', call_args[0][0])
        self.assertIn('down', call_args[0][0])

        print("✓ Test 07: Detener stack OK")

    @patch('subprocess.run')
    def test_08_stack_restart(self, mock_subprocess):
        """Test: Reiniciar un stack"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Restarted", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        result = manager.stack_restart('test-stack')

        self.assertTrue(result['success'])
        # Debe llamar 2 veces: down y up
        self.assertEqual(mock_subprocess.call_count, 2)

        print("✓ Test 08: Reiniciar stack OK")

    @patch('subprocess.run')
    def test_09_stack_update(self, mock_subprocess):
        """Test: Actualizar un stack (pull + recreate)"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        result = manager.stack_update('test-stack')

        self.assertTrue(result['success'])
        # Debe llamar 2 veces: pull y up --force-recreate
        self.assertEqual(mock_subprocess.call_count, 2)

        # Verificar comandos
        calls = [call[0][0] for call in mock_subprocess.call_args_list]
        self.assertTrue(any('pull' in ' '.join(cmd) for cmd in calls))
        self.assertTrue(any('--force-recreate' in ' '.join(cmd) for cmd in calls))

        print("✓ Test 09: Actualizar stack OK")

    @patch('subprocess.run')
    def test_09b_stack_update_no_force_recreate(self, mock_subprocess):
        """Test: Actualizar un stack SIN --force-recreate"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        # Pasar force_recreate=False explícitamente
        result = manager.stack_update('test-stack', force_recreate=False)

        self.assertTrue(result['success'])
        # Debe llamar 2 veces: pull y up (sin --force-recreate)
        self.assertEqual(mock_subprocess.call_count, 2)

        # Verificar comandos
        calls = [call[0][0] for call in mock_subprocess.call_args_list]
        self.assertTrue(any('pull' in ' '.join(cmd) for cmd in calls))
        # NO debe tener --force-recreate
        self.assertFalse(any('--force-recreate' in ' '.join(cmd) for cmd in calls))

        print("✓ Test 09b: Actualizar stack sin force-recreate OK")

    @patch('docker_compose_manager.docker.from_env')
    @patch('subprocess.run')
    def test_09c_stack_update_with_label_no_force(self, mock_subprocess, mock_docker_from_env):
        """Test: Actualizar stack con label DCB-Stack-No-Force-Recreate"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        self.create_test_stack('test-stack', {
            'app': {'image': 'nginx:latest'}
        })

        # Mock contenedor con label de NO force recreate
        mock_container = MagicMock()
        mock_container.name = 'test-stack-app'
        mock_container.status = 'running'
        mock_container.labels = {
            'com.docker.compose.project': 'test-stack',
            'com.docker.compose.service': 'app',
            'DCB-Stack-No-Force-Recreate': ''  # Label presente
        }
        mock_container.attrs = {'Config': {'Image': 'nginx:latest'}}
        mock_container.id = 'abc123'

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container]
        mock_client.containers.get.return_value = mock_container
        mock_docker_from_env.return_value = mock_client

        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated", stderr="")

        manager = DockerComposeManager(self.test_stacks_dir)
        # No especificar force_recreate, debe detectar el label y NO usar --force-recreate
        result = manager.stack_update('test-stack')

        self.assertTrue(result['success'])

        # Verificar que NO se usó --force-recreate por el label
        calls = [call[0][0] for call in mock_subprocess.call_args_list]
        self.assertFalse(any('--force-recreate' in ' '.join(cmd) for cmd in calls))

        print("✓ Test 09c: Actualizar stack con label no-force OK")

    @patch('docker_compose_manager.docker.from_env')
    def test_10_list_all_stacks(self, mock_docker_from_env):
        """Test: Listar todos los stacks (directorio + corriendo)"""
        from docker_compose_manager import DockerComposeManager

        # Crear stacks en directorio
        self.create_test_stack('stack1', {'app': {'image': 'nginx:latest'}})
        self.create_test_stack('stack2', {'app': {'image': 'redis:latest'}})

        # Mock de stacks corriendo
        mock_container = MagicMock()
        mock_container.name = 'stack3-app'
        mock_container.status = 'running'
        mock_container.labels = {
            'com.docker.compose.project': 'stack3',
            'com.docker.compose.service': 'app'
        }
        mock_container.attrs = {'Config': {'Image': 'postgres:14'}}
        mock_container.id = 'xyz123456789'

        # Mock específico para este test
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container]
        mock_docker_from_env.return_value = mock_client

        manager = DockerComposeManager(self.test_stacks_dir)
        all_stacks = manager.list_all_stacks()

        self.assertGreaterEqual(len(all_stacks), 3, "Debe detectar al menos 3 stacks")
        stack_names = [s['name'] for s in all_stacks]
        self.assertIn('stack1', stack_names)
        self.assertIn('stack2', stack_names)
        self.assertIn('stack3', stack_names)

        print("✓ Test 10: Listar todos los stacks OK")


class TestStackIntegration(unittest.TestCase):
    """Tests de integración completos"""

    @patch('docker.from_env')
    def setUp(self, mock_docker):
        """Setup para tests de integración"""
        self.mock_docker_client = MagicMock()
        mock_docker.return_value = self.mock_docker_client

        self.test_stacks_dir = tempfile.mkdtemp()
        os.environ['COMPOSE_STACKS_DIR'] = self.test_stacks_dir

    def tearDown(self):
        """Cleanup"""
        import shutil
        if os.path.exists(self.test_stacks_dir):
            shutil.rmtree(self.test_stacks_dir)

    def test_11_pihole_stack_scenario(self):
        """Test: Escenario real con stack de Pihole"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de pihole (caso del issue)
        pihole_compose = {
            'pihole': {
                'image': 'pihole/pihole:latest',
                'container_name': 'pihole',
                'environment': [
                    'TZ=Europe/Madrid',
                    'WEBPASSWORD=admin'
                ],
                'volumes': [
                    './etc-pihole:/etc/pihole',
                    './etc-dnsmasq.d:/etc/dnsmasq.d'
                ],
                'ports': [
                    '53:53/tcp',
                    '53:53/udp',
                    '80:80/tcp'
                ],
                'restart': 'unless-stopped'
            }
        }

        stack_dir = os.path.join(self.test_stacks_dir, 'pihole')
        os.makedirs(stack_dir, exist_ok=True)
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        with open(compose_file, 'w') as f:
            yaml.dump({'version': '3', 'services': pihole_compose}, f)

        manager = DockerComposeManager(self.test_stacks_dir)
        stacks = manager.scan_stacks_directory()

        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0]['name'], 'pihole')
        # services es una lista de diccionarios con 'name' e 'image'
        service_names = [s['name'] for s in stacks[0]['services']]
        self.assertIn('pihole', service_names)

        print("✓ Test 11: Escenario Pihole OK")

    def test_12_multi_service_stack(self):
        """Test: Stack con múltiples servicios (web + db + redis)"""
        from docker_compose_manager import DockerComposeManager

        multi_compose = {
            'web': {
                'image': 'nginx:latest',
                'container_name': 'web',
                'depends_on': ['db', 'redis']
            },
            'db': {
                'image': 'postgres:14',
                'container_name': 'db'
            },
            'redis': {
                'image': 'redis:alpine',
                'container_name': 'redis'
            }
        }

        stack_dir = os.path.join(self.test_stacks_dir, 'webapp')
        os.makedirs(stack_dir, exist_ok=True)
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        with open(compose_file, 'w') as f:
            yaml.dump({'version': '3', 'services': multi_compose}, f)

        manager = DockerComposeManager(self.test_stacks_dir)
        stack_info = manager.get_stack_info('webapp')

        self.assertIsNotNone(stack_info)
        self.assertEqual(len(stack_info['services']), 3)
        service_names = [s['name'] for s in stack_info['services']]
        self.assertIn('web', service_names)
        self.assertIn('db', service_names)
        self.assertIn('redis', service_names)

        print("✓ Test 12: Stack multi-servicio OK")

    def test_13_custom_compose_file_patterns(self):
        """Test: Patrones personalizados de nombres de archivos compose"""
        from docker_compose_manager import DockerComposeManager
        import config

        # Guardar patrón original
        original_patterns = config.COMPOSE_FILE_PATTERNS

        try:
            # Test 1: Patrón con wildcard docker-compose-*.yml
            config.COMPOSE_FILE_PATTERNS = ['docker-compose-*.yml']

            # Crear archivos de test
            stack_dir = os.path.join(self.test_stacks_dir, 'pihole')
            os.makedirs(stack_dir, exist_ok=True)
            compose_file = os.path.join(stack_dir, 'docker-compose-pihole.yml')
            with open(compose_file, 'w') as f:
                yaml.dump({'version': '3', 'services': {'pihole': {'image': 'pihole/pihole:latest'}}}, f)

            manager = DockerComposeManager(self.test_stacks_dir)
            found = manager._find_compose_file(stack_dir)

            self.assertIsNotNone(found, "Debe encontrar docker-compose-pihole.yml con patrón docker-compose-*.yml")
            self.assertTrue(found.endswith('docker-compose-pihole.yml'))

            # Test 2: Patrón *compose*.yml debe encontrar cualquier archivo con "compose"
            config.COMPOSE_FILE_PATTERNS = ['*compose*.yml', '*compose*.yaml']

            # Crear más archivos de test
            test_files = [
                'compose.yml',
                'docker-compose-nginx.yml',
                'my-compose-file.yml',
                'stack-compose-prod.yml'
            ]

            for filename in test_files:
                test_dir = os.path.join(self.test_stacks_dir, f'test_{filename.replace(".", "_")}')
                os.makedirs(test_dir, exist_ok=True)
                test_file = os.path.join(test_dir, filename)
                with open(test_file, 'w') as f:
                    yaml.dump({'version': '3', 'services': {'app': {'image': 'nginx:latest'}}}, f)

                found = manager._find_compose_file(test_dir)
                self.assertIsNotNone(found, f"Debe encontrar {filename} con patrón *compose*.yml")

            print("✓ Test 13: Patrones personalizados de archivos compose OK")

        finally:
            # Restaurar patrón original
            config.COMPOSE_FILE_PATTERNS = original_patterns

    def test_14_scan_with_wildcard_patterns(self):
        """Test: Escanear directorio con diferentes patrones de nombres"""
        from docker_compose_manager import DockerComposeManager
        import config

        # Guardar patrón original
        original_patterns = config.COMPOSE_FILE_PATTERNS

        try:
            # Configurar patrón que acepta cualquier archivo con "compose"
            config.COMPOSE_FILE_PATTERNS = ['*compose*.yml', '*compose*.yaml']

            # Crear varios stacks con diferentes nombres
            stacks_config = {
                'pihole': 'docker-compose-pihole.yml',
                'nginx': 'docker-compose-nginx.yml',
                'redis': 'compose.yml',
                'grafana': 'my-compose-grafana.yml'
            }

            for stack_name, compose_filename in stacks_config.items():
                stack_dir = os.path.join(self.test_stacks_dir, stack_name)
                os.makedirs(stack_dir, exist_ok=True)
                compose_file = os.path.join(stack_dir, compose_filename)
                with open(compose_file, 'w') as f:
                    yaml.dump({
                        'version': '3',
                        'services': {
                            stack_name: {'image': f'{stack_name}:latest'}
                        }
                    }, f)

            manager = DockerComposeManager(self.test_stacks_dir)
            stacks = manager.scan_stacks_directory()

            self.assertEqual(len(stacks), 4, "Debe detectar los 4 stacks con diferentes nombres")
            stack_names = [s['name'] for s in stacks]

            for stack_name in stacks_config.keys():
                self.assertIn(stack_name, stack_names, f"Debe detectar stack {stack_name}")

            print("✓ Test 14: Escaneo con patrones wildcard OK")

        finally:
            # Restaurar patrón original
            config.COMPOSE_FILE_PATTERNS = original_patterns

    def test_15_flat_directory_with_wildcards(self):
        """Test: Archivos compose en directorio plano con wildcards"""
        from docker_compose_manager import DockerComposeManager
        import config

        # Guardar patrón original
        original_patterns = config.COMPOSE_FILE_PATTERNS

        try:
            # Usar patrón permisivo
            config.COMPOSE_FILE_PATTERNS = ['*compose*.yml', '*compose*.yaml']

            # Crear archivos compose directamente en el directorio raíz
            compose_files = {
                'docker-compose-pihole.yml': {'pihole': {'image': 'pihole/pihole:latest'}},
                'docker-compose-nginx.yml': {'nginx': {'image': 'nginx:latest'}},
                'compose-redis.yml': {'redis': {'image': 'redis:alpine'}},
                'my-compose-grafana.yaml': {'grafana': {'image': 'grafana/grafana:latest'}}
            }

            for filename, services in compose_files.items():
                compose_file = os.path.join(self.test_stacks_dir, filename)
                with open(compose_file, 'w') as f:
                    yaml.dump({'version': '3', 'services': services}, f)

            manager = DockerComposeManager(self.test_stacks_dir)
            stacks = manager.scan_stacks_directory()

            self.assertGreaterEqual(len(stacks), 4, "Debe detectar al menos 4 stacks en directorio plano")

            # Verificar que los nombres se extraen correctamente
            stack_names = [s['name'] for s in stacks]
            # docker-compose-pihole.yml -> pihole
            self.assertIn('pihole', stack_names)
            self.assertIn('nginx', stack_names)
            # compose-redis.yml -> redis
            self.assertIn('redis', stack_names)
            # my-compose-grafana.yaml -> grafana
            self.assertIn('grafana', stack_names)

            print("✓ Test 15: Directorio plano con wildcards OK")

        finally:
            # Restaurar patrón original
            config.COMPOSE_FILE_PATTERNS = original_patterns

    def test_16_mixed_subdirs_and_flat_with_wildcards(self):
        """Test: Mezcla de subdirectorios y archivos planos con wildcards"""
        from docker_compose_manager import DockerComposeManager
        import config

        # Guardar patrón original
        original_patterns = config.COMPOSE_FILE_PATTERNS

        try:
            # Usar patrón por defecto (*compose*.yml)
            config.COMPOSE_FILE_PATTERNS = ['*compose*.yml', '*compose*.yaml']

            # Subdirectorios con compose files
            subdir_stacks = {
                'pihole': 'docker-compose.yml',
                'nginx': 'compose.yml',
                'grafana': 'docker-compose-grafana.yml'
            }

            for stack_name, compose_filename in subdir_stacks.items():
                stack_dir = os.path.join(self.test_stacks_dir, stack_name)
                os.makedirs(stack_dir, exist_ok=True)
                compose_file = os.path.join(stack_dir, compose_filename)
                with open(compose_file, 'w') as f:
                    yaml.dump({
                        'version': '3',
                        'services': {stack_name: {'image': f'{stack_name}:latest'}}
                    }, f)

            # Archivos planos en el directorio raíz
            flat_files = {
                'docker-compose-redis.yml': {'redis': {'image': 'redis:alpine'}},
                'compose-postgres.yml': {'postgres': {'image': 'postgres:14'}}
            }

            for filename, services in flat_files.items():
                compose_file = os.path.join(self.test_stacks_dir, filename)
                with open(compose_file, 'w') as f:
                    yaml.dump({'version': '3', 'services': services}, f)

            manager = DockerComposeManager(self.test_stacks_dir)
            stacks = manager.scan_stacks_directory()

            # Debe detectar todos: 3 subdirectorios + 2 archivos planos = 5 stacks
            self.assertGreaterEqual(len(stacks), 5, "Debe detectar 5 stacks (3 subdirs + 2 flat)")

            stack_names = [s['name'] for s in stacks]
            # Subdirectorios
            self.assertIn('pihole', stack_names)
            self.assertIn('nginx', stack_names)
            self.assertIn('grafana', stack_names)
            # Archivos planos
            self.assertIn('redis', stack_names)
            self.assertIn('postgres', stack_names)

            print("✓ Test 16: Mezcla subdirectorios y archivos planos OK")

        finally:
            # Restaurar patrón original
            config.COMPOSE_FILE_PATTERNS = original_patterns

    def test_17_check_stack_updates_no_checker(self):
        """Test: Verificar actualizaciones de stack sin función de verificación"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        stack_dir = os.path.join(self.test_stacks_dir, 'teststack')
        os.makedirs(stack_dir, exist_ok=True)
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        with open(compose_file, 'w') as f:
            yaml.dump({
                'version': '3',
                'services': {
                    'web': {'image': 'nginx:latest'},
                    'db': {'image': 'postgres:14'}
                }
            }, f)

        manager = DockerComposeManager(self.test_stacks_dir)

        # Sin función de verificación (solo detecta que está corriendo)
        result = manager.check_stack_updates('teststack')

        self.assertIsNotNone(result)
        self.assertEqual(result['stack_name'], 'teststack')
        self.assertEqual(result['total_services'], 2)
        self.assertFalse(result['has_updates'])  # Sin checker, no hay updates
        self.assertEqual(len(result['services_with_updates']), 0)

        print("✓ Test 17: Verificar actualizaciones sin checker OK")

    @patch('docker_compose_manager.docker.from_env')
    def test_18_check_stack_updates_with_checker(self, mock_docker_from_env):
        """Test: Verificar actualizaciones con función checker"""
        from docker_compose_manager import DockerComposeManager

        # Crear stack de test
        stack_dir = os.path.join(self.test_stacks_dir, 'teststack')
        os.makedirs(stack_dir, exist_ok=True)
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        with open(compose_file, 'w') as f:
            yaml.dump({
                'version': '3',
                'services': {
                    'web': {'image': 'nginx:latest'},
                    'db': {'image': 'postgres:14'}
                }
            }, f)

        # Mock contenedores corriendo
        mock_container_web = MagicMock()
        mock_container_web.name = 'teststack_web'
        mock_container_web.status = 'running'
        mock_container_web.labels = {
            'com.docker.compose.project': 'teststack',
            'com.docker.compose.service': 'web'
        }
        mock_container_web.attrs = {'Config': {'Image': 'nginx:latest'}}
        mock_container_web.id = 'web123'

        mock_container_db = MagicMock()
        mock_container_db.name = 'teststack_db'
        mock_container_db.status = 'running'
        mock_container_db.labels = {
            'com.docker.compose.project': 'teststack',
            'com.docker.compose.service': 'db'
        }
        mock_container_db.attrs = {'Config': {'Image': 'postgres:14'}}
        mock_container_db.id = 'db456'

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container_web, mock_container_db]
        mock_client.containers.get.side_effect = lambda id: mock_container_web if id == 'web123' else mock_container_db
        mock_docker_from_env.return_value = mock_client

        manager = DockerComposeManager(self.test_stacks_dir)

        # Función checker que simula que web tiene actualización
        def mock_update_checker(container):
            return container.name == 'teststack_web'

        result = manager.check_stack_updates('teststack', mock_update_checker)

        self.assertTrue(result['has_updates'])
        self.assertEqual(len(result['services_with_updates']), 1)
        self.assertEqual(result['services_with_updates'][0]['service_name'], 'web')
        self.assertEqual(result['services_with_updates'][0]['container_name'], 'teststack_web')

        print("✓ Test 18: Verificar actualizaciones con checker OK")

    @patch('docker_compose_manager.docker.from_env')
    def test_19_get_all_stacks_with_updates(self, mock_docker_from_env):
        """Test: Obtener todos los stacks con actualizaciones"""
        from docker_compose_manager import DockerComposeManager

        # Crear dos stacks
        for stack_name in ['stack1', 'stack2']:
            stack_dir = os.path.join(self.test_stacks_dir, stack_name)
            os.makedirs(stack_dir, exist_ok=True)
            compose_file = os.path.join(stack_dir, 'docker-compose.yml')
            with open(compose_file, 'w') as f:
                yaml.dump({
                    'version': '3',
                    'services': {
                        'app': {'image': 'nginx:latest'}
                    }
                }, f)

        # Mock contenedores corriendo
        mock_containers = []
        for i, stack_name in enumerate(['stack1', 'stack2']):
            mock_container = MagicMock()
            mock_container.name = f'{stack_name}_app'
            mock_container.status = 'running'
            mock_container.labels = {
                'com.docker.compose.project': stack_name,
                'com.docker.compose.service': 'app'
            }
            mock_container.attrs = {'Config': {'Image': 'nginx:latest'}}
            mock_container.id = f'id{i}'
            mock_containers.append(mock_container)

        mock_client = MagicMock()
        mock_client.containers.list.return_value = mock_containers
        mock_client.containers.get.side_effect = lambda id: mock_containers[0] if id == 'id0' else mock_containers[1]
        mock_docker_from_env.return_value = mock_client

        manager = DockerComposeManager(self.test_stacks_dir)

        # Checker que dice que solo stack1 tiene updates
        def mock_update_checker(container):
            return 'stack1' in container.name

        stacks_with_updates = manager.get_all_stacks_with_updates(mock_update_checker)

        self.assertEqual(len(stacks_with_updates), 1)
        self.assertEqual(stacks_with_updates[0]['stack_name'], 'stack1')
        self.assertTrue(stacks_with_updates[0]['has_updates'])

        print("✓ Test 19: Obtener todos los stacks con actualizaciones OK")


def run_tests():
    """Ejecutar todos los tests"""
    print("\n" + "="*70)
    print("TESTS DE FUNCIONALIDAD DOCKER COMPOSE STACKS")
    print("="*70 + "\n")

    # Crear suite de tests
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Añadir tests en orden
    suite.addTests(loader.loadTestsFromTestCase(TestDockerComposeManager))
    suite.addTests(loader.loadTestsFromTestCase(TestStackIntegration))

    # Ejecutar con verbose
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Resumen
    print("\n" + "="*70)
    print("RESUMEN DE TESTS")
    print("="*70)
    print(f"Tests ejecutados: {result.testsRun}")
    print(f"Tests exitosos: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"Tests fallidos: {len(result.failures)}")
    print(f"Errores: {len(result.errors)}")

    if result.wasSuccessful():
        print("\n✅ TODOS LOS TESTS PASARON EXITOSAMENTE")
    else:
        print("\n❌ ALGUNOS TESTS FALLARON")

    print("="*70 + "\n")

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
