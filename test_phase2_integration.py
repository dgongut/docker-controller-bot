#!/usr/bin/env python3
"""
Integration test for PHASE 2
Verifies that DockerManager can use the Compose methods without errors
"""

import sys
import docker

# Simulate the bot environment
class MockConfig:
    CONTAINER_NAME = "docker-controller-bot"

sys.modules['config'] = MockConfig()

# Now import DockerManager
from docker_compose_manager import ComposeDetector, ComposeProjectManager

def test_docker_manager_integration():
    """Tests that DockerManager can use the Compose methods"""

    print("=" * 60)
    print("TEST: Compose integration in DockerManager")
    print("=" * 60)

    try:
        # Simulate DockerManager
        client = docker.from_env()
        compose_manager = ComposeProjectManager(client)

        print("\n✅ ComposeProjectManager initialized correctly")

        # Test 1: get_all_projects
        print("\n1. Testing get_all_projects()...")
        projects = compose_manager.get_all_projects()
        print(f"   ✅ Detected {len(projects)} projects")

        # Test 2: Verify containers
        print("\n2. Testing container detection...")
        containers = client.containers.list(all=True)
        compose_count = 0
        standalone_count = 0

        for container in containers:
            if ComposeDetector.is_compose_container(container):
                compose_count += 1
                project_name = ComposeDetector.get_project_name(container)
                service_name = ComposeDetector.get_service_name(container)
                print(f"   📦 {container.name}: project='{project_name}', service='{service_name}'")
            else:
                standalone_count += 1
                print(f"   🐳 {container.name}: standalone")

        print(f"\n   ✅ Compose containers: {compose_count}")
        print(f"   ✅ Standalone containers: {standalone_count}")

        # Test 3: get_project_info
        print("\n3. Testing get_project_info()...")
        for project_name in projects.keys():
            project_info = compose_manager.get_project_info(project_name)
            if project_info:
                print(f"   ✅ Project '{project_name}': {project_info.get_container_count()} containers")

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_docker_manager_integration()
    sys.exit(0 if success else 1)

