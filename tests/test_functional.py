#!/usr/bin/env python3
"""
Test funcional con stacks reales de demostración
"""
import os
import sys

# Añadir el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
os.environ['COMPOSE_STACKS_DIR'] = '/tmp/demo_stacks'

print("="*70)
print("TEST FUNCIONAL CON STACKS DE DEMOSTRACIÓN")
print("="*70)
print()

# Test 1: Verificar que los stacks de demo existen
print("✓ Test 1: Verificando stacks de demostración...")
stacks_dir = '/tmp/demo_stacks'

if not os.path.exists(stacks_dir):
    print(f"  ❌ ERROR: Directorio {stacks_dir} no existe")
    print(f"  Ejecuta: ./create_demo_stacks.sh")
    sys.exit(1)

expected_stacks = ['nginx-demo', 'redis-demo', 'webapp-demo']
found_stacks = []

for stack_name in expected_stacks:
    stack_path = os.path.join(stacks_dir, stack_name, 'docker-compose.yml')
    if os.path.exists(stack_path):
        found_stacks.append(stack_name)
        print(f"  ✅ Stack encontrado: {stack_name}")
    else:
        print(f"  ❌ Stack no encontrado: {stack_name}")

if len(found_stacks) != len(expected_stacks):
    print(f"  ❌ ERROR: Se esperaban {len(expected_stacks)} stacks, se encontraron {len(found_stacks)}")
    sys.exit(1)

print(f"  ✅ Todos los stacks de demo encontrados ({len(found_stacks)})")
print()

# Test 2: Importar y usar DockerComposeManager
print("✓ Test 2: Probando DockerComposeManager con stacks reales...")
try:
    from docker_compose_manager import DockerComposeManager

    manager = DockerComposeManager(stacks_dir)
    print("  ✅ DockerComposeManager instanciado")

    # Escanear stacks
    stacks = manager.scan_stacks_directory()
    print(f"  ✅ Stacks detectados: {len(stacks)}")

    for stack in stacks:
        print(f"     • {stack['name']} - {stack['service_count']} service(s)")

    if len(stacks) != 3:
        print(f"  ❌ ERROR: Se esperaban 3 stacks, se detectaron {len(stacks)}")
        sys.exit(1)

    print("  ✅ Número correcto de stacks detectados")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 3: Validar archivos compose
print("✓ Test 3: Validando archivos docker-compose.yml...")
try:
    for stack in stacks:
        stack_name = stack['name']
        compose_file = stack['compose_file']

        is_valid = manager.validate_compose_file(compose_file)

        if is_valid:
            print(f"  ✅ {stack_name}: Válido")
        else:
            print(f"  ❌ {stack_name}: Inválido")
            sys.exit(1)

    print("  ✅ Todos los archivos compose son válidos")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 4: Obtener info detallada de cada stack
print("✓ Test 4: Obteniendo información detallada de stacks...")
try:
    for stack_name in expected_stacks:
        stack_info = manager.get_stack_info(stack_name)

        if not stack_info:
            print(f"  ❌ {stack_name}: No se pudo obtener info")
            sys.exit(1)

        services = stack_info.get('services', [])
        service_names = [s['name'] for s in services]

        print(f"  ✅ {stack_name}:")
        print(f"     Services: {', '.join(service_names)}")
        print(f"     Source: {stack_info.get('source')}")
        print(f"     Running: {stack_info.get('running', False)}")

    print("  ✅ Información obtenida correctamente para todos los stacks")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 5: Listar todos los stacks (combinado)
print("✓ Test 5: Listando todos los stacks...")
try:
    all_stacks = manager.list_all_stacks()

    print(f"  ✅ Total de stacks listados: {len(all_stacks)}")

    for stack in all_stacks:
        name = stack['name']
        running = stack.get('running', False)
        services = len(stack.get('services', []))
        source = stack.get('source', 'unknown')

        status = "🟢" if running else "⚪"
        print(f"     {status} {name} ({services} services) - source: {source}")

    print("  ✅ Listado completo generado correctamente")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 6: Verificar que los métodos de operación existen
print("✓ Test 6: Verificando métodos de operación...")
try:
    operations = [
        'stack_start',
        'stack_stop',
        'stack_restart',
        'stack_update',
        'stack_logs'
    ]

    for op in operations:
        if not hasattr(manager, op):
            print(f"  ❌ Método {op} no encontrado")
            sys.exit(1)

    print(f"  ✅ Todos los métodos de operación disponibles ({len(operations)})")

    # Verificar que son callables
    for op in operations:
        method = getattr(manager, op)
        if not callable(method):
            print(f"  ❌ {op} no es callable")
            sys.exit(1)

    print(f"  ✅ Todos los métodos son invocables")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Resumen
print("="*70)
print("✅ TODOS LOS TESTS FUNCIONALES PASARON")
print("="*70)
print()
print("Resumen:")
print(f"  • Stacks de demo creados y detectados: {len(found_stacks)}")
print(f"  • Archivos compose validados: {len(stacks)}")
print(f"  • Información detallada obtenida: {len(expected_stacks)}")
print(f"  • Métodos de operación verificados: {len(operations)}")
print()
print("📋 Stacks disponibles para pruebas:")
for stack in all_stacks:
    print(f"   • {stack['name']}")
print()
print("🔧 Próximos pasos:")
print("   1. Los stacks están listos en: /tmp/demo_stacks")
print("   2. Configura el bot con COMPOSE_STACKS_ENABLED=1")
print("   3. Configura COMPOSE_STACKS_DIR=/tmp/demo_stacks")
print("   4. Reinicia el bot")
print("   5. Ejecuta /stacks en Telegram")
print()
print("🚀 Para iniciar un stack manualmente:")
print(f"   docker compose -f /tmp/demo_stacks/nginx-demo/docker-compose.yml up -d")
print()
print("🛑 Para detener:")
print(f"   docker compose -f /tmp/demo_stacks/nginx-demo/docker-compose.yml down")
print()
