#!/bin/bash
# Script para ejecutar todos los tests del proyecto

set -e  # Salir si algún comando falla

echo "========================================================================"
echo "EJECUTANDO TODOS LOS TESTS"
echo "========================================================================"
echo ""

# Colores para output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Contador de tests
TESTS_PASSED=0
TESTS_FAILED=0

# Función para ejecutar un test
run_test() {
    local test_name="$1"
    local test_file="$2"

    echo "────────────────────────────────────────────────────────────────────────"
    echo "🧪 Ejecutando: $test_name"
    echo "────────────────────────────────────────────────────────────────────────"

    if python3 "$test_file"; then
        echo -e "${GREEN}✅ $test_name: PASSED${NC}"
        ((TESTS_PASSED++))
        echo ""
        return 0
    else
        echo -e "${RED}❌ $test_name: FAILED${NC}"
        ((TESTS_FAILED++))
        echo ""
        return 1
    fi
}

# Cambiar al directorio raíz del proyecto
cd "$(dirname "$0")/.."

# Verificar que estamos en el directorio correcto
if [ ! -f "docker-controller-bot.py" ]; then
    echo -e "${RED}ERROR: No se encuentra docker-controller-bot.py${NC}"
    exit 1
fi

# Verificar que existe el venv
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}⚠️  Virtual environment no encontrado. Creando...${NC}"
    python3 -m venv venv
    source venv/bin/activate
    pip install -q -r requirements.txt
    echo -e "${GREEN}✅ Virtual environment creado${NC}"
    echo ""
else
    source venv/bin/activate
fi

# Verificar que los stacks de demo existen
if [ ! -d "/tmp/demo_stacks" ]; then
    echo -e "${YELLOW}⚠️  Stacks de demo no encontrados. Creando...${NC}"
    ./tests/create_demo_stacks.sh /tmp/demo_stacks > /dev/null
    echo -e "${GREEN}✅ Stacks de demo creados${NC}"
    echo ""
fi

echo ""
echo "Entorno verificado. Iniciando tests..."
echo ""
echo ""

# Ejecutar tests en orden
CONTINUE=true

# Test 1: Tests de Integración (más rápido)
if $CONTINUE; then
    run_test "Tests de Integración" "tests/test_integration.py" || CONTINUE=false
fi

# Test 2: Tests de Modo Desarrollo
if $CONTINUE; then
    run_test "Tests de Modo Desarrollo" "tests/test_dev_mode.py" || CONTINUE=false
fi

# Test 3: Tests Funcionales
if $CONTINUE; then
    run_test "Tests Funcionales" "tests/test_functional.py" || CONTINUE=false
fi

# Test 4: Tests Unitarios (más completo)
if $CONTINUE; then
    run_test "Tests Unitarios" "tests/test_docker_compose_stacks.py" || CONTINUE=false
fi

# Resumen
echo "========================================================================"
echo "RESUMEN DE TESTS"
echo "========================================================================"
echo ""

TOTAL_TESTS=$((TESTS_PASSED + TESTS_FAILED))

echo "Total de suites ejecutadas: $TOTAL_TESTS"
echo -e "${GREEN}Suites exitosas: $TESTS_PASSED${NC}"

if [ $TESTS_FAILED -gt 0 ]; then
    echo -e "${RED}Suites fallidas: $TESTS_FAILED${NC}"
else
    echo "Suites fallidas: 0"
fi

echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✅ TODOS LOS TESTS PASARON EXITOSAMENTE${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "📊 Detalles:"
    echo "  • Tests de Integración: ✅ Passing"
    echo "  • Tests de Desarrollo: ✅ Passing"
    echo "  • Tests Funcionales: ✅ Passing"
    echo "  • Tests Unitarios (14): ✅ Passing"
    echo ""
    echo "🚀 El código está listo para:"
    echo "  • Commit"
    echo "  • Pull Request"
    echo "  • Deployment"
    echo ""
    exit 0
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ ALGUNOS TESTS FALLARON${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "Por favor revisa los errores arriba y corrige antes de hacer commit."
    echo ""
    exit 1
fi
