# SPDX-License-Identifier: MIT
# Makefile for Agentic Graph LLM Document Pipeline
# Target: RHEL 8/9, Docker 19.03.15+

SHELL := /bin/bash
EXPORTED_ENV := $(shell cat .env 2>/dev/null | grep -v '^#' | grep -v '^$$' | tr '\n' ' ')

# Default target
.PHONY: help
help:
	@echo ""
	@echo "======================================================================"
	@echo "  Agentic Graph LLM Document Pipeline - Makefile"
	@echo "======================================================================"
	@echo ""
	@echo "Available targets:"
	@echo "  setup         - Install prerequisites and prepare environment"
	@echo "  check         - Verify system prerequisites and configuration"
	@echo "  start         - Start all pipeline services"
	@echo "  start-infra   - Start infrastructure services only (MinIO, Redis, etc.)"
	@echo "  start-full    - Start infrastructure + all microservices"
	@echo "  stop          - Stop all running services"
	@echo "  restart       - Restart all services (stop then start)"
	@echo "  logs          - Tail logs from all services (use SERVICE=name for specific)"
	@echo "  logs-follow   - Follow logs from all services"
	@echo "  status        - Show status of all containers"
	@echo "  clean         - Remove all containers, volumes, and cached data"
	@echo "  clean-images  - Remove all built images"
	@echo "  test          - Run basic connectivity and health checks"
	@echo "  help          - Show this help message"
	@echo ""
	@echo "======================================================================"

# Setup environment
.PHONY: setup
setup: export SKIP_PROMPT := false
setup:
	@echo ""
	@echo "======================================================================"
	@echo "  Setting up environment..."
	@echo "======================================================================"
	@if [ ! -f .env ]; then \
		echo "Creating .env from template..."; \
		cp .env.example .env; \
		echo "Please edit .env with your configuration!"; \
	else \
		echo ".env already exists, skipping..."; \
	fi
	@echo ""
	@bash scripts/check_prerequisites.sh
	@echo ""
	@bash scripts/setup_environment.sh
	@echo ""
	@echo "======================================================================"
	@echo "  Setup complete!"
	@echo "======================================================================"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit .env with your configuration"
	@echo "  2. Run: make check"
	@echo "  3. Run: make start"
	@echo ""

# Check prerequisites
.PHONY: check
check:
	@echo ""
	@echo "======================================================================"
	@echo "  Checking prerequisites..."
	@echo "======================================================================"
	@bash scripts/check_prerequisites.sh
	@echo ""
	@bash scripts/network_check.sh
	@echo ""
	@echo "======================================================================"
	@echo "  Prerequisite check complete!"
	@echo "======================================================================"

# Start all services
.PHONY: start
start: export COMPOSE_PROJECT_NAME := pipeline
start:
	@echo ""
	@echo "======================================================================"
	@echo "  Starting pipeline services..."
	@echo "======================================================================"
	@bash scripts/start_services.sh
	@echo ""
	@echo "======================================================================"
	@echo "  All services started!"
	@echo "======================================================================"

# Start infrastructure only
.PHONY: start-infra
start-infra: export COMPOSE_PROJECT_NAME := pipeline
start-infra:
	@echo ""
	@echo "Starting infrastructure services only..."
	@docker-compose up -d minio postgres redis qdrant neo4j
	@echo ""
	@echo "Infrastructure started. Run 'make start-full' for microservices."

# Start all services including microservices
.PHONY: start-full
start-full: export COMPOSE_PROJECT_NAME := pipeline
start-full: export COMPOSE_START_FULL := true
start-full:
	@echo ""
	@echo "======================================================================"
	@echo "  Starting full pipeline..."
	@echo "======================================================================"
	@bash scripts/start_services.sh
	@echo ""
	@echo "======================================================================"
	@echo "  Full pipeline started!"
	@echo "======================================================================"

# Stop all services
.PHONY: stop
stop:
	@echo ""
	@echo "======================================================================"
	@echo "  Stopping pipeline services..."
	@echo "======================================================================"
	@bash scripts/stop_services.sh
	@echo ""
	@echo "======================================================================"
	@echo "  All services stopped!"
	@echo "======================================================================"

# Restart all services
.PHONY: restart
restart: stop start

# Tail logs
.PHONY: logsvc
logsvc = $(if $(SERVICE),--service $(SERVICE),--tail 100)
logs:
	@echo "Showing logs (Ctrl+C to stop)..."
	@docker-compose logs -f $(logsvc)

# Follow logs without tail limit
.PHONY: logs-follow
logs-follow:
	@echo "Following all logs (Ctrl+C to stop)..."
	@docker-compose logs -f

# Show status
.PHONY: status
status:
	@docker-compose ps

# Clean everything
.PHONY: clean
clean:
	@echo ""
	@echo "======================================================================"
	@echo "  WARNING: This will remove ALL containers, volumes, and cached data!"
	@echo "======================================================================"
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ ! $$REPLY =~ ^[Yy]$$ ]]; then \
		echo "Aborted."; \
		exit 1; \
	fi
	@echo "Stopping containers..."
	@docker-compose down -v --remove-orphans 2>/dev/null || true
	@echo "Removing built images..."
	@docker images -q "pipeline-*" | xargs -r docker rmi -f 2>/dev/null || true
	@echo "Cleaning cache directories..."
	@rm -rf mymupdf/__pycache__ mineru/__pycache__ graphify/__pycache__ agentic-rag/__pycache__ agentic-wiki/__pycache__ 2>/dev/null || true
	@rm -rf **/__pycache__ 2>/dev/null || true
	@rm -rf .pytest_cache 2>/dev/null || true
	@echo ""
	@echo "======================================================================"
	@echo "  Clean complete!"
	@echo "======================================================================"

# Clean images only
.PHONY: clean-images
clean-images:
	@echo "Removing built images..."
	@docker images -q "pipeline-*" | xargs -r docker rmi -f 2>/dev/null || true
	@echo "Done."

# Test connectivity
.PHONY: test
test:
	@echo ""
	@echo "======================================================================"
	@echo "  Running connectivity and health checks..."
	@echo "======================================================================"
	@bash scripts/network_check.sh
	@echo ""
	@echo "Checking container health..."
	@docker-compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
	@echo ""
	@echo "======================================================================"
	@echo "  Tests complete!"
	@echo "======================================================================"