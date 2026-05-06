#!/usr/bin/env python3
"""
Test script to validate Docker Compose project detection
Part of PHASE 1 of v4.0.0 implementation

HOW TO RUN THIS SCRIPT:
-----------------------
This script must be run inside the bot container where Docker is available.

Option 1 - Run inside the container:
    docker exec -it <bot-container-name> python3 test_compose_detection.py

Option 2 - Copy into the container and run:
    docker cp test_compose_detection.py <bot-container-name>:/app/
    docker exec -it <bot-container-name> python3 /app/test_compose_detection.py

Option 3 - If the bot is not running, run temporarily:
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
        -v $(pwd):/app -w /app python:3.12 \
        sh -c "pip install docker && python3 test_compose_detection.py"
"""

import docker
from docker_compose_manager import (
    ComposeDetector,
    ComposeProjectManager,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL
)


def print_separator(title=""):
    """Prints a visual separator"""
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    else:
        print(f"{'='*60}")


def test_compose_detection():
    """Tests Compose project detection"""

    print_separator("TEST: Docker Compose Project Detection")

    # Initialize client and manager
    client = docker.from_env()
    manager = ComposeProjectManager(client)

    # 1. List all containers
    print("\n1. ALL CONTAINERS:")
    print("-" * 60)
    all_containers = client.containers.list(all=True)
    print(f"Total containers: {len(all_containers)}\n")

    for container in all_containers:
        is_compose = ComposeDetector.is_compose_container(container)
        status_icon = "📦" if is_compose else "🐳"
        print(f"{status_icon} {container.name:30} | Status: {container.status:10} | Compose: {is_compose}")

    # 2. Detect Compose projects
    print_separator("2. DETECTED COMPOSE PROJECTS")
    projects = manager.get_all_projects()

    if not projects:
        print("❌ No Compose projects detected")
        return

    print(f"✅ Detected {len(projects)} Compose project(s):\n")

    for project_name, project_info in sorted(projects.items()):
        container_count = project_info.get_container_count()
        print(f"📦 Project: {project_name}")
        print(f"   Containers: {container_count}")
        print(f"   Services: {', '.join(project_info.get_service_names())}")

        working_dir = project_info.get_working_dir()
        if working_dir:
            print(f"   Directory: {working_dir}")

        config_files = project_info.get_config_files()
        if config_files:
            print(f"   Config: {config_files}")

        print()

    # 3. Details of each project
    print_separator("3. CONTAINER DETAIL PER PROJECT")

    for project_name, project_info in sorted(projects.items()):
        print(f"\n📦 Project: {project_name}")
        print("-" * 60)

        for container in project_info.containers:
            service_name = ComposeDetector.get_service_name(container)
            dependencies = manager.get_service_dependencies(container)
            raw_depends = container.labels.get('com.docker.compose.depends_on', '')

            print(f"  • {container.name}")
            print(f"    Service: {service_name}")
            print(f"    Status: {container.status}")
            print(f"    ID: {container.id[:12]}")

            if raw_depends:
                print(f"    Depends_on (raw): {raw_depends}")

            if dependencies:
                print(f"    Depends on (parsed): {', '.join(dependencies)}")
            else:
                print(f"    Depends on: (none)")
            print()

    # 4. Dependency order
    print_separator("4. START/STOP ORDER PER PROJECT")

    for project_name, project_info in sorted(projects.items()):
        print(f"\n📦 Project: {project_name}")
        print("-" * 60)

        # Sort containers
        sorted_containers = manager.sort_containers_by_dependencies(project_info.containers)

        print("  START order (respecting dependencies):")
        for i, container in enumerate(sorted_containers, 1):
            service_name = ComposeDetector.get_service_name(container)
            print(f"    {i}. {service_name} ({container.name})")

        print("\n  STOP order (reverse):")
        for i, container in enumerate(reversed(sorted_containers), 1):
            service_name = ComposeDetector.get_service_name(container)
            print(f"    {i}. {service_name} ({container.name})")
        print()

    # 5. Standalone containers (non-Compose)
    print_separator("5. STANDALONE CONTAINERS (Non-Compose)")

    standalone_containers = [c for c in all_containers if not ComposeDetector.is_compose_container(c)]

    if standalone_containers:
        print(f"Total: {len(standalone_containers)}\n")
        for container in standalone_containers:
            print(f"🐳 {container.name:30} | Status: {container.status}")
    else:
        print("No standalone containers")

    # 6. Final summary
    print_separator("SUMMARY")
    print(f"Total containers: {len(all_containers)}")
    print(f"Compose projects: {len(projects)}")
    print(f"Containers in projects: {sum(p.get_container_count() for p in projects.values())}")
    print(f"Standalone containers: {len(standalone_containers)}")
    print_separator()


if __name__ == "__main__":
    try:
        test_compose_detection()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

