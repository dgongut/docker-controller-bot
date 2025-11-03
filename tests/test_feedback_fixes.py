#!/usr/bin/env python3
"""
Tests para las correcciones basadas en feedback del autor del proyecto

Problema #1: Stacks ficticios (solo corriendo, sin archivo compose) aparecían en lista
Problema #2: Contenedores huérfanos no se eliminaban al actualizar
Problema #3: Case sensitivity causaba duplicados (TEST vs test)
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import Mock, MagicMock, patch

# Mock de las variables de entorno necesarias
os.environ['TELEGRAM_TOKEN'] = 'test_token'
os.environ['TELEGRAM_ADMIN'] = '12345'
os.environ['CONTAINER_NAME'] = 'docker-controller-bot'
os.environ['TZ'] = 'UTC'
os.environ['CHECK_UPDATES'] = '0'
os.environ['CHECK_UPDATE_EVERY_HOURS'] = '4'
os.environ['CHECK_UPDATE_STOPPED_CONTAINERS'] = '0'
os.environ['LANGUAGE'] = 'EN'
os.environ['EXTENDED_MESSAGES'] = '0'
os.environ['BUTTON_COLUMNS'] = '2'
os.environ['COMPOSE_STACKS_ENABLED'] = '1'
os.environ['COMPOSE_STACKS_DIR'] = '/tmp/test_stacks'


class TestFeedbackFix1_NoFictitiousStacks(unittest.TestCase):
    """
    Problema #1: Stacks que solo están corriendo (sin archivo compose) no deben aparecer
    """

    @patch('docker.from_env')
    def test_running_stack_without_file_not_listed(self, mock_docker):
        """Test: Stack corriendo sin archivo compose NO debe aparecer en la lista"""
        from docker_compose_manager import DockerComposeManager

        # Mock de contenedores corriendo con labels de stack
        running_container = MagicMock()
        running_container.name = 'orphan-redis'
        running_container.labels = {
            'com.docker.compose.project': 'orphan-stack',
            'com.docker.compose.service': 'redis'
        }
        running_container.status = 'running'

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [running_container]
        mock_docker.return_value = mock_client

        # Crear directorio vacío (sin archivos compose)
        test_dir = tempfile.mkdtemp()

        try:
            manager = DockerComposeManager(test_dir)
            stacks = manager.list_all_stacks()

            # El stack "orphan-stack" NO debe aparecer (solo está corriendo, sin archivo)
            stack_names = [s['name'] for s in stacks]
            self.assertNotIn('orphan-stack', stack_names)
            self.assertEqual(len(stacks), 0, "No debe haber ningún stack sin archivo compose")

        finally:
            import shutil
            shutil.rmtree(test_dir)

    @patch('docker.from_env')
    def test_stack_with_file_appears_even_if_not_running(self, mock_docker):
        """Test: Stack con archivo compose debe aparecer aunque no esté corriendo"""
        from docker_compose_manager import DockerComposeManager

        mock_client = MagicMock()
        mock_client.containers.list.return_value = []  # No hay contenedores corriendo
        mock_docker.return_value = mock_client

        # Crear directorio con un archivo compose
        test_dir = tempfile.mkdtemp()
        stack_dir = os.path.join(test_dir, 'mystack')
        os.makedirs(stack_dir)

        compose_content = """
version: '3'
services:
  web:
    image: nginx:alpine
"""
        with open(os.path.join(stack_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)
            stacks = manager.list_all_stacks()

            # El stack "mystack" DEBE aparecer (tiene archivo)
            stack_names = [s['name'] for s in stacks]
            self.assertIn('mystack', stack_names)
            self.assertEqual(len(stacks), 1)

            # Y debe indicar que no está corriendo
            mystack = [s for s in stacks if s['name'] == 'mystack'][0]
            self.assertFalse(mystack['running'])

        finally:
            import shutil
            shutil.rmtree(test_dir)


class TestFeedbackFix2_RemoveOrphans(unittest.TestCase):
    """
    Problema #2: Contenedores huérfanos deben eliminarse al actualizar
    """

    def test_stack_start_includes_remove_orphans(self):
        """Test: stack_start incluye el flag --remove-orphans"""
        from docker_compose_manager import DockerComposeManager

        test_dir = tempfile.mkdtemp()
        stack_dir = os.path.join(test_dir, 'teststack')
        os.makedirs(stack_dir)

        compose_content = """
version: '3'
services:
  web:
    image: nginx:alpine
"""
        with open(os.path.join(stack_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)

            # Mock de _run_compose_command para capturar el comando
            with patch.object(manager, '_run_compose_command') as mock_run:
                mock_run.return_value = {'success': True, 'stdout': '', 'stderr': ''}

                manager.stack_start('teststack')

                # Verificar que se llamó con --remove-orphans
                mock_run.assert_called_once()
                call_args = mock_run.call_args[0]
                command = call_args[1]

                self.assertIn('--remove-orphans', command,
                             "El comando up debe incluir --remove-orphans")

        finally:
            import shutil
            shutil.rmtree(test_dir)

    def test_stack_update_includes_remove_orphans(self):
        """Test: stack_update incluye el flag --remove-orphans"""
        from docker_compose_manager import DockerComposeManager

        test_dir = tempfile.mkdtemp()
        stack_dir = os.path.join(test_dir, 'teststack')
        os.makedirs(stack_dir)

        compose_content = """
version: '3'
services:
  web:
    image: nginx:alpine
"""
        with open(os.path.join(stack_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)

            # Mock de _run_compose_command
            with patch.object(manager, '_run_compose_command') as mock_run:
                mock_run.return_value = {'success': True, 'stdout': '', 'stderr': ''}

                manager.stack_update('teststack')

                # Debe haber 2 llamadas: pull y up
                self.assertEqual(mock_run.call_count, 2)

                # La segunda llamada (up) debe incluir --remove-orphans
                second_call_args = mock_run.call_args_list[1][0]
                command = second_call_args[1]

                self.assertIn('--remove-orphans', command,
                             "El comando up del update debe incluir --remove-orphans")

        finally:
            import shutil
            shutil.rmtree(test_dir)


class TestFeedbackFix3_CaseSensitivity(unittest.TestCase):
    """
    Problema #3: Case sensitivity causaba duplicados (TEST vs test)
    """

    @patch('docker.from_env')
    def test_stack_name_normalized_to_lowercase_subdirectory(self, mock_docker):
        """Test: Nombre de stack de subdirectorio se normaliza a lowercase"""
        from docker_compose_manager import DockerComposeManager

        mock_client = MagicMock()
        mock_client.containers.list.return_value = []
        mock_docker.return_value = mock_client

        # Crear directorio con carpeta en MAYÚSCULAS
        test_dir = tempfile.mkdtemp()
        stack_dir = os.path.join(test_dir, 'TEST')  # MAYÚSCULAS
        os.makedirs(stack_dir)

        compose_content = """
version: '3'
services:
  web:
    image: nginx:alpine
"""
        with open(os.path.join(stack_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)
            stacks = manager.list_all_stacks()

            # El nombre debe estar en lowercase
            stack_names = [s['name'] for s in stacks]
            self.assertIn('test', stack_names)
            self.assertNotIn('TEST', stack_names)

        finally:
            import shutil
            shutil.rmtree(test_dir)

    @patch('docker.from_env')
    def test_stack_name_normalized_to_lowercase_filename(self, mock_docker):
        """Test: Nombre de stack extraído de archivo se normaliza a lowercase"""
        from docker_compose_manager import DockerComposeManager

        mock_client = MagicMock()
        mock_client.containers.list.return_value = []
        mock_docker.return_value = mock_client

        # Crear archivo con nombre en MAYÚSCULAS
        test_dir = tempfile.mkdtemp()
        compose_file = os.path.join(test_dir, 'docker-compose-REDIS.yml')  # MAYÚSCULAS

        compose_content = """
version: '3'
services:
  redis:
    image: redis:alpine
"""
        with open(compose_file, 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)
            stacks = manager.list_all_stacks()

            # El nombre debe estar en lowercase
            stack_names = [s['name'] for s in stacks]
            # El nombre se extrae de "docker-compose-REDIS.yml" -> "REDIS" -> "redis"
            self.assertIn('redis', stack_names)
            self.assertNotIn('REDIS', stack_names)

        finally:
            import shutil
            shutil.rmtree(test_dir)

    @patch('docker.from_env')
    def test_no_duplicate_stacks_with_mixed_case(self, mock_docker):
        """Test: No debe haber duplicados por diferencia de case"""
        from docker_compose_manager import DockerComposeManager

        # Mock de contenedor corriendo con project name en lowercase
        running_container = MagicMock()
        running_container.name = 'test-web'
        running_container.labels = {
            'com.docker.compose.project': 'test',  # Docker Compose usa lowercase
            'com.docker.compose.service': 'web'
        }
        running_container.status = 'running'

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [running_container]
        mock_docker.return_value = mock_client

        # Crear directorio con carpeta en MAYÚSCULAS
        test_dir = tempfile.mkdtemp()
        stack_dir = os.path.join(test_dir, 'TEST')  # MAYÚSCULAS
        os.makedirs(stack_dir)

        compose_content = """
version: '3'
services:
  web:
    image: nginx:alpine
"""
        with open(os.path.join(stack_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose_content)

        try:
            manager = DockerComposeManager(test_dir)
            stacks = manager.list_all_stacks()

            # Solo debe haber 1 stack llamado "test" (no "TEST" separado)
            stack_names = [s['name'] for s in stacks]
            self.assertEqual(len(stacks), 1)
            self.assertIn('test', stack_names)

            # Y debe estar marcado como running
            test_stack = stacks[0]
            self.assertTrue(test_stack['running'])

        finally:
            import shutil
            shutil.rmtree(test_dir)


if __name__ == '__main__':
    unittest.main()
