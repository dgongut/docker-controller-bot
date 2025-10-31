#!/usr/bin/env python3
"""
Test de modo desarrollo - Verificación de arranque del bot
Este test simula el arranque del bot en modo desarrollo
"""
import os
import sys
# Añadir el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

print("="*70)
print("TEST DE MODO DESARROLLO")
print("="*70)
print()

# Configurar variables de entorno como en desarrollo
os.environ['TELEGRAM_TOKEN'] = 'test_token_dev'
os.environ['TELEGRAM_ADMIN'] = '12345'
os.environ['CONTAINER_NAME'] = 'docker-controller-bot-dev'
os.environ['TZ'] = 'Europe/Madrid'
os.environ['CHECK_UPDATES'] = '0'
os.environ['CHECK_UPDATE_EVERY_HOURS'] = '4'
os.environ['CHECK_UPDATE_STOPPED_CONTAINERS'] = '0'
os.environ['LANGUAGE'] = 'ES'
os.environ['EXTENDED_MESSAGES'] = '0'
os.environ['BUTTON_COLUMNS'] = '2'
os.environ['COMPOSE_STACKS_ENABLED'] = '1'
os.environ['COMPOSE_STACKS_DIR'] = '/tmp/test_stacks_dev'
os.environ['COMPOSE_STACKS_FORCE_RECREATE'] = '1'

print("✓ Variables de entorno configuradas")
print()

# Test 1: Verificar que config.py carga correctamente
print("Test 1: Cargando config.py...")
try:
    from config import (
        TELEGRAM_TOKEN, TELEGRAM_ADMIN, CONTAINER_NAME,
        COMPOSE_STACKS_ENABLED, COMPOSE_STACKS_DIR,
        COMPOSE_STACKS_FORCE_RECREATE,
        LABEL_STACK_NO_FORCE_RECREATE
    )

    assert TELEGRAM_TOKEN == 'test_token_dev'
    assert COMPOSE_STACKS_ENABLED == True
    assert COMPOSE_STACKS_FORCE_RECREATE == True
    assert LABEL_STACK_NO_FORCE_RECREATE == "DCB-Stack-No-Force-Recreate"

    print("  ✅ config.py cargado correctamente")
    print(f"     COMPOSE_STACKS_ENABLED: {COMPOSE_STACKS_ENABLED}")
    print(f"     COMPOSE_STACKS_FORCE_RECREATE: {COMPOSE_STACKS_FORCE_RECREATE}")
    print(f"     Label configurado: {LABEL_STACK_NO_FORCE_RECREATE}")
except Exception as e:
    print(f"  ❌ ERROR: {e}")
    sys.exit(1)

print()

# Test 2: Verificar que docker_compose_manager.py carga con config
print("Test 2: Cargando docker_compose_manager.py...")
try:
    from docker_compose_manager import DockerComposeManager

    test_dir = tempfile.mkdtemp()
    manager = DockerComposeManager(test_dir)

    print("  ✅ DockerComposeManager instanciado")

    # Verificar que el método stack_update tiene el parámetro force_recreate
    import inspect
    sig = inspect.signature(manager.stack_update)
    params = list(sig.parameters.keys())

    if 'force_recreate' not in params:
        print("  ❌ ERROR: Método stack_update no tiene parámetro force_recreate")
        sys.exit(1)

    print(f"  ✅ Método stack_update tiene parámetros: {params}")

    # Verificar método _should_force_recreate
    if not hasattr(manager, '_should_force_recreate'):
        print("  ❌ ERROR: Método _should_force_recreate no existe")
        sys.exit(1)

    print("  ✅ Método _should_force_recreate existe")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 3: Verificar que docker-controller-bot.py tiene sintaxis correcta
print("Test 3: Verificando docker-controller-bot.py...")
try:
    import ast

    with open('docker-controller-bot.py', 'r') as f:
        bot_code = f.read()

    # Verificar sintaxis
    ast.parse(bot_code)
    print("  ✅ Sintaxis correcta")

    # Verificar imports de compose_manager
    if 'from docker_compose_manager import DockerComposeManager' not in bot_code:
        print("  ❌ ERROR: Import de DockerComposeManager no encontrado")
        sys.exit(1)

    print("  ✅ Import de DockerComposeManager presente")

    # Verificar instanciación condicional
    if 'if COMPOSE_STACKS_ENABLED:' not in bot_code:
        print("  ❌ ERROR: Instanciación condicional no encontrada")
        sys.exit(1)

    if 'compose_manager = DockerComposeManager(COMPOSE_STACKS_DIR)' not in bot_code:
        print("  ❌ ERROR: Instanciación de compose_manager no encontrada")
        sys.exit(1)

    print("  ✅ Instanciación condicional correcta")

    # Verificar comando /stacks
    if '/stacks' not in bot_code:
        print("  ❌ ERROR: Comando /stacks no encontrado")
        sys.exit(1)

    print("  ✅ Comando /stacks implementado")

    # Verificar callbacks de stacks
    stack_callbacks = [
        'listStacks', 'stackInfo', 'stackStart', 'stackStop',
        'stackRestart', 'stackUpdate', 'stackLogs'
    ]

    missing = []
    for callback in stack_callbacks:
        if f'elif comando == "{callback}"' not in bot_code:
            missing.append(callback)

    if missing:
        print(f"  ❌ ERROR: Callbacks no encontrados: {missing}")
        sys.exit(1)

    print(f"  ✅ Todos los callbacks implementados ({len(stack_callbacks)})")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 4: Simular imports como lo haría el bot al iniciar
print("Test 4: Simulando inicio del bot (imports principales)...")
try:
    # Estos son los imports que hace el bot al iniciar
    import docker
    import hashlib
    import io
    import json
    import pickle
    import re
    import requests
    import telebot
    import threading
    import time
    import uuid
    import yaml
    from croniter import croniter
    from datetime import datetime

    print("  ✅ Todos los imports estándar cargados")

    # Import condicional como en el bot
    if COMPOSE_STACKS_ENABLED:
        from docker_compose_manager import DockerComposeManager
        print("  ✅ DockerComposeManager importado (COMPOSE_STACKS_ENABLED=True)")

except Exception as e:
    print(f"  ❌ ERROR en imports: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()

# Test 5: Verificar archivos de desarrollo necesarios
print("Test 5: Verificando archivos de desarrollo...")
dev_files = {
    'docker-compose.debug.yaml': 'Archivo de compose para desarrollo',
    'Dockerfile_debug': 'Dockerfile para debugging',
    '.env-example': 'Ejemplo de variables de entorno',
}

for filename, description in dev_files.items():
    if os.path.exists(filename):
        print(f"  ✅ {filename}: {description}")
    else:
        print(f"  ⚠️  {filename}: No encontrado (opcional)")

print()

# Test 6: Verificar que los locale files tienen las claves básicas
print("Test 6: Verificando archivos de locale...")
try:
    locale_files = ['locale/es.json', 'locale/en.json']

    for locale_file in locale_files:
        if os.path.exists(locale_file):
            with open(locale_file, 'r', encoding='utf-8') as f:
                locale_data = json.load(f)

            # Verificar que es un dict válido
            if not isinstance(locale_data, dict):
                print(f"  ❌ {locale_file}: Formato inválido")
                sys.exit(1)

            print(f"  ✅ {locale_file}: {len(locale_data)} claves")
        else:
            print(f"  ⚠️  {locale_file}: No encontrado")

except Exception as e:
    print(f"  ❌ ERROR: {e}")
    sys.exit(1)

print()

# Resumen
print("="*70)
print("✅ TODOS LOS TESTS DE MODO DESARROLLO PASARON")
print("="*70)
print()
print("El bot está listo para ejecutarse en modo desarrollo con:")
print()
print("1. Crea archivo .env:")
print("   cp .env-example .env")
print("   # Edita .env con tus valores de TELEGRAM_TOKEN y TELEGRAM_ADMIN")
print()
print("2. Añade las variables de stacks al .env:")
print("   COMPOSE_STACKS_ENABLED=1")
print("   COMPOSE_STACKS_DIR=/tmp/demo_stacks")
print("   COMPOSE_STACKS_FORCE_RECREATE=1")
print()
print("3. Crea stacks de demo:")
print("   ./create_demo_stacks.sh /tmp/demo_stacks")
print()
print("4. Levanta el bot en modo debug:")
print("   docker compose -f docker-compose.debug.yaml up -d --build --force-recreate")
print()
print("5. Ver logs:")
print("   docker logs -f docker-controller-bot-dev")
print()
print("6. Probar desde Telegram:")
print("   /stacks")
print()
print("7. Para detener:")
print("   docker compose -f docker-compose.debug.yaml down")
print()
print("="*70)
print()
print("📝 Notas:")
print("  • Los cambios en el código se refrescan en caliente")
print("  • El debugger está en puerto 5678 para VS Code")
print("  • Usa un TELEGRAM_TOKEN diferente al de producción")
print()
