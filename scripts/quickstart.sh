#!/usr/bin/env bash
# Quick-start script for mailroom-reloaded development environment
# Handles .env setup, image building, and service launch

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$PROJECT_ROOT/deploy"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

show_help() {
    cat << EOF
mailroom-reloaded quick-start

Usage:  $(basename "$0") [COMMAND] [OPTIONS]

Commands:
  up              Start dev stack (default)
  down            Stop dev stack
  logs            Follow app logs
  status          Check service health
  reset           Stop, remove volumes, clear data
  
Options:
  --provider      LLM provider: mock, openrouter, vllm, llamafile (default: mock)
  --token         API token (auto-generated if not provided)
  --no-build      Skip image rebuild
  --help          Show this message

Examples:
  $(basename "$0") up                              # Start with mock provider
  $(basename "$0") up --provider vllm              # Start with vLLM
  $(basename "$0") down                            # Stop services
  $(basename "$0") logs                            # Follow logs
  $(basename "$0") reset                           # Full reset

EOF
}

init_env() {
    if [ -f "$PROJECT_ROOT/.env" ]; then
        echo -e "${YELLOW}⚠ .env already exists${NC}"
        return 0
    fi
    
    echo -e "${BLUE}Creating .env from .env.example${NC}"
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    
    # Set defaults
    local token="${API_TOKEN:-mailroom-dev-token-$(openssl rand -hex 8 2>/dev/null || date +%s)}"
    local provider="${PROVIDER:-mock}"
    
    # Update .env
    sed -i '' "s/DEFAULT_PROVIDER=mock/DEFAULT_PROVIDER=$provider/" "$PROJECT_ROOT/.env"
    sed -i '' "s/MAILROOM_API_TOKEN=$/MAILROOM_API_TOKEN=$token/" "$PROJECT_ROOT/.env"
    sed -i '' "s/GRAFANA_ADMIN_PASSWORD=$/GRAFANA_ADMIN_PASSWORD=admin/" "$PROJECT_ROOT/.env"
    
    echo -e "${GREEN}✓ .env created${NC}"
    echo "  Token: $token"
    echo "  Provider: $provider"
}

cmd_up() {
    local no_build="${NO_BUILD:-}"
    
    echo -e "${BLUE}🚀 Starting mailroom-reloaded dev stack${NC}"
    echo ""
    
    init_env
    
    local build_flag=""
    [ -z "$no_build" ] && build_flag="--build"
    
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" up -d $build_flag
    
    echo ""
    echo -e "${GREEN}✓ Stack started${NC}"
    echo ""
    echo "📍 Services:"
    echo "  API/UI:      http://127.0.0.1:8000"
    echo "  Phoenix:     http://127.0.0.1:6006"
    echo "  Prometheus:  http://127.0.0.1:9090"
    echo "  Grafana:     http://127.0.0.1:3000 (admin/admin)"
    echo ""
    echo "🔄 Health check:"
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" ps
}

cmd_down() {
    echo -e "${BLUE}🛑 Stopping mailroom-reloaded dev stack${NC}"
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" down
    echo -e "${GREEN}✓ Stack stopped${NC}"
}

cmd_logs() {
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" logs -f app
}

cmd_status() {
    echo -e "${BLUE}📊 Stack status${NC}"
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" ps
    
    echo ""
    echo -e "${BLUE}🏥 Health checks${NC}"
    
    local checks=(
        "app:http://127.0.0.1:8000/health"
        "phoenix:http://127.0.0.1:6006"
        "prometheus:http://127.0.0.1:9090/-/healthy"
        "grafana:http://127.0.0.1:3000/api/health"
    )
    
    for check in "${checks[@]}"; do
        local name="${check%%:*}"
        local url="${check##*:}"
        if curl -s "$url" > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} $name"
        else
            echo -e "${RED}✗${NC} $name"
        fi
    done
}

cmd_reset() {
    echo -e "${RED}⚠️  This will:"
    echo "   • Stop all containers"
    echo "   • Delete volumes (data)"
    echo "   • Remove .env file"
    echo "${NC}"
    read -p "Continue? (yes/no) " -r confirm
    
    if [ "$confirm" != "yes" ]; then
        echo "Cancelled"
        return 1
    fi
    
    echo "Resetting..."
    docker compose -f "$DEPLOY_DIR/docker-compose.dev.yml" down -v
    rm -rf "$PROJECT_ROOT/data"
    rm -f "$PROJECT_ROOT/.env"
    echo -e "${GREEN}✓ Reset complete${NC}"
}

# Parse arguments
COMMAND="${1:-up}"
shift || true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)
            PROVIDER="$2"
            shift 2
            ;;
        --token)
            API_TOKEN="$2"
            shift 2
            ;;
        --no-build)
            NO_BUILD=1
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

case "$COMMAND" in
    up)
        cmd_up
        ;;
    down)
        cmd_down
        ;;
    logs)
        cmd_logs
        ;;
    status)
        cmd_status
        ;;
    reset)
        cmd_reset
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
