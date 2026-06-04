#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# check_prerequisites.sh - System prerequisite checker
# Target: RHEL 8/9, Docker 19.03.15+, NVIDIA Tesla L2

set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# Check if running as root or has sudo
check_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        SUDO_CMD=""
    elif command -v sudo &>/dev/null; then
        SUDO_CMD="sudo"
    else
        log_fail "Cannot run without root or sudo access"
        exit 1
    fi
}

# Check RHEL/CentOS version
check_os_version() {
    log_info "Checking OS version..."
    if [ -f /etc/redhat-release ]; then
        local os_version
        os_version=$(cat /etc/redhat-release | head -1)
        local major_version
        major_version=$(grep -oE '[0-9]+' /etc/redhat-release | head -1)

        if [ "$major_version" -ge 8 ] && [ "$major_version" -le 9 ]; then
            log_ok "OS: $os_version"
        else
            log_fail "OS version $major_version not supported (requires 8 or 9)"
        fi
    elif [ -f /etc/os-release ]; then
        local os_id
        os_id=$(grep -E '^ID=' /etc/os-release | cut -d'"' -f2)
        if [ "$os_id" = "rhel" ] || [ "$os_id" = "centos" ] || [ "$os_id" = "rocky" ] || [ "$os_id" = "alma" ]; then
            local os_version_id
            os_version_id=$(grep -E '^VERSION_ID=' /etc/os-release | cut -d'"' -f2 | cut -d'.' -f1)
            if [ "$os_version_id" -ge 8 ]; then
                log_ok "OS: RHEL-compatible $os_version_id"
            else
                log_fail "OS version $os_version_id not supported"
            fi
        else
            log_warn "Non-RHEL OS detected: $os_id"
        fi
    else
        log_warn "Cannot determine OS version"
    fi
}

# Check kernel version
check_kernel() {
    log_info "Checking kernel version..."
    local kernel_version
    kernel_version=$(uname -r)
    local kernel_major
    kernel_major=$(echo "$kernel_version" | cut -d'.' -f1)
    local kernel_minor
    kernel_minor=$(echo "$kernel_version" | cut -d'.' -f2)

    if [ "$kernel_major" -ge 4 ] && [ "$kernel_minor" -ge 18 ]; then
        log_ok "Kernel: $kernel_version"
    else
        log_warn "Kernel $kernel_version may not have full support (4.18+ recommended)"
    fi
}

# Check SELinux status
check_selinux() {
    log_info "Checking SELinux status..."
    if command -v getenforce &>/dev/null; then
        local selinux_status
        selinux_status=$(getenforce 2>/dev/null || echo "Unknown")
        if [ "$selinux_status" = "Enforcing" ]; then
            log_ok "SELinux: Enforcing (correct for production)"
        elif [ "$selinux_status" = "Permissive" ]; then
            log_warn "SELinux: Permissive (consider Enforcing for production)"
        else
            log_warn "SELinux: $selinux_status"
        fi
    else
        log_warn "SELinux tools not available"
    fi

    # Check SELinux context for Docker
    if [ -f /etc/selinux/config ]; then
        local selinux_config_mode
        selinux_config_mode=$(grep "^SELINUX=" /etc/selinux/config | cut -d'=' -f2)
        log_info "SELinux config: $selinux_config_mode"
    fi
}

# Check NVIDIA driver
check_nvidia_driver() {
    log_info "Checking NVIDIA driver..."
    if command -v nvidia-smi &>/dev/null; then
        if nvidia-smi &>/dev/null; then
            local driver_version
            driver_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
            local cuda_version
            cuda_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits | head -1 | awk '{print $2}')
            log_ok "NVIDIA Driver: $driver_version"

            # Check CUDA runtime version
            if command -v nvcc &>/dev/null; then
                local nvcc_version
                nvcc_version=$(nvcc --version | grep "release" | awk '{print $5}' | tr -d ',')
                log_ok "CUDA nvcc: $nvcc_version"
            fi

            # Check GPU info
            local gpu_count
            gpu_count=$(nvidia-smi --query-gpu=gpu_name --format=csv,noheader | wc -l)
            log_info "GPU count: $gpu_count"
            nvidia-smi --query-gpu=gpu_name,memory.total,memory.free --format=csv
        else
            log_fail "NVIDIA driver installed but not functional"
        fi
    else
        log_fail "NVIDIA driver not installed"
        ((FAIL_COUNT++))
    fi
}

# Check Docker version
check_docker() {
    log_info "Checking Docker version..."
    if command -v docker &>/dev/null; then
        local docker_version
        docker_version=$(docker --version | awk '{print $3}' | tr -d ',')
        local major minor
        major=$(echo "$docker_version" | cut -d'.' -f1)
        minor=$(echo "$docker_version" | cut -d'.' -f2)

        if [ "$major" -gt 19 ] || { [ "$major" -eq 19 ] && [ "$minor" -ge 3 ]; }; then
            log_ok "Docker: $docker_version"
        else
            log_fail "Docker $docker_version not supported (requires 19.03.15+)"
        fi

        # Check Docker daemon
        if docker info &>/dev/null; then
            log_ok "Docker daemon: running"
        else
            log_fail "Docker daemon not accessible"
            ((FAIL_COUNT++))
        fi

        # Check Docker compose
        if command -v docker-compose &>/dev/null; then
            local compose_version
            compose_version=$(docker-compose --version | awk '{print $3}' | tr -d ',')
            log_ok "Docker Compose: $compose_version"
        else
            log_warn "docker-compose not found (using docker compose plugin)"
        fi
    else
        log_fail "Docker not installed"
        ((FAIL_COUNT++))
    fi
}

# Check NVIDIA Docker support
check_nvidia_docker() {
    log_info "Checking NVIDIA Docker support..."
    if command -v nvidia-docker &>/dev/null; then
        local nvidia_docker_version
        nvidia_docker_version=$(nvidia-docker --version | awk '{print $3}' | tr -d ',')
        log_ok "nvidia-docker: $nvidia_docker_version"
    elif docker info 2>/dev/null | grep -q "nvidia"; then
        log_ok "NVIDIA Docker: available via Docker plugin"
    else
        log_fail "NVIDIA Docker not installed"
        log_info "Install with: sudo dnf install nvidia-docker2"
        ((FAIL_COUNT++))
    fi

    # Test nvidia runtime
    if docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi &>/dev/null; then
        log_ok "NVIDIA runtime: functional"
    else
        log_warn "NVIDIA runtime test failed"
    fi
}

# Check disk space
check_disk_space() {
    log_info "Checking disk space..."
    local required_gb=250
    local available_gb

    # Get root partition space
    available_gb=$(df -BG / | awk 'NR==2 {print $4}' | tr -d 'G')
    available_gb=$((available_gb + 0))  # Convert to int

    if [ "$available_gb" -ge "$required_gb" ]; then
        log_ok "Disk space: ${available_gb}GB available (${required_gb}GB required)"
    else
        log_fail "Disk space: ${available_gb}GB available (${required_gb}GB required)"
        ((FAIL_COUNT++))
    fi

    # Check Docker storage location
    local docker_root
    docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "/var/lib/docker")
    local docker_space
    docker_space=$(df -BG "$docker_root" | awk 'NR==2 {print $4}' | tr -d 'G')
    docker_space=$((docker_space + 0))
    log_info "Docker storage ($docker_root): ${docker_space}GB available"
}

# Check network connectivity
check_network() {
    log_info "Checking network connectivity..."

    # Check if ports are available
    local ports=(9000 9001 5432 6333 6334 7474 7687 8001 8002 8003 8004 8005 8006)
    local all_ports_ok=true

    for port in "${ports[@]}"; do
        if netstat -tuln 2>/dev/null | grep -q ":$port " || ss -tuln 2>/dev/null | grep -q ":$port "; then
            log_warn "Port $port is already in use"
            all_ports_ok=false
        fi
    done

    if $all_ports_ok; then
        log_ok "Required ports are available"
    fi

    # Check external connectivity
    local test_hosts=(8.8.8.8 google.com github.com)
    for host in "${test_hosts[@]}"; do
        if timeout 5 ping -c 1 -W 2 "$host" &>/dev/null; then
            log_ok "Network: $host reachable"
        else
            log_warn "Network: $host not reachable"
        fi
    done
}

# Check memory
check_memory() {
    log_info "Checking system memory..."
    local total_mem_kb
    total_mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    local total_mem_gb=$((total_mem_kb / 1024 / 1024))
    local available_mem_kb
    available_mem_kb=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
    local available_mem_gb=$((available_mem_kb / 1024 / 1024))

    if [ "$total_mem_gb" -ge 180 ]; then
        log_ok "Memory: ${total_mem_gb}GB total, ${available_mem_gb}GB available"
    else
        log_warn "Memory: ${total_mem_gb}GB total (200GB recommended)"
    fi
}

# Check firewall
check_firewall() {
    log_info "Checking firewall status..."
    if command -v firewall-cmd &>/dev/null; then
        if firewall-cmd --state &>/dev/null; then
            local active_zones
            active_zones=$(firewall-cmd --get-active-zones | grep -v "^$" | head -5)
            log_info "Firewalld active zones: $active_zones"
            log_warn "Firewalld is running - ensure ports are open if needed"
        else
            log_ok "Firewalld: not running"
        fi
    elif command -v iptables &>/dev/null; then
        local iptables_rules
        iptables_rules=$(iptables -L -n 2>/dev/null | wc -l)
        log_info "iptables rules: $iptables_rules"
    else
        log_info "No firewall tool detected"
    fi
}

# Main execution
main() {
    echo ""
    echo "======================================================================"
    echo "  System Prerequisite Checker"
    echo "  Target: RHEL 8/9, Docker 19.03.15+, NVIDIA Tesla L2"
    echo "======================================================================"
    echo ""

    check_sudo
    check_os_version
    check_kernel
    check_selinux
    check_nvidia_driver
    check_docker
    check_nvidia_docker
    check_disk_space
    check_network
    check_memory
    check_firewall

    echo ""
    echo "======================================================================"
    if [ "$FAIL_COUNT" -eq 0 ]; then
        echo -e "${GREEN}All prerequisite checks passed!${NC}"
        echo "======================================================================"
        exit 0
    else
        echo -e "${RED}$FAIL_COUNT check(s) failed - please resolve before proceeding${NC}"
        echo "======================================================================"
        exit 1
    fi
}

main "$@"