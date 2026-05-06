"""
Docker Compose Manager
Manages containers that are part of Docker Compose projects
"""
import docker
from typing import Dict, List, Optional

# Standard Docker Compose labels
COMPOSE_PROJECT_LABEL = 'com.docker.compose.project'
COMPOSE_SERVICE_LABEL = 'com.docker.compose.service'
COMPOSE_WORKING_DIR_LABEL = 'com.docker.compose.project.working_dir'
COMPOSE_CONFIG_FILES_LABEL = 'com.docker.compose.project.config_files'
COMPOSE_DEPENDS_ON_LABEL = 'com.docker.compose.depends_on'


class ComposeProjectInfo:
    """Information about a Docker Compose project"""

    def __init__(self, project_name: str, containers: List):
        self.project_name = project_name
        self.containers = containers
        self.services = {}  # service_name -> container

        # Group containers by service
        for container in containers:
            service_name = container.labels.get(COMPOSE_SERVICE_LABEL)
            if service_name:
                self.services[service_name] = container

    def get_service_names(self) -> List[str]:
        """Returns the list of service names"""
        return sorted(list(self.services.keys()))

    def get_container_count(self) -> int:
        """Number of containers in the project"""
        return len(self.containers)

    def get_working_dir(self) -> Optional[str]:
        """Returns the project's working directory"""
        if self.containers:
            return self.containers[0].labels.get(COMPOSE_WORKING_DIR_LABEL)
        return None

    def get_config_files(self) -> Optional[str]:
        """Returns the path to the docker-compose.yml"""
        if self.containers:
            return self.containers[0].labels.get(COMPOSE_CONFIG_FILES_LABEL)
        return None


class ComposeDetector:
    """Detects whether a container is part of a Compose project"""

    @staticmethod
    def is_compose_container(container) -> bool:
        """
        Checks whether a container is part of a Compose project.

        Args:
            container: Docker SDK container object

        Returns:
            bool: True if it is part of a Compose project
        """
        return COMPOSE_PROJECT_LABEL in container.labels

    @staticmethod
    def get_project_name(container) -> Optional[str]:
        """
        Returns the Compose project name of a container.

        Args:
            container: Docker SDK container object

        Returns:
            str: Project name or None if not a Compose container
        """
        return container.labels.get(COMPOSE_PROJECT_LABEL)

    @staticmethod
    def get_service_name(container) -> Optional[str]:
        """
        Returns the service name within the Compose project.

        Args:
            container: Docker SDK container object

        Returns:
            str: Service name or None if not a Compose container
        """
        return container.labels.get(COMPOSE_SERVICE_LABEL)

    @staticmethod
    def get_compose_info(container) -> Optional[Dict[str, str]]:
        """
        Extracts all Compose information from a container.

        Args:
            container: Docker SDK container object

        Returns:
            dict: Compose information or None if not part of a Compose project
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
    """Manages operations on entire Docker Compose projects"""

    def __init__(self, client=None):
        self.client = client or docker.from_env()

    def get_all_projects(self) -> Dict[str, ComposeProjectInfo]:
        """
        Returns all detected Compose projects.

        Returns:
            dict: Dictionary {project_name: ComposeProjectInfo}
        """
        all_containers = self.client.containers.list(all=True)
        projects = {}

        for container in all_containers:
            project_name = ComposeDetector.get_project_name(container)
            if project_name:
                if project_name not in projects:
                    projects[project_name] = []
                projects[project_name].append(container)

        # Convert to ComposeProjectInfo
        result = {}
        for project_name, containers in projects.items():
            result[project_name] = ComposeProjectInfo(project_name, containers)

        return result

    def get_project_containers(self, project_name: str) -> List:
        """
        Returns all containers belonging to a Compose project.

        Args:
            project_name: Compose project name

        Returns:
            list: List of containers in the project
        """
        filters = {
            'label': f'{COMPOSE_PROJECT_LABEL}={project_name}'
        }
        return self.client.containers.list(all=True, filters=filters)

    def get_project_info(self, project_name: str) -> Optional[ComposeProjectInfo]:
        """
        Returns full information about a Compose project.

        Args:
            project_name: Project name

        Returns:
            ComposeProjectInfo: Project information or None if it doesn't exist
        """
        containers = self.get_project_containers(project_name)
        if not containers:
            return None
        return ComposeProjectInfo(project_name, containers)

    def get_service_dependencies(self, container) -> List[str]:
        """
        Returns the dependencies (depends_on) of a service.

        Args:
            container: Container to get dependencies from

        Returns:
            list: List of service names this service depends on
        """
        depends_on = container.labels.get(COMPOSE_DEPENDS_ON_LABEL, '')
        if not depends_on:
            return []

        # The format can be:
        # - "service1,service2"
        # - "service1:service_started:false,service2:service_started:false"
        # We need to extract only the service name
        dependencies = []
        for dep in depends_on.split(','):
            dep = dep.strip()
            if dep:
                # Extract only the service name (before the first ':')
                service_name = dep.split(':')[0].strip()
                if service_name:
                    dependencies.append(service_name)

        return dependencies

    def sort_containers_by_dependencies(self, containers: List) -> List:
        """
        Sorts containers by their dependencies (topological sort).
        Containers without dependencies come first.

        Args:
            containers: List of containers to sort

        Returns:
            list: Containers sorted according to dependencies
        """
        # Build service -> container map
        service_to_container = {}
        for container in containers:
            service_name = ComposeDetector.get_service_name(container)
            if service_name:
                service_to_container[service_name] = container

        # Build dependency graph
        # dependencies[service] = [list of services it depends on]
        dependencies = {}
        for container in containers:
            service_name = ComposeDetector.get_service_name(container)
            if service_name:
                deps = self.get_service_dependencies(container)
                # Keep only dependencies that exist in this project
                deps = [d for d in deps if d in service_to_container]
                dependencies[service_name] = deps

        # Topological sort (Kahn's algorithm)
        # Compute in-degree (number of services each one depends on)
        in_degree = {service: len(deps) for service, deps in dependencies.items()}

        # Queue with services without dependencies (in-degree = 0)
        queue = [service for service, degree in in_degree.items() if degree == 0]
        sorted_services = []

        while queue:
            # Sort alphabetically for consistency
            queue.sort()
            service = queue.pop(0)
            sorted_services.append(service)

            # For each service depending on the current one, reduce its in-degree
            for other_service, deps in dependencies.items():
                if service in deps and other_service not in sorted_services:
                    in_degree[other_service] -= 1
                    if in_degree[other_service] == 0:
                        queue.append(other_service)

        # If there are cycles, append remaining services at the end
        remaining = [s for s in service_to_container.keys() if s not in sorted_services]
        sorted_services.extend(sorted(remaining))

        # Convert sorted services back to containers
        sorted_containers = []
        for service in sorted_services:
            if service in service_to_container:
                sorted_containers.append(service_to_container[service])

        return sorted_containers

