#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# network_check.sh - Network connectivity checker for pipeline services
# Tests connectivity to required endpoints

set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Counter for failures
FAIL_COUNT=0

# Logging functions
log_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((FAIL_COUNT++))
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Check MinIO connectivity
check_minio() {
    log_info "Checking MinIO connectivity..."
    local minio_host="${MINIO_HOST:-localhost}"
    local minio_port="${MINIO_PORT:-9000}"

    if timeout 5 nc -z "$minio_host" "$minio_port" 2>/dev/null; then
        log_ok "MinIO ($minio_host:$minio_port): reachable"
    else
        log_fail "MinIO ($minio_host:$minio_port): not reachable"
    fi
}

# Check MinIO Console
check_minio_console() {
    log_info "Checking MinIO Console..."
    local minio_host="${MINIO_HOST:-localhost}"
    local minio_port="${MINIO_PORT:-9001}"

    if timeout 5 nc -z "$minio_host" "$minio_port" 2>/dev/null; then
        log_ok "MinIO Console ($minio_host:$minio_port): reachable"
    else
        log_fail "MinIO Console ($minio_host:$minio_port): not reachable"
    fi
}

# Check PostgreSQL connectivity
check_postgres() {
    log_info "Checking PostgreSQL connectivity..."
    local pg_host="${POSTGRES_HOST:-localhost}"
    local pg_port="${POSTGRES_PORT:-5432}"

    if timeout 5 nc -z "$pg_host" "$pg_port" 2>/dev/null; then
        log_ok "PostgreSQL ($pg_host:$pg_port): reachable"

        # Check if we can authenticate
        if command -v psql &>/dev/null; then
            if PG_PASSWORD="${POSTGRES_PASSWORD:-changeme}" timeout 10 psql -h "$pg_host" -p "$pg_port" -U "${POSTGRES_USER:-pipeline_user}" -d "${POSTGRES_DB:-agentic_graph}" -c "SELECT 1;" &>/dev/null; then
                log_ok "PostgreSQL authentication: successful"
            else
                log_warn "PostgreSQL authentication: failed (check credentials)"
            fi
        fi
    else
        log_fail "PostgreSQL ($pg_host:$pg_port): not reachable"
    fi
}

# Check Redis connectivity
check_redis() {
    log_info "Checking Redis connectivity..."
    local redis_host="${REDIS_HOST:-localhost}"
    local redis_port="${REDIS_PORT:-6379}"

    if timeout 5 nc -z "$redis_host" "$redis_port" 2>/dev/null; then
        log_ok "Redis ($redis_host:$redis_port): reachable"

        # Check Redis ping
        if command -v redis-cli &>/dev/null; then
            if redis-cli -h "$redis_host" -p "$redis_port" ping &>/dev/null; then
                log_ok "Redis PING: successful"
            else
                log_warn "Redis PING: failed"
            fi
        fi
    else
        log_fail "Redis ($redis_host:$redis_port): not reachable"
    fi
}

# Check Qdrant connectivity
check_qdrant() {
    log_info "Checking Qdrant connectivity..."
    local qdrant_host="${QDRANT_HOST:-localhost}"
    local qdrant_http="${QDRANT_HTTP_PORT:-6333}"
    local qdrant_grpc="${QDRANT_GRPC_PORT:-6334}"

    if timeout 5 nc -z "$qdrant_host" "$qdrant_http" 2>/dev/null; then
        log_ok "Qdrant HTTP ($qdrant_host:$qdrant_http): reachable"
    else
        log_fail "Qdrant HTTP ($qdrant_host:$qdrant_http): not reachable"
    fi

    if timeout 5 nc -z "$qdrant_host" "$qdrant_grpc" 2>/dev/null; then
        log_ok "Qdrant gRPC ($qdrant_host:$qdrant_grpc): reachable"
    else
        log_fail "Qdrant gRPC ($qdrant_host:$qdrant_grpc): not reachable"
    fi

    # Try API health check
    if command -v curl &>/dev/null; then
        if timeout 5 curl -sf "http://$qdrant_host:$qdrant_http/readyz" &>/dev/null; then
            log_ok "Qdrant API health check: passing"
        else
            log_warn "Qdrant API health check: failing"
        fi
    fi
}

# Check Neo4j connectivity
check_neo4j() {
    log_info "Checking Neo4j connectivity..."
    local neo4j_host="${NEO4J_HOST:-localhost}"
    local neo4j_http="${NEO4J_HTTP_PORT:-7474}"
    local neo4j_bolt="${NEO4J_BOLT_PORT:-7687}"

    if timeout 5 nc -z "$neo4j_host" "$neo4j_http" 2>/dev/null; then
        log_ok "Neo4j HTTP ($neo4j_host:$neo4j_http): reachable"
    else
        log_fail "Neo4j HTTP ($neo4j_host:$neo4j_http): not reachable"
    fi

    if timeout 5 nc -z "$neo4j_host" "$neo4j_bolt" 2>/dev/null; then
        log_ok "Neo4j Bolt ($neo4j_host:$neo4j_bolt): reachable"
    else
        log_fail "Neo4j Bolt ($neo4j_host:$neo4j_bolt): not reachable"
    fi
}

# Check pipeline service health endpoints
check_service_health() {
    log_info "Checking pipeline service health endpoints..."

    local services=(
        "mymupdf:8001:/health"
        "mineru-wrapper:8002:/health"
        "mineru-core:8003:/health"
        "graphify:8004:/health"
        "agentic-rag:8005:/health"
        "agentic-wiki:8006:/health"
    )

    for service in "${services[@]}"; do
        local name="${service%%:*}"
        local port_path="${service##*:}"
        local port="${port_path%%/*}"
        local path="${port_path#*/}"

        if timeout 5 nc -z localhost "$port" 2>/dev/null; then
            if command -v curl &>/dev/null; then
                if timeout 5 curl -sf "http://localhost:$port/$path" &>/dev/null; then
                    log_ok "$name (port $port): healthy"
                else
                    log_warn "$name (port $port): not responding to health check"
                fi
            else
                log_ok "$name (port $port): port open"
            fi
        else
            log_warn "$name (port $port): not reachable"
        fi
    done
}

# Check external connectivity
check_external() {
    log_info "Checking external connectivity..."

    local endpoints=(
        "https://google.com:443"
        "https://github.com:443"
        "https://huggingface.co:443"
    )

    for endpoint in "${endpoints[@]}"; do
        if command -v curl &>/dev/null; then
            if timeout 10 curl -sfI "$endpoint" &>/dev/null; then
                log_ok "External: $endpoint reachable"
            else
                log_warn "External: $endpoint not reachable"
            fi
        elif command -v wget &>/dev/null; then
            if timeout 10 wget -q --spider "$endpoint" 2>/dev/null; then
                log_ok "External: $endpoint reachable"
            else
                log_warn "External: $endpoint not reachable"
            fi
        fi
    done
}

# Check internal Docker network
check_docker_network() {
    log_info "Checking Docker network configuration..."

    # Check if docker network exists
    if docker network ls | grep -q "pipeline_pipeline"; then
        log_ok "Docker network 'pipeline_pipeline': exists"
    else
        log_warn "Docker network 'pipeline_pipeline': not found (will be created)"
    fi

    # Check network connectivity between containers
    local containers=("minio" "postgres" "redis" "qdrant" "neo4j")
    for container in "${containers[@]}"; do
        if docker ps --format '{{.Names}}' | grep -q "^pipeline-${container}$"; then
            local ip
            ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "pipeline-${container}" 2>/dev/null || echo "N/A")
            log_info "Container pipeline-${container}: IP=$ip"
        fi
    done
}

# Check NVIDIA GPU visibility from containers
check_gpu_access() {
    log_info "Checking GPU access from containers..."

    if command -v nvidia-smi &>/dev/null; then
        # Test running a container with GPU access
        if docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null; then
            log_ok "GPU access from containers: functional"
        else
            log_warn "GPU access from containers: test container failed"
        fi
    else
        log_warn "nvidia-smi not available on host"
    fi
}

# Main execution
main() {
    echo ""
    echo "======================================================================"
    echo "  Network Connectivity Checker"
    echo "======================================================================"
    echo ""

    # Load .env if exists
    if [ -f .env ]; then
        set -a
        source .env 2>/dev/null || true
        set +a
    fi

    check_minio
    check_minio_console
    check_postgres
    check_redis
    check_qdrant
    check_neo4j
    check_service_health
    check_external
    check_docker_network
    check_gpu_access

    echo ""
    echo "======================================================================"
    if [ "$FAIL_COUNT" -eq 0 ]; then
        echo -e "${GREEN}All network checks passed!${NC}"
        echo "======================================================================"
        exit 0
    else
        echo -e "${RED}$FAIL_COUNT check(s) failed${NC}"
        echo "======================================================================"
        exit 1
    fi
}

main "$@"