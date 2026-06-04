#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# start_services.sh - Start pipeline services with health checks
# Target: RHEL 8/9, Docker 19.03.15+

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-pipeline}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; }

# Check prerequisites before starting
check_prereqs() {
    info "Running prerequisite checks..."
    if ! bash "$SCRIPT_DIR/check_prerequisites.sh" --fast; then
        fail "Prerequisites check failed. Fix issues before starting."
        exit 1
    fi
    ok "Prerequisites OK"
}

# Wait for service health
wait_for_health() {
    local service="$1"
    local port="$2"
    local max_wait="${3:-300}"
    local interval="${4:-5}"

    info "Waiting for $service to be healthy (port $port)..."
    local elapsed=0
    while [ $elapsed -lt $max_wait ]; do
        if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
            ok "$service is healthy"
            return 0
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
        echo -n "."
    done
    echo ""
    fail "$service failed to become healthy after ${max_wait}s"
    return 1
}

# Start infrastructure services first
start_infrastructure() {
    info "Starting infrastructure services..."
    docker-compose up -d minio postgres redis qdrant neo4j
    info "Waiting for infrastructure to be ready..."

    wait_for_health "minio"    "9000" 120 5
    wait_for_health "postgres" "5432" 120 5
    wait_for_health "redis"    "6379"  60 3
    wait_for_health "qdrant"   "6333" 120 5
    wait_for_health "neo4j"    "7474" 180 5

    ok "Infrastructure services are healthy"
}

# Start document processing services
start_document_processing() {
    info "Starting document processing services..."

    # Start mineru-core first (model inference backend)
    docker-compose up -d mineru-core
    wait_for_health "mineru-core" "8003" 300 10

    # Then mineru-wrapper (HTTP API frontend)
    docker-compose up -d mineru-wrapper
    wait_for_health "mineru-wrapper" "8002" 180 5

    # Then mymupdf
    docker-compose up -d mymupdf
    wait_for_health "mymupdf" "8001" 180 5

    ok "Document processing services are healthy"
}

# Start knowledge graph service
start_graphify() {
    info "Starting knowledge graph service..."
    docker-compose up -d graphify
    wait_for_health "graphify" "8004" 180 5
    ok "Graphify is healthy"
}

# Start agentic services
start_agentic() {
    info "Starting agentic services..."
    docker-compose up -d agentic-rag agentic-wiki
    wait_for_health "agentic-rag"  "8005" 180 5
    wait_for_health "agentic-wiki"  "8006" 180 5
    ok "Agentic services are healthy"
}

# Print access info
print_access_info() {
    echo ""
    echo "======================================================================"
    echo "  Pipeline Services Started Successfully!"
    echo "======================================================================"
    echo ""
    echo "Service Access Points:"
    echo ""
    echo "  Infrastructure:"
    echo "    MinIO Console:  http://localhost:9001  (user: ${MINIO_ROOT_USER:-minioadmin})"
    echo "    MinIO API:       http://localhost:9000"
    echo "    PostgreSQL:      localhost:5432         (db: ${POSTGRES_DB:-agentic_graph})"
    echo "    Redis:           localhost:6379"
    echo "    Qdrant:          http://localhost:6333"
    echo "    Neo4j Browser:   http://localhost:7474"
    echo ""
    echo "  Document Processing:"
    echo "    MyMuPDF:        http://localhost:8001"
    echo "    MinerU Wrapper:  http://localhost:8002"
    echo "    MinerU Core:     http://localhost:8003"
    echo ""
    echo "  Knowledge & Agents:"
    echo "    Graphify:       http://localhost:8004"
    echo "    Agentic RAG:     http://localhost:8005"
    echo "    Agentic Wiki:    http://localhost:8006"
    echo ""
    echo "======================================================================"
    echo ""
    echo "Useful commands:"
    echo "  make status          - Show container status"
    echo "  make logs            - Tail logs"
    echo "  make logs SERVICE=x  - Logs for specific service"
    echo "  make stop            - Stop all services"
    echo ""
}

# Main
main() {
    cd "$PROJECT_ROOT"

    # Check if .env exists
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        warn ".env not found. Copying from .env.example..."
        cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
        warn "Please edit .env with your configuration before continuing!"
        exit 1
    fi

    # Source .env for access info
    set -a
    source "$PROJECT_ROOT/.env"
    set +a

    check_prereqs
    start_infrastructure
    start_document_processing
    start_graphify
    start_agentic
    print_access_info

    ok "All services started!"
}

main "$@"