#!/usr/bin/env bash
# mailroom-reloaded Docker build and verification script
# Validates Dockerfiles and docker-compose configurations before deployment

set -e

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy" && pwd)"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)"

echo "🔍 mailroom-reloaded Docker Validation"
echo "======================================"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check Docker installation
echo ""
echo "📋 Checking prerequisites..."
if ! command -v docker &> /dev/null; then
    echo -e "${RED}✗ Docker not found. Install from https://www.docker.com/products/docker-desktop${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker${NC} $(docker --version | awk '{print $3}' | tr -d ',')"

if docker compose version > /dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose > /dev/null 2>&1 && docker-compose version > /dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo -e "${RED}✗ Docker Compose not found${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker Compose${NC} $("${COMPOSE[@]}" version --short)"

failures=0

# Validate Dockerfiles
echo ""
echo "📝 Validating Dockerfiles..."
for dockerfile in "$DEPLOY_DIR"/Dockerfile "$DEPLOY_DIR"/Dockerfile.dev "$DEPLOY_DIR"/Dockerfile.sandbox; do
    name=$(basename "$dockerfile")
    if [ ! -f "$dockerfile" ]; then
        echo -e "${RED}✗ $name${NC} (missing file)"
        failures=$((failures + 1))
        continue
    fi
    # Basic syntax check: ensure key directives are present
    if grep -q "^FROM" "$dockerfile" && grep -q "^COPY\|^RUN" "$dockerfile"; then
        echo -e "${GREEN}✓ $name${NC}"
    else
        echo -e "${RED}✗ $name${NC} (missing key directives)"
        failures=$((failures + 1))
    fi
done

# Validate docker-compose files
echo ""
echo "🐳 Validating docker-compose files..."
for compose in "$DEPLOY_DIR"/docker-compose.yml "$DEPLOY_DIR"/docker-compose.dev.yml "$DEPLOY_DIR"/docker-compose.sandbox.yml; do
    name=$(basename "$compose")
    if [ ! -f "$compose" ]; then
        echo -e "${RED}✗ $name${NC} (missing file)"
        failures=$((failures + 1))
        continue
    fi
    # Synthetic values satisfy production's required variables for config checks
    # only. Suppress rendered configuration and errors that may include secrets.
    if MAILROOM_API_TOKEN=validation-only GRAFANA_ADMIN_PASSWORD=validation-only \
        "${COMPOSE[@]}" -f "$compose" config --quiet > /dev/null 2>&1; then
        echo -e "${GREEN}✓ $name${NC}"
    else
        echo -e "${RED}✗ $name${NC} (Compose configuration validation failed)"
        failures=$((failures + 1))
    fi
done

# Check .env.example
echo ""
echo "⚙️  Checking environment file..."
if [ -f "$PROJECT_ROOT/.env.example" ]; then
    vars=$(grep -c "^[A-Z_].*=" "$PROJECT_ROOT/.env.example" || true)
    echo -e "${GREEN}✓ .env.example${NC} ($vars environment variables)"
else
    echo -e "${YELLOW}⚠ .env.example${NC} (using defaults)"
fi

# Summary
echo ""
echo "======================================"
if [ "$failures" -ne 0 ]; then
    echo -e "${RED}✗ $failures validation(s) failed.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ All validations passed!${NC}"
echo ""
echo "🚀 Next steps:"
echo "  1. Copy .env.example to .env:"
echo "     cp .env.example .env"
echo ""
echo "  2. Edit .env with your settings:"
echo "     MAILROOM_API_TOKEN=your-secret-token"
echo "     GRAFANA_ADMIN_PASSWORD=your-password"
echo "     DEFAULT_PROVIDER=mock  # or openrouter, vllm, llamafile"
echo ""
echo "  3. Build and start development stack:"
echo "     docker compose -f deploy/docker-compose.dev.yml up -d --build"
echo ""
echo "  4. Verify services are running:"
echo "     docker compose -f deploy/docker-compose.dev.yml ps"
echo ""
echo "  5. Access the UI:"
echo "     open http://127.0.0.1:8000/ui"
echo ""
echo "📚 For more info, see DOCKER_DEPLOYMENT.md"
