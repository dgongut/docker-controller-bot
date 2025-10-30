#!/bin/bash
# Script para crear stacks de demostración
# Uso: ./create_demo_stacks.sh [directorio]

STACKS_DIR="${1:-/tmp/demo_stacks}"

echo "=========================================="
echo "Creando Stacks de Demostración"
echo "=========================================="
echo ""
echo "Directorio: $STACKS_DIR"
echo ""

# Crear directorio principal
mkdir -p "$STACKS_DIR"

# ==================== STACK 1: Nginx Simple ====================
echo "📦 Creando stack: nginx-demo"
mkdir -p "$STACKS_DIR/nginx-demo"
cat > "$STACKS_DIR/nginx-demo/docker-compose.yml" <<'EOF'
version: '3'
services:
  nginx:
    image: nginx:alpine
    container_name: nginx-demo
    ports:
      - "8080:80"
    restart: unless-stopped
EOF
echo "   ✅ Creado: $STACKS_DIR/nginx-demo/docker-compose.yml"
echo ""

# ==================== STACK 2: Redis ====================
echo "📦 Creando stack: redis-demo"
mkdir -p "$STACKS_DIR/redis-demo"
cat > "$STACKS_DIR/redis-demo/docker-compose.yml" <<'EOF'
version: '3'
services:
  redis:
    image: redis:alpine
    container_name: redis-demo
    ports:
      - "6379:6379"
    restart: unless-stopped
EOF
echo "   ✅ Creado: $STACKS_DIR/redis-demo/docker-compose.yml"
echo ""

# ==================== STACK 3: Aplicación Web con Base de Datos ====================
echo "📦 Creando stack: webapp-demo"
mkdir -p "$STACKS_DIR/webapp-demo"
cat > "$STACKS_DIR/webapp-demo/docker-compose.yml" <<'EOF'
version: '3'
services:
  web:
    image: nginx:alpine
    container_name: webapp-demo-web
    ports:
      - "8081:80"
    depends_on:
      - db
    restart: unless-stopped

  db:
    image: postgres:14-alpine
    container_name: webapp-demo-db
    environment:
      - POSTGRES_PASSWORD=demo123
      - POSTGRES_USER=demo
      - POSTGRES_DB=webapp
    restart: unless-stopped
EOF
echo "   ✅ Creado: $STACKS_DIR/webapp-demo/docker-compose.yml"
echo ""

# ==================== Resumen ====================
echo "=========================================="
echo "✅ Stacks de demostración creados"
echo "=========================================="
echo ""
echo "Estructura creada:"
tree -L 2 "$STACKS_DIR" 2>/dev/null || find "$STACKS_DIR" -type f
echo ""
echo "Para usar con docker-controller-bot:"
echo ""
echo "1. Configura en docker-compose.yml del bot:"
echo "   environment:"
echo "     - COMPOSE_STACKS_ENABLED=1"
echo "     - COMPOSE_STACKS_DIR=$STACKS_DIR"
echo ""
echo "2. Mapea el directorio:"
echo "   volumes:"
echo "     - $STACKS_DIR:$STACKS_DIR:ro"
echo ""
echo "3. Reinicia el bot y ejecuta /stacks en Telegram"
echo ""
echo "=========================================="
echo "Tests rápidos (sin bot):"
echo "=========================================="
echo ""
echo "# Iniciar stack de nginx:"
echo "docker compose -f $STACKS_DIR/nginx-demo/docker-compose.yml up -d"
echo ""
echo "# Ver stacks corriendo:"
echo "docker ps --filter label=com.docker.compose.project"
echo ""
echo "# Detener stack:"
echo "docker compose -f $STACKS_DIR/nginx-demo/docker-compose.yml down"
echo ""
echo "=========================================="
