#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# stop_services.sh - Safely stop pipeline services
# Target: RHEL 8/9, Docker 19.03.15+

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-pipeline}"

# Colors
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; }

# Graceful shutdown with timeout
GRACEFUL_TIMEOUT=60

# Stop services in reverse dependency order
stop_agentic() {
    info "Stopping agentic services..."
    docker-compose stop -t 30 agentic-wiki agentic-rag 2>/dev/null || true
    ok "Agentic services stopped"
}

stop_graphify() {
    info "Stopping graphify..."
    docker-compose stop -t 30 graphify 2>/dev/null || true
    ok "Graphify stopped"
}

stop_document_processing() {
    info "Stopping document processing services..."
    docker-compose stop -t 60 mymupdf mineru-wrapper mineru-core 2>/dev/null || true
    ok "Document processing services stopped"
}

stop_infrastructure() {
    info "Stopping infrastructure services..."
    docker-compose stop -t 30 neo4j qdrant redis postgres minio 2>/dev/null || true
    ok "Infrastructure services stopped"
}

# Full cleanup (remove containers but not volumes)
cleanup() {
    info "Removing containers..."
    docker-compose rm -f --stop 2>/dev/null || true
    ok "Containers removed (volumes preserved)"
}

# Deep clean (remove everything including volumes)
deep_clean() {
    warn "This will remove ALL data volumes!"
    read -p "Are you sure? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        info "Aborted"
        exit 0
    fi

    info "Removing containers and volumes..."
    docker-compose down -v --remove-orphans 2>/dev/null || true
    ok "Deep clean complete - all volumes deleted"
}

# Show usage
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --graceful    Graceful stop (default)"
    echo "  --cleanup     Stop and remove containers (keep volumes)"
    echo "  --deep        Full cleanup (remove containers AND volumes)"
    echo "  --help        Show this help"
    echo ""
}

# Main
main() {
    local mode="${1:-}"

    case "$mode" in
        --cleanup)
            cd "$PROJECT_ROOT"
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            cleanup
            ok "Cleanup complete"
            ;;
        --deep)
            cd "$PROJECT_ROOT"
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            deep_clean
            ;;
        --help)
            usage
            ;;
        *)
            cd "$PROJECT_ROOT"
            info "Stopping pipeline services (graceful, ${GRACEFUL_TIMEOUT}s timeout)..."
            stop_agentic
            stop_graphify
            stop_document_processing
            stop_infrastructure
            ok "All services stopped gracefully"
            ;;
    esac
}

main "$@"