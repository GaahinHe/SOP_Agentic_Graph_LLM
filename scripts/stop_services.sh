#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# stop_services.sh - Safely stop pipeline services
# Target: RHEL 8/9, Docker 19.03.15+

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-pipeline}"

GRACEFUL_TIMEOUT=60

# Load .env if exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env" 2>/dev/null || true
    set +a
fi

# Stop services in reverse dependency order
stop_agentic() {
    log_info "Stopping agentic services..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" stop -t 30 agentic-wiki agentic-rag 2>/dev/null || true
    log_ok "Agentic services stopped"
}

stop_graphify() {
    log_info "Stopping graphify..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" stop -t 30 graphify 2>/dev/null || true
    log_ok "Graphify stopped"
}

stop_document_processing() {
    log_info "Stopping document processing services..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" stop -t 60 mymupdf mineru-wrapper mineru-core 2>/dev/null || true
    log_ok "Document processing services stopped"
}

stop_infrastructure() {
    log_info "Stopping infrastructure services..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" stop -t 30 neo4j qdrant redis postgres minio 2>/dev/null || true
    log_ok "Infrastructure services stopped"
}

# Full cleanup (remove containers but not volumes)
cleanup() {
    log_info "Removing containers..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" rm -f --stop 2>/dev/null || true
    log_ok "Containers removed (volumes preserved)"
}

# Deep clean (remove everything including volumes)
deep_clean() {
    log_warn "This will remove ALL data volumes!"
    read -p "Are you sure? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Aborted"
        exit 0
    fi

    log_info "Removing containers and volumes..."
    docker-compose -p "$COMPOSE_PROJECT_NAME" down -v --remove-orphans 2>/dev/null || true
    log_ok "Deep clean complete - all volumes deleted"
}

# Force kill stuck containers
force_cleanup() {
    log_warn "Force cleaning up stuck containers..."
    docker ps -aq --filter "name=pipeline-" 2>/dev/null | xargs -r docker kill 2>/dev/null || true
    docker ps -aq --filter "name=pipeline-" 2>/dev/null | xargs -r docker rm -f 2>/dev/null || true
    docker images -q "pipeline-*" 2>/dev/null | xargs -r docker rmi -f 2>/dev/null || true
    log_ok "Force cleanup complete"
}

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --graceful    Graceful stop (default)"
    echo "  --cleanup     Stop and remove containers (keep volumes)"
    echo "  --deep        Full cleanup (remove containers AND volumes)"
    echo "  --force       Force kill stuck containers"
    echo "  --help        Show this help"
    echo ""
}

# Main
main() {
    local mode="${1:-}"

    cd "$PROJECT_ROOT"

    case "$mode" in
        --cleanup)
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            cleanup
            log_ok "Cleanup complete"
            ;;
        --deep)
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            deep_clean
            ;;
        --force)
            force_cleanup
            ;;
        --help)
            usage
            ;;
        *)
            log_info "Stopping pipeline services (graceful, ${GRACEFUL_TIMEOUT}s timeout)..."
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            log_ok "All services stopped gracefully"
            ;;
    esac

    echo ""
    echo "To start again: make start"
    echo ""
}

main "$@"