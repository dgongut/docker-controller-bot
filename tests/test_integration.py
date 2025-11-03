#!/usr/bin/env python3
"""
Test de integración básico para verificar que el código se ejecuta sin errores
"""
import os
import sys
import tempfile

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
os.environ['COMPOSE_STACKS_ENABLED'] = '0'  # Deshabilitado por defecto para test
os.environ['COMPOSE_STACKS_DIR'] = '/tmp/test_stacks'

print("="*70)
print("TEST DE INTEGRACIÓN - VERIFICACIÓN DE SINTAXIS Y IMPORTS")
print("="*70)
print()

# Test 1: Verificar que config.py carga correctamente
print("✓ Test 1: Verificando config.py...")
try:
    from config import (
        TELEGRAM_TOKEN, TELEGRAM_ADMIN, CONTAINER_NAME,
        COMPOSE_STACKS_ENABLED, COMPOSE_STACKS_DIR, CALL_PATTERNS
    )
    print("  ✅ config.py cargado correctamente")
    print(f"     COMPOSE_STACKS_ENABLED: {COMPOSE_STACKS_ENABLED}")
    print(f"     COMPOSE_STACKS_DIR: {COMPOSE_STACKS_DIR}")

    # Verificar que los nuevos CALL_PATTERNS están definidos
    stack_patterns = [
        "listStacks", "stackInfo", "stackStart", "stackStop",
        "stackRestart", "stackUpdate", "stackLogs",
        "confirmStackStart", "confirmStackStop",
        "confirmStackRestart", "confirmStackUpdate"
    ]

    for pattern in stack_patterns:
        if pattern not in CALL_PATTERNS:
            print(f"  ❌ ERROR: Pattern '{pattern}' no encontrado en CALL_PATTERNS")
            sys.exit(1)

    print(f"  ✅ Todos los patterns de stacks definidos correctamente ({len(stack_patterns)} patterns)")

except Exception as e:
    print(f"  ❌ ERROR cargando config.py: {e}")
    sys.exit(1)

print()

# Test 2: Verificar que docker_compose_manager.py existe y es válido
print("✓ Test 2: Verificando docker_compose_manager.py...")
try:
    from docker_compose_manager import DockerComposeManager
    print("  ✅ docker_compose_manager.py importado correctamente")

    # Crear instancia de prueba
    test_dir = tempfile.mkdtemp()
    manager = DockerComposeManager(test_dir)
    print(f"  ✅ DockerComposeManager instanciado correctamente")

    # Verificar métodos principales
    methods = [
        'scan_stacks_directory', 'detect_running_stacks', 'list_all_stacks',
        'get_stack_info', 'validate_compose_file', 'stack_start',
        'stack_stop', 'stack_restart', 'stack_update', 'stack_logs'
    ]

    for method in methods:
        if not hasattr(manager, method):
            print(f"  ❌ ERROR: Método '{method}' no encontrado")
            sys.exit(1)

    print(f"  ✅ Todos los métodos principales disponibles ({len(methods)} métodos)")

except Exception as e:
    print(f"  ❌ ERROR con docker_compose_manager.py: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 3: Verificar sintaxis del archivo principal (sin ejecutarlo completamente)
print("✓ Test 3: Verificando sintaxis de docker-controller-bot.py...")
try:
    import ast
    with open('docker-controller-bot.py', 'r') as f:
        code = f.read()

    # Parsear el código para verificar sintaxis
    ast.parse(code)
    print("  ✅ Sintaxis correcta en docker-controller-bot.py")

    # Verificar que los comandos de stacks están en el código
    if '/stacks' not in code:
        print("  ❌ ERROR: Comando /stacks no encontrado en el código")
        sys.exit(1)
    print("  ✅ Comando /stacks encontrado")

    # Verificar que los callbacks de stacks están implementados
    stack_commands = [
        'listStacks', 'stackInfo', 'stackStart', 'stackStop',
        'stackRestart', 'stackUpdate', 'stackLogs'
    ]

    missing_commands = []
    for cmd in stack_commands:
        if f'elif comando == "{cmd}"' not in code:
            missing_commands.append(cmd)

    if missing_commands:
        print(f"  ❌ ERROR: Callbacks no encontrados: {missing_commands}")
        sys.exit(1)

    print(f"  ✅ Todos los callbacks de stacks implementados ({len(stack_commands)} callbacks)")

    # Verificar imports
    if 'from docker_compose_manager import DockerComposeManager' not in code:
        print("  ❌ ERROR: Import de DockerComposeManager no encontrado")
        sys.exit(1)
    print("  ✅ Import de DockerComposeManager correcto")

    # Verificar instanciación condicional
    if 'if COMPOSE_STACKS_ENABLED:' not in code or 'compose_manager = DockerComposeManager' not in code:
        print("  ❌ ERROR: Instanciación condicional de compose_manager no encontrada")
        sys.exit(1)
    print("  ✅ Instanciación condicional de compose_manager correcta")

except SyntaxError as e:
    print(f"  ❌ ERROR DE SINTAXIS en docker-controller-bot.py: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
except Exception as e:
    print(f"  ❌ ERROR verificando docker-controller-bot.py: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 4: Verificar que el archivo de tests existe y es válido
print("✓ Test 4: Verificando test_docker_compose_stacks.py...")
try:
    with open('tests/test_docker_compose_stacks.py', 'r') as f:
        test_code = f.read()

    ast.parse(test_code)
    print("  ✅ test_docker_compose_stacks.py tiene sintaxis correcta")

    # Contar número de tests
    test_count = test_code.count('def test_')
    print(f"  ✅ {test_count} tests definidos")

except Exception as e:
    print(f"  ❌ ERROR con test_docker_compose_stacks.py: {e}")
    sys.exit(1)

print()
print("="*70)
print("✅ TODOS LOS TESTS DE INTEGRACIÓN PASARON")
print("="*70)
print()
print("Resumen:")
print("  • config.py: OK - Variables y patterns configurados")
print("  • docker_compose_manager.py: OK - Clase y métodos disponibles")
print("  • docker-controller-bot.py: OK - Sintaxis correcta, comandos implementados")
print("  • test_docker_compose_stacks.py: OK - Tests definidos")
print()
print("📝 Siguiente paso: Ejecutar tests completos con:")
print("   ./tests/run_all_tests.sh")
print()
print("🚀 Para probar en ambiente real:")
print("   1. Configurar COMPOSE_STACKS_ENABLED=1 en docker-compose")
print("   2. Configurar COMPOSE_STACKS_DIR (default: /srv/stacks)")
print("   3. Reiniciar el bot")
print()
