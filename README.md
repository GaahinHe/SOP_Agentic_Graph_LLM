# SPDX-License-Identifier: MIT
# Agentic Graph LLM Document Pipeline

A production-ready document processing pipeline for extracting, understanding, and querying unstructured documents using multiple AI services.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Docker Network (pipeline)                          │
│                                                                             │
│  ┌──────────┐  ┌─────────────┐  ┌──────────────┐  ┌───────────┐           │
│  │  MinIO   │  │ PostgreSQL  │  │    Redis     │  │  Qdrant   │           │
│  │ :9000    │  │   :5432     │  │   :6379      │  │  :6333    │           │
│  └──────────┘  └─────────────┘  └──────────────┘  └───────────┘           │
│                                                   │                        │
│                     ┌─────────────────────────────┼────────────────────┐  │
│                     │                             │                    │  │
│              ┌──────┴──────┐              ┌───────┴──────┐        ┌──────┴──┐
│              │   Neo4j     │              │  Graphify   │        │ MinerU  │
│              │ :7474 :7687 │              │   :8004     │        │ :8002   │
│              └─────────────┘              └─────────────┘        └─────────┘
│                     │                             │                    │
│         ┌───────────┼───────────┐                  │                    │
│         │           │           │                  │                    │
│  ┌──────┴──┐  ┌─────┴────┐  ┌──┴────┐        ┌─────┴─────┐        ┌────┴────┐
│  │  MyMuPDF│  │Agentic  │  │Agentic│        │  Agentic  │        │ MinerU  │
│  │  :8001  │  │  RAG    │  │ Wiki  │        │  RAG      │        │ Core    │
│  │         │  │ :8005   │  │ :8006 │        │ :8005     │        │ :8003   │
│  └─────────┘  └─────────┘  └───────┘        └───────────┘        └─────────┘
└─────────────────────────────────────────────────────────────────────────────┘
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| **MyMuPDF** | 8001 | Document preprocessing - OCR, layout detection, text extraction |
| **MinerU** | 8002 | Document structure extraction - formulas, tables, images |
| **Graphify** | 8004 | Knowledge graph construction - entity and relationship extraction |
| **Agentic RAG** | 8005 | Multi-agent retrieval system - vector + graph search |
| **Agentic Wiki** | 8006 | Interactive knowledge base with semantic search |

## Infrastructure Services

| Service | Port | Description |
|---------|------|-------------|
| **MinIO** | 9000/9001 | S3-compatible object storage |
| **PostgreSQL** | 5432 | Metadata storage |
| **Redis** | 6379 | Caching and job queue |
| **Qdrant** | 6333/6334 | Vector database for similarity search |
| **Neo4j** | 7474/7687 | Graph database for knowledge representation |

## Quick Start

### Prerequisites

- Docker 19.03.15+
- NVIDIA Docker runtime (for GPU support)
- 32GB+ RAM recommended
- 200GB+ disk space

### Deployment

```bash
# 1. Clone repository
git clone <repository-url>
cd SOP_Agentic_Graph_LLM

# 2. Configure environment
cp .env.example .env
# Edit .env with your configuration

# 3. Check prerequisites
make check

# 4. Start all services
make start

# 5. View service status
make status

# 6. Tail logs
make logs
```

## Makefile Commands

```bash
make help          # Show all available commands
make setup         # Install prerequisites and prepare environment
make check         # Verify system prerequisites
make start         # Start all pipeline services
make stop          # Stop all services
make restart       # Restart all services
make status        # Show container status
make logs          # Tail logs from all services
make clean         # Remove containers and volumes
make test          # Run connectivity and health checks
```

## Environment Variables

Key environment variables (see `.env.example` for full list):

| Variable | Description | Default |
|----------|-------------|---------|
| `COMPOSE_PROJECT_NAME` | Docker Compose project name | `pipeline` |
| `LLM_API_KEY` | API key for LLM services | - |
| `MINIO_ROOT_USER` | MinIO access key | `minioadmin` |
| `MINIO_ROOT_PASSWORD` | MinIO secret key | `changeme` |
| `NEO4J_PASSWORD` | Neo4j password | `changeme` |
| `ENABLE_GPU` | Enable GPU support | `true` |
| `CUDA_VISIBLE_DEVICES` | GPU device IDs | `0` |

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 8 cores | 16 cores |
| RAM | 16 GB | 32 GB |
| GPU | NVIDIA Tesla L2 (24GB) | NVIDIA Tesla L2 |
| Disk | 200 GB SSD | 500 GB SSD |

## Target Platform

Validated for:
- **OS**: RHEL 8/9
- **Docker**: 19.03.15+
- **NVIDIA Driver**: 535.161.08 (CUDA 12.2)
- **GPU**: NVIDIA Tesla L2

## Data Flow

1. **Ingest**: Document uploaded to MinIO object storage
2. **Preprocess**: MyMuPDF extracts text and performs OCR
3. **Structure**: MinerU identifies formulas, tables, images
4. **Graph**: Graphify builds knowledge graph in Neo4j
5. **Index**: Document chunks indexed in Qdrant vector DB
6. **Query**: Agentic RAG retrieves relevant context via vector + graph search
7. **Answer**: LLM generates answer from retrieved context

## API Endpoints

### MyMuPDF
- `POST /process` - Process PDF/image document
- `GET /status/{doc_id}` - Get processing status

### MinerU
- `POST /process` - Extract structured content

### Graphify
- `POST /build` - Build knowledge graph
- `GET /graph/{doc_id}` - Retrieve graph
- `GET /stats` - Graph statistics

### Agentic RAG
- `POST /query` - Query with RAG
- `WS /ws` - WebSocket for streaming

### Agentic Wiki
- `GET /wiki` - List pages
- `POST /wiki` - Create page
- `POST /search` - Search knowledge base
- `GET /ui` - Web interface

## Monitoring

Optional monitoring stack:
```bash
# Start with monitoring
docker-compose --profile monitoring up -d
```

Access points:
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

## Troubleshooting

### Check prerequisites
```bash
bash scripts/check_prerequisites.sh
```

### Check network connectivity
```bash
bash scripts/network_check.sh
```

### View service logs
```bash
docker-compose logs -f <service-name>
```

### Restart a specific service
```bash
docker-compose restart <service-name>
```

## License

MIT License - See LICENSE file for details