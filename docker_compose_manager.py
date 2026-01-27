"""
Docker Compose Manager
Gestiona contenedores que son parte de proyectos Docker Compose
"""
import docker
from typing import Dict, List, Optional

# Labels estándar de Docker Compose
COMPOSE_PROJECT_LABEL = 'com.docker.compose.project'
COMPOSE_SERVICE_LABEL = 'com.docker.compose.service'
COMPOSE_WORKING_DIR_LABEL = 'com.docker.compose.project.working_dir'
COMPOSE_CONFIG_FILES_LABEL = 'com.docker.compose.project.config_files'
COMPOSE_DEPENDS_ON_LABEL = 'com.docker.compose.depends_on'


class ComposeProjectInfo:
    """Información de un proyecto Docker Compose"""
    
    def __init__(self, project_name: str, containers: List):
        self.project_name = project_name
        self.containers = containers
        self.services = {}  # service_name -> container
        
        # Agrupar contenedores por servicio
        for container in containers:
            service_name = container.labels.get(COMPOSE_SERVICE_LABEL)
            if service_name:
                self.services[service_name] = container
    
    def get_service_names(self) -> List[str]:
        """Obtiene lista de nombres de servicios"""
        return sorted(list(self.services.keys()))
    
    def get_container_count(self) -> int:
        """Número de contenedores en el proyecto"""
        return len(self.containers)
    
    def get_working_dir(self) -> Optional[str]:
        """Obtiene el directorio de trabajo del proyecto"""
        if self.containers:
            return self.containers[0].labels.get(COMPOSE_WORKING_DIR_LABEL)
        return None
    
    def get_config_files(self) -> Optional[str]:
        """Obtiene la ruta del docker-compose.yml"""
        if self.containers:
            return self.containers[0].labels.get(COMPOSE_CONFIG_FILES_LABEL)
        return None


class ComposeDetector:
    """Detecta si un contenedor es parte de un proyecto Compose"""
    
    @staticmethod
    def is_compose_container(container) -> bool:
        """
        Verifica si un contenedor es parte de un proyecto Compose.
        
        Args:
            container: Objeto container de Docker SDK
            
        Returns:
            bool: True si es parte de un proyecto Compose
        """
        return COMPOSE_PROJECT_LABEL in container.labels
    
    @staticmethod
    def get_project_name(container) -> Optional[str]:
        """
        Obtiene el nombre del proyecto Compose de un contenedor.
        
        Args:
            container: Objeto container de Docker SDK
            
        Returns:
            str: Nombre del proyecto o None si no es Compose
        """
        return container.labels.get(COMPOSE_PROJECT_LABEL)
    
    @staticmethod
    def get_service_name(container) -> Optional[str]:
        """
        Obtiene el nombre del servicio dentro del proyecto Compose.
        
        Args:
            container: Objeto container de Docker SDK
            
        Returns:
            str: Nombre del servicio o None si no es Compose
        """
        return container.labels.get(COMPOSE_SERVICE_LABEL)
    
    @staticmethod
    def get_compose_info(container) -> Optional[Dict[str, str]]:
        """
        Extrae toda la información de Compose de un contenedor.
        
        Args:
            container: Objeto container de Docker SDK
            
        Returns:
            dict: Información de Compose o None si no es parte de Compose
        """
        if not ComposeDetector.is_compose_container(container):
            return None
        
        return {
            'project': container.labels.get(COMPOSE_PROJECT_LABEL),
            'service': container.labels.get(COMPOSE_SERVICE_LABEL),
            'working_dir': container.labels.get(COMPOSE_WORKING_DIR_LABEL),
            'config_files': container.labels.get(COMPOSE_CONFIG_FILES_LABEL),
            'depends_on': container.labels.get(COMPOSE_DEPENDS_ON_LABEL, ''),
        }


class ComposeProjectManager:
    """Gestiona operaciones sobre proyectos Docker Compose completos"""

    def __init__(self, client=None):
        self.client = client or docker.from_env()

    def get_all_projects(self) -> Dict[str, ComposeProjectInfo]:
        """
        Obtiene todos los proyectos Compose detectados.

        Returns:
            dict: Diccionario {project_name: ComposeProjectInfo}
        """
        all_containers = self.client.containers.list(all=True)
        projects = {}

        for container in all_containers:
            project_name = ComposeDetector.get_project_name(container)
            if project_name:
                if project_name not in projects:
                    projects[project_name] = []
                projects[project_name].append(container)

        # Convertir a ComposeProjectInfo
        result = {}
        for project_name, containers in projects.items():
            result[project_name] = ComposeProjectInfo(project_name, containers)

        return result

    def get_project_containers(self, project_name: str) -> List:
        """
        Obtiene todos los contenedores de un proyecto Compose.

        Args:
            project_name: Nombre del proyecto Compose

        Returns:
            list: Lista de contenedores del proyecto
        """
        filters = {
            'label': f'{COMPOSE_PROJECT_LABEL}={project_name}'
        }
        return self.client.containers.list(all=True, filters=filters)

    def get_project_info(self, project_name: str) -> Optional[ComposeProjectInfo]:
        """
        Obtiene información completa de un proyecto Compose.

        Args:
            project_name: Nombre del proyecto

        Returns:
            ComposeProjectInfo: Información del proyecto o None si no existe
        """
        containers = self.get_project_containers(project_name)
        if not containers:
            return None
        return ComposeProjectInfo(project_name, containers)

    def get_service_dependencies(self, container) -> List[str]:
        """
        Obtiene las dependencias (depends_on) de un servicio.

        Args:
            container: Contenedor del cual obtener dependencias

        Returns:
            list: Lista de nombres de servicios de los que depende
        """
        depends_on = container.labels.get(COMPOSE_DEPENDS_ON_LABEL, '')
        if not depends_on:
            return []

        # El formato puede ser:
        # - "service1,service2"
        # - "service1:service_started:false,service2:service_started:false"
        # Necesitamos extraer solo el nombre del servicio
        dependencies = []
        for dep in depends_on.split(','):
            dep = dep.strip()
            if dep:
                # Extraer solo el nombre del servicio (antes del primer ':')
                service_name = dep.split(':')[0].strip()
                if service_name:
                    dependencies.append(service_name)

        return dependencies

    def sort_containers_by_dependencies(self, containers: List) -> List:
        """
        Ordena contenedores según sus dependencias (topological sort).
        Los contenedores sin dependencias van primero.

        Args:
            containers: Lista de contenedores a ordenar

        Returns:
            list: Contenedores ordenados según dependencias
        """
        # Crear mapa de servicio -> contenedor
        service_to_container = {}
        for container in containers:
            service_name = ComposeDetector.get_service_name(container)
            if service_name:
                service_to_container[service_name] = container

        # Crear grafo de dependencias
        # dependencies[service] = [list of services it depends on]
        dependencies = {}
        for container in containers:
            service_name = ComposeDetector.get_service_name(container)
            if service_name:
                deps = self.get_service_dependencies(container)
                # Filtrar solo dependencias que existen en este proyecto
                deps = [d for d in deps if d in service_to_container]
                dependencies[service_name] = deps

        # Ordenamiento topológico (Kahn's algorithm)
        # Calcular in-degree (número de servicios que dependen de cada uno)
        in_degree = {service: len(deps) for service, deps in dependencies.items()}

        # Cola con servicios sin dependencias (in-degree = 0)
        queue = [service for service, degree in in_degree.items() if degree == 0]
        sorted_services = []

        while queue:
            # Ordenar alfabéticamente para consistencia
            queue.sort()
            service = queue.pop(0)
            sorted_services.append(service)

            # Para cada servicio que depende del actual, reducir su in-degree
            for other_service, deps in dependencies.items():
                if service in deps and other_service not in sorted_services:
                    in_degree[other_service] -= 1
                    if in_degree[other_service] == 0:
                        queue.append(other_service)

        # Si hay ciclos, añadir servicios restantes al final
        remaining = [s for s in service_to_container.keys() if s not in sorted_services]
        sorted_services.extend(sorted(remaining))

        # Convertir servicios ordenados a contenedores
        sorted_containers = []
        for service in sorted_services:
            if service in service_to_container:
                sorted_containers.append(service_to_container[service])

        return sorted_containers

