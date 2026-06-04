#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# setup_environment.sh - Environment setup script
# Target: RHEL 8/9, Docker 19.03.15+, NVIDIA Tesla L2

set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Check if running as root or has sudo
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &>/dev/null; then
        SUDO_CMD="sudo"
    else
        echo -e "${RED}[FAIL]${NC} This script must be run as root or with sudo"
        exit 1
    fi
else
    SUDO_CMD=""
fi

# Logging functions
log_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_step() {
    echo -e "${CYAN}[STEP]${NC} $1"
}

# Detect OS
detect_os() {
    if [ -f /etc/redhat-release ]; then
        echo "rhel"
    elif [ -f /etc/os-release ]; then
        local os_id
        os_id=$(grep -E '^ID=' /etc/os-release | cut -d'"' -f2)
        echo "$os_id"
    else
        echo "unknown"
    fi
}

# Install nvidia-docker2
install_nvidia_docker() {
    log_step "Installing nvidia-docker2..."

    local os=$(detect_os)

    if [ "$os" = "rhel" ] || [ "$os" = "centos" ] || [ "$os" = "rocky" ] || [ "$os" = "alma" ]; then
        # Check if nvidia-docker is already installed
        if command -v nvidia-docker &>/dev/null; then
            log_ok "nvidia-docker already installed"
            return 0
        fi

        # Install from NVIDIA repository
        $SUDO_CMD dnf config-manager --add-repo=https://nvidia.github.io/libnvidia-container/rhel$(grep -oE '[0-9]+' /etc/redhat-release | head -1)/nvidia-docker.repo
        $SUDO_CMD dnf install -y nvidia-docker2
        $SUDO_CMD systemctl restart docker

        log_ok "nvidia-docker2 installed"
    else
        log_warn "Cannot install nvidia-docker2 - unknown OS"
    fi
}

# Configure pip mirror (JFrog)
configure_pip_mirror() {
    log_step "Configuring pip mirror..."

    local pip_conf_file="${HOME}/.pip/pip.conf"
    local pip_conf_dir="${HOME}/.pip"

    if [ -f "$pip_conf_file" ]; then
        if grep -q "jfrog" "$pip_conf_file"; then
            log_ok "Pip mirror already configured"
            return 0
        fi
        log_warn "Existing pip.conf found - backing up"
        $SUDO_CMD cp "$pip_conf_file" "${pip_conf_file}.backup"
    fi

    $SUDO_CMD mkdir -p "$pip_conf_dir"
    $SUDO_CMD tee "$pip_conf_file" > /dev/null << 'EOF'
[global]
index-url = https://your-company.jfrog.io/artifactory/api/pypi/pypi-virtual/simple
trusted-host = your-company.jfrog.io
timeout = 120
retries = 3

[install]
trusted-host = your-company.jfrog.io
EOF

    log_ok "Pip mirror configured at $pip_conf_file"
}

# Create .env template if not exists
create_env_template() {
    log_step "Checking .env configuration..."

    if [ -f .env ]; then
        log_ok ".env already exists"
        return 0
    fi

    if [ -f .env.example ]; then
        log_info "Creating .env from .env.example..."
        $SUDO_CMD cp .env.example .env
        log_ok ".env created - please edit with your configuration"
    else
        log_fail ".env.example not found"
        return 1
    fi
}

# Configure firewall ports
configure_firewall() {
    log_step "Configuring firewall..."

    if ! command -v firewall-cmd &>/dev/null; then
        log_warn "firewalld not available - skipping firewall config"
        return 0
    fi

    if ! firewall-cmd --state &>/dev/null; then
        log_ok "Firewalld not running - no configuration needed"
        return 0
    fi

    local ports=(
        "9000/tcp"   # MinIO API
        "9001/tcp"   # MinIO Console
        "5432/tcp"   # PostgreSQL
        "6333/tcp"   # Qdrant HTTP
        "6334/tcp"   # Qdrant gRPC
        "7474/tcp"   # Neo4j HTTP
        "7687/tcp"   # Neo4j Bolt
        "8001/tcp"   # mymupdf
        "8002/tcp"   # mineru-wrapper
        "8003/tcp"   # mineru-core
        "8004/tcp"   # graphify
        "8005/tcp"   # agentic-rag
        "8006/tcp"   # agentic-wiki
        "9090/tcp"   # Prometheus (optional)
        "3000/tcp"   # Grafana (optional)
    )

    echo "The following ports need to be opened in firewall:"
    for port in "${ports[@]}"; do
        echo "  - $port"
    done

    read -p "Open these ports now? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Skipping firewall configuration"
        return 0
    fi

    for port in "${ports[@]}"; do
        $SUDO_CMD firewall-cmd --permanent --add-port="$port" 2>/dev/null || true
    done
    $SUDO_CMD firewall-cmd --reload

    log_ok "Firewall ports configured"
}

# Configure Docker daemon
configure_docker() {
    log_step "Configuring Docker daemon..."

    local docker_conf="/etc/docker/daemon.json"
    local docker_conf_dir="/etc/docker"

    # Create daemon.json if it doesn't exist
    if [ ! -f "$docker_conf" ]; then
        $SUDO_CMD mkdir -p "$docker_conf_dir"
        $SUDO_CMD tee "$docker_conf" > /dev/null << 'EOF'
{
    "storage-driver": "overlay2",
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "100m",
        "max-file": "3"
    },
    "default-ulimits": {
        "nofile": {
            "Name": "nofile",
            "Hard": 64000,
            "Soft": 64000
        }
    },
    "default-cgroupns-mode": "host",
    "live-restore": true,
    "userland-proxy": false,
    "icc": false,
    "ip-forward": false,
    "ip-masq": false,
    "bridge": "none"
}
EOF
        log_ok "Docker daemon configuration created"
    else
        log_warn "Docker daemon.json already exists - manual review required"
    fi

    # Configure nvidia runtime
    if [ ! -f "$docker_conf" ] || ! grep -q "nvidia" "$docker_conf"; then
        log_info "Adding NVIDIA runtime to Docker configuration..."
        $SUDO_CMD mkdir -p "$docker_conf_dir"
        $SUDO_CMD tee -a "$docker_conf" > /dev/null << 'EOF'
{
    "runtimes": {
        "nvidia": {
            "path": "nvidia-container-runtime",
            "args": ["--config", "/etc/nvidia-container-runtime/config.toml"]
        }
    }
}
EOF
    fi

    # Restart Docker
    $SUDO_CMD systemctl restart docker
    log_ok "Docker daemon restarted"
}

# Create necessary directories
create_directories() {
    log_step "Creating necessary directories..."

    local dirs=(
        "mineru/models"
        "graphify/models"
        "agentic-rag/data"
        "agentic-wiki/data"
        "monitoring"
        "logs"
    )

    for dir in "${dirs[@]}"; do
        if [ ! -d "$dir" ]; then
            $SUDO_CMD mkdir -p "$dir"
            log_info "Created: $dir"
        fi
    done

    log_ok "Directories created"
}

# Main execution
main() {
    echo ""
    echo "======================================================================"
    echo "  Environment Setup"
    echo "  Target: RHEL 8/9, Docker 19.03.15+, NVIDIA Tesla L2"
    echo "======================================================================"
    echo ""

    # Check prerequisites
    if ! command -v docker &>/dev/null; then
        log_fail "Docker not installed - please install Docker first"
        exit 1
    fi

    create_directories
    install_nvidia_docker
    configure_pip_mirror
    create_env_template
    configure_firewall
    configure_docker

    echo ""
    echo "======================================================================"
    echo -e "${GREEN}Environment setup complete!${NC}"
    echo "======================================================================"
    echo ""
    echo "Next steps:"
    echo "  1. Edit .env with your configuration"
    echo "  2. Run: make check"
    echo "  3. Run: make start"
    echo ""
}

main "$@"