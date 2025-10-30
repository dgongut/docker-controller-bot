# Tests - Docker Compose Stacks

Este directorio contiene todos los tests para la funcionalidad de Docker Compose Stacks.

## Estructura

```
tests/
├── README.md                          # Este archivo
├── run_all_tests.sh                   # Script para ejecutar todos los tests
├── create_demo_stacks.sh              # Script para crear stacks de demostración
├── test_integration.py                # Tests de integración (sintaxis, imports)
├── test_dev_mode.py                   # Tests de modo desarrollo
├── test_functional.py                 # Tests funcionales con stacks reales
└── test_docker_compose_stacks.py      # Tests unitarios completos (14 tests)
```

## Ejecutar Tests

### Opción 1: Todos los tests (Recomendado)

```bash
# Desde el directorio raíz del proyecto
./tests/run_all_tests.sh
```

Este script:
- ✅ Verifica el entorno virtual
- ✅ Crea stacks de demostración si no existen
- ✅ Ejecuta las 4 suites de tests en orden
- ✅ Muestra un resumen completo

### Opción 2: Tests individuales

Desde el directorio raíz del proyecto:

```bash
# Activar entorno virtual
source venv/bin/activate

# Tests de integración (rápidos)
python3 tests/test_integration.py

# Tests de modo desarrollo
python3 tests/test_dev_mode.py

# Tests funcionales (requiere demo stacks)
./tests/create_demo_stacks.sh /tmp/demo_stacks
python3 tests/test_functional.py

# Tests unitarios (completos)
python3 tests/test_docker_compose_stacks.py
```

## Cobertura de Tests

### Test Suite 1: Integración (test_integration.py)
- ✅ Verificación de sintaxis de archivos
- ✅ Imports correctos
- ✅ Compatibilidad entre módulos
- ✅ Configuración de variables

### Test Suite 2: Modo Desarrollo (test_dev_mode.py)
- ✅ Carga de config.py
- ✅ Instanciación de DockerComposeManager
- ✅ Verificación de docker-controller-bot.py
- ✅ Imports principales del bot
- ✅ Archivos de desarrollo
- ✅ Archivos de locale

### Test Suite 3: Funcionales (test_functional.py)
- ✅ Detección de stacks de demo
- ✅ Validación de archivos compose
- ✅ Información detallada de stacks
- ✅ Listado completo
- ✅ Métodos de operación

### Test Suite 4: Unitarios (test_docker_compose_stacks.py)
**14 tests completos:**

1. **test_01**: Escanear directorio vacío
2. **test_02**: Detección de stacks en directorio
3. **test_03**: Detección de stacks corriendo por labels
4. **test_04**: Obtener información de stack
5. **test_05**: Validación de archivos compose
6. **test_06**: Iniciar stack
7. **test_07**: Detener stack
8. **test_08**: Reiniciar stack
9. **test_09**: Actualizar stack (con --force-recreate)
10. **test_09b**: Actualizar stack sin force-recreate
11. **test_09c**: Actualizar stack con label DCB-Stack-No-Force-Recreate
12. **test_10**: Listar todos los stacks
13. **test_11**: Escenario Pihole (issue #66)
14. **test_12**: Stack multi-servicio

## Feature: --force-recreate

Los tests validan la configuración de `--force-recreate` con 3 niveles de prioridad:

1. **Parámetro explícito**: `stack_update(force_recreate=True/False)`
2. **Label**: `DCB-Stack-No-Force-Recreate` (para deshabilitar)
3. **Variable global**: `COMPOSE_STACKS_FORCE_RECREATE` (default: 1)

Por defecto, `--force-recreate` está **habilitado** para solucionar el problema de Pihole (issue #66).

## Requisitos

```bash
# Python 3.8+
pip install -r requirements.txt
```

**Dependencias principales:**
- docker
- pyyaml
- unittest (built-in)

## Crear Stacks de Demostración

```bash
./tests/create_demo_stacks.sh /tmp/demo_stacks
```

Esto crea 3 stacks de ejemplo:
- **nginx-demo**: Servidor web simple
- **redis-demo**: Cache Redis
- **webapp-demo**: Stack multi-servicio (web + db)

## Resultados Esperados

```
✅ Tests de Integración: PASSED
✅ Tests de Modo Desarrollo: PASSED
✅ Tests Funcionales: PASSED
✅ Tests Unitarios: PASSED (14/14)

Total: 4/4 suites exitosas
```

## Troubleshooting

### Error: ModuleNotFoundError

```bash
# Asegúrate de tener el venv activado
source venv/bin/activate
pip install -r requirements.txt
```

### Error: No demo stacks found

```bash
# Crea los stacks de demo
./tests/create_demo_stacks.sh /tmp/demo_stacks
```

### Tests fallan por permisos

```bash
# Asegúrate de que los scripts son ejecutables
chmod +x tests/run_all_tests.sh
chmod +x tests/create_demo_stacks.sh
```

## CI/CD

Para integración continua, ejecuta:

```bash
./tests/run_all_tests.sh
```

El script retorna:
- `0` si todos los tests pasan
- `1` si algún test falla

## Desarrollo

### Añadir nuevos tests

1. Crea un nuevo archivo `test_*.py` en este directorio
2. Sigue la estructura de los tests existentes
3. Añádelo a `run_all_tests.sh` si es necesario

### Convenciones

- Usa `unittest` como framework
- Mockea Docker SDK cuando sea posible
- Nombres de test descriptivos: `test_XX_descripcion`
- Incluye prints para seguimiento: `print("✓ Test XX: Descripción OK")`

## Referencias

- [README.md](../README.md) - README principal del proyecto con guía de Docker Compose Stacks
