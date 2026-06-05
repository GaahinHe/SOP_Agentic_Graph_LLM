#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# start_services.sh - Start pipeline services with pre-flight checks
# Target: RHEL 8/9, Docker 19.03.15+, NVIDIA Tesla L2

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_step() { echo -e "${CYAN}[STEP]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-pipeline}"

# Load .env if exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env" 2>/dev/null || true
    set +a
fi

# Pre-flight checks (quick version)
preflight_check() {
    log_step "Running pre-flight checks..."

    # Check Docker is running
    if ! docker info &>/dev/null; then
        log_fail "Docker daemon is not running"
        exit 1
    fi

    # Check docker-compose is available
    if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null; then
        log_fail "docker-compose not found"
        exit 1
    fi

    # Check NVIDIA runtime if GPU enabled
    if [ "${ENABLE_GPU:-true}" = "true" ]; then
        if ! docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu20.04 nvidia-smi &>/dev/null; then
            log_warn "NVIDIA runtime test failed - GPU services may not start"
        else
            log_ok "NVIDIA runtime: functional"
        fi
    fi

    log_ok "Pre-flight checks passed"
}

# Wait for service health
wait_for_health() {
    local service=$1
    local port=$2
    local path="${3:-/health}"
    local retries="${4:-20}"
    local interval=5

    log_info "Waiting for $service to be healthy (port $port)..."

    for i in $(seq 1 "$retries"); do
        if timeout 5 curl -sf "http://localhost:$port$path" &>/dev/null; then
            log_ok "$service is healthy"
            return 0
        fi
        sleep $interval
    done

    log_warn "$service health check timeout after ${retries} attempts"
    return 1
}

# Start infrastructure services first
start_infrastructure() {
    log_step "Starting infrastructure services..."

    docker-compose up -d minio postgres redis qdrant neo4j

    log_info "Waiting for infrastructure to be healthy..."

    wait_for_health "minio" 9000 "/minio/health/live" 30 5

    # PostgreSQL - use pg_isready
    local pg_retries=30
    for i in $(seq 1 $pg_retries); do
        if docker exec pipeline-postgres pg_isready -U "${POSTGRES_USER:-pipeline_user}" &>/dev/null; then
            log_ok "PostgreSQL is healthy"
            break
        fi
        sleep 2
    done

    wait_for_health "redis" 6379 "/" 20 3
    wait_for_health "qdrant" 6333 "/readyz" 30 5

    # Neo4j - give it more time
    local neo4j_retries=40
    for i in $(seq 1 $neo4j_retries); do
        if timeout 5 curl -sf "http://localhost:7474" &>/dev/null; then
            log_ok "Neo4j is healthy"
            break
        fi
        sleep 3
    done

    log_ok "Infrastructure services started"
}

# Start document processing services
start_document_processing() {
    log_step "Starting document processing services..."

    # Start mineru-core first (model inference backend)
    docker-compose up -d mineru-core
    wait_for_health "mineru-core" 8003 "/health" 60 10

    # Then mineru-wrapper (HTTP API frontend)
    docker-compose up -d mineru-wrapper
    wait_for_health "mineru-wrapper" 8002 "/health" 40 5

    # Then mymupdf
    docker-compose up -d mymupdf
    wait_for_health "mymupdf" 8001 "/health" 40 5

    log_ok "Document processing services started"
}

# Start knowledge graph services
start_knowledge_graph() {
    log_step "Starting knowledge graph services..."

    docker-compose up -d graphify

    wait_for_health "graphify" 8004 "/health" 40 5

    log_ok "Knowledge graph services started"
}

# Start RAG and Wiki services
start_rag_services() {
    log_step "Starting RAG and Wiki services..."

    docker-compose up -d agentic-rag agentic-wiki

    wait_for_health "agentic-rag" 8005 "/health" 40 5
    wait_for_health "agentic-wiki" 8006 "/health" 40 5

    log_ok "RAG and Wiki services started"
}

# Print access information
print_access_info() {
    echo ""
    echo "======================================================================"
    echo -e "${GREEN}  Pipeline services started successfully!${NC}"
    echo "======================================================================"
    echo ""
    echo "Service endpoints:"
    echo "  MinIO API:        http://localhost:9000"
    echo "  MinIO Console:    http://localhost:9001  (user: ${MINIO_ROOT_USER:-minioadmin})"
    echo "  PostgreSQL:       localhost:5432  (db: ${POSTGRES_DB:-agentic_graph})"
    echo "  Redis:            localhost:6379"
    echo "  Qdrant:           http://localhost:6333"
    echo "  Neo4j:            http://localhost:7474"
    echo ""
    echo "Microservices:"
    echo "  MyMuPDF:          http://localhost:8001"
    echo "  MinerU Wrapper:   http://localhost:8002"
    echo "  MinerU Core:      http://localhost:8003"
    echo "  Graphify:         http://localhost:8004"
    echo "  Agentic RAG:      http://localhost:8005"
    echo "  Agentic Wiki:     http://localhost:8006"
    echo ""
    echo "Default credentials (change in .env):"
    echo "  MinIO:           minioadmin / changeme"
    echo "  PostgreSQL:      pipeline_user / changeme"
    echo "  Neo4j:           neo4j / changeme"
    echo "  Grafana:         admin / changeme"
    echo ""
    echo "To view logs:    docker-compose logs -f"
    echo "To stop:         make stop"
    echo "======================================================================"
}

# Main
main() {
    echo ""
    echo "======================================================================"
    echo "  Starting Agentic Graph LLM Pipeline"
    echo "  Project: $COMPOSE_PROJECT_NAME"
    echo "======================================================================"
    echo ""

    cd "$PROJECT_ROOT"

    # Check if .env exists
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        if [ -f "$PROJECT_ROOT/.env.example" ]; then
            log_info "Creating .env from .env.example..."
            cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
            log_warn "Please edit .env with your configuration!"
        else
            log_fail ".env.example not found"
            exit 1
        fi
    fi

    preflight_check

    echo ""
    start_infrastructure

    echo ""
    start_document_processing

    echo ""
    start_knowledge_graph

    echo ""
    start_rag_services

    echo ""
    print_access_info
}

main "$@"