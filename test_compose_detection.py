#!/usr/bin/env python3
"""
Script de prueba para validar la detección de proyectos Docker Compose
Parte de la FASE 1 de implementación v4.0.0

CÓMO EJECUTAR ESTE SCRIPT:
--------------------------
Este script debe ejecutarse dentro del contenedor del bot donde Docker está disponible.

Opción 1 - Ejecutar dentro del contenedor:
    docker exec -it <nombre-contenedor-bot> python3 test_compose_detection.py

Opción 2 - Copiar al contenedor y ejecutar:
    docker cp test_compose_detection.py <nombre-contenedor-bot>:/app/
    docker exec -it <nombre-contenedor-bot> python3 /app/test_compose_detection.py

Opción 3 - Si el bot no está corriendo, ejecutar temporalmente:
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
    """Imprime un separador visual"""
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    else:
        print(f"{'='*60}")


def test_compose_detection():
    """Prueba la detección de proyectos Compose"""
    
    print_separator("TEST: Detección de Proyectos Docker Compose")
    
    # Inicializar cliente y manager
    client = docker.from_env()
    manager = ComposeProjectManager(client)
    
    # 1. Listar todos los contenedores
    print("\n1. TODOS LOS CONTENEDORES:")
    print("-" * 60)
    all_containers = client.containers.list(all=True)
    print(f"Total de contenedores: {len(all_containers)}\n")
    
    for container in all_containers:
        is_compose = ComposeDetector.is_compose_container(container)
        status_icon = "📦" if is_compose else "🐳"
        print(f"{status_icon} {container.name:30} | Status: {container.status:10} | Compose: {is_compose}")
    
    # 2. Detectar proyectos Compose
    print_separator("2. PROYECTOS COMPOSE DETECTADOS")
    projects = manager.get_all_projects()
    
    if not projects:
        print("❌ No se detectaron proyectos Compose")
        return
    
    print(f"✅ Se detectaron {len(projects)} proyecto(s) Compose:\n")
    
    for project_name, project_info in sorted(projects.items()):
        container_count = project_info.get_container_count()
        print(f"📦 Proyecto: {project_name}")
        print(f"   Contenedores: {container_count}")
        print(f"   Servicios: {', '.join(project_info.get_service_names())}")
        
        working_dir = project_info.get_working_dir()
        if working_dir:
            print(f"   Directorio: {working_dir}")
        
        config_files = project_info.get_config_files()
        if config_files:
            print(f"   Config: {config_files}")
        
        print()
    
    # 3. Detalles de cada proyecto
    print_separator("3. DETALLE DE CONTENEDORES POR PROYECTO")
    
    for project_name, project_info in sorted(projects.items()):
        print(f"\n📦 Proyecto: {project_name}")
        print("-" * 60)
        
        for container in project_info.containers:
            service_name = ComposeDetector.get_service_name(container)
            dependencies = manager.get_service_dependencies(container)
            raw_depends = container.labels.get('com.docker.compose.depends_on', '')

            print(f"  • {container.name}")
            print(f"    Servicio: {service_name}")
            print(f"    Status: {container.status}")
            print(f"    ID: {container.id[:12]}")

            if raw_depends:
                print(f"    Depends_on (raw): {raw_depends}")

            if dependencies:
                print(f"    Depende de (parsed): {', '.join(dependencies)}")
            else:
                print(f"    Depende de: (ninguno)")
            print()
    
    # 4. Orden de dependencias
    print_separator("4. ORDEN DE INICIO/PARADA POR PROYECTO")
    
    for project_name, project_info in sorted(projects.items()):
        print(f"\n📦 Proyecto: {project_name}")
        print("-" * 60)
        
        # Ordenar contenedores
        sorted_containers = manager.sort_containers_by_dependencies(project_info.containers)
        
        print("  Orden de INICIO (respetando dependencias):")
        for i, container in enumerate(sorted_containers, 1):
            service_name = ComposeDetector.get_service_name(container)
            print(f"    {i}. {service_name} ({container.name})")
        
        print("\n  Orden de PARADA (inverso):")
        for i, container in enumerate(reversed(sorted_containers), 1):
            service_name = ComposeDetector.get_service_name(container)
            print(f"    {i}. {service_name} ({container.name})")
        print()
    
    # 5. Contenedores standalone (no Compose)
    print_separator("5. CONTENEDORES STANDALONE (No Compose)")
    
    standalone_containers = [c for c in all_containers if not ComposeDetector.is_compose_container(c)]
    
    if standalone_containers:
        print(f"Total: {len(standalone_containers)}\n")
        for container in standalone_containers:
            print(f"🐳 {container.name:30} | Status: {container.status}")
    else:
        print("No hay contenedores standalone")
    
    # 6. Resumen final
    print_separator("RESUMEN")
    print(f"Total de contenedores: {len(all_containers)}")
    print(f"Proyectos Compose: {len(projects)}")
    print(f"Contenedores en proyectos: {sum(p.get_container_count() for p in projects.values())}")
    print(f"Contenedores standalone: {len(standalone_containers)}")
    print_separator()


if __name__ == "__main__":
    try:
        test_compose_detection()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

