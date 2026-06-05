# SOP Agentic Graph LLM — 文档解析管线

> 基于多Agent协作的非结构化文档解析、知识图谱构建与检索增强生成系统。生产环境验证于 **RHEL 8/9 + Docker 19.03.15 + NVIDIA Tesla L2**。

---

## 系统架构

```
                            ┌──────────────────────────────────────────────┐
                            │              Docker Network (pipeline)         │
                            │                                              │
┌──────────┐  ┌─────────────┴────┐   ┌──────────────────────────────┐    │
│  MinIO   │  │    PostgreSQL    │   │         Qdrant                 │    │
│  :9000   │  │      :5432       │   │   :6333 HTTP  :6334 gRPC      │    │
└──────────┘  └──────────────────┘   └──────────────────────────────┘    │
                                                           │              │
                     ┌──────────────┐  ┌───────────────────┴────────────┐ │
                     │    Redis     │  │           Neo4j                 │ │
                     │    :6379     │  │     :7474 HTTP  :7687 Bolt      │ │
                     └──────────────┘  └────────────────────────────────┘ │
                                          │                                │
                     ┌────────────────────┼────────────────────────────────┤
                     │                    │                                 │
            ┌────────┴───────┐  ┌─────────┴────────┐  ┌─────────────────┴┐
            │   MyMuPDF       │  │   MinerU          │  │   Graphify       │
            │   :8001         │  │   :8002  :8003    │  │   :8004          │
            └────────┬────────┘  └────────┬──────────┘  └────────┬─────────┘
                     │                    │                      │
                     └────────────────────┼──────────────────────┘
                                          │
                               ┌──────────┴──────────┐
                               │   Agentic RAG        │
                               │   :8005              │
                               └──────────┬──────────┘
                                          │
                               ┌──────────┴──────────┐
                               │   Agentic Wiki       │
                               │   :8006              │
                               └──────────────────────┘
```

## 微服务说明

| 服务 | 端口 | 职责 |
|------|------|------|
| **MyMuPDF** | 8001 | 文档预处理：OCR、布局检测、文本提取 |
| **MinerU Core** | 8003 | 文档深度理解：公式/表格/图片提取（GPU加速） |
| **MinerU Wrapper** | 8002 | API封装与任务编排 |
| **Graphify** | 8004 | 知识图谱构建：实体抽取 → Neo4j |
| **Agentic RAG** | 8005 | 多Agent RAG：向量+图检索、重新排序 |
| **Agentic Wiki** | 8006 | 交互式知识库前端 |

## 基础设施

| 服务 | 端口 | 用途 |
|------|------|------|
| MinIO | 9000/9001 | S3兼容对象存储 |
| PostgreSQL | 5432 | 元数据存储 |
| Redis | 6379 | 缓存与任务队列 |
| Qdrant | 6333/6334 | 向量数据库 |
| Neo4j | 7474/7687 | 图数据库 |

## 快速开始

```bash
# 1. 环境检查（首次部署）
make check

# 2. 环境配置（安装 nvidia-docker2 等）
make setup

# 3. 编辑配置
cp .env.example .env
# 填入 LLM_API_KEY 等敏感配置

# 4. 启动所有服务
make start

# 5. 查看服务状态
make status

# 6. 查看日志
make logs SERVICE=mymupdf
```

## 环境要求

| 组件 | 最低要求 |
|------|---------|
| CPU | 16核+ |
| 内存 | 200GB |
| GPU | NVIDIA Tesla L2 (24GB) 或同等 |
| 存储 | 500GB SSD |
| Docker | 19.03.15+ |
| Docker Compose | 1.27+ |

## SELinux 配置

RHEL 默认 Enforcing，卷挂载时需使用 `:z` 标签，docker-compose.yml 已配置。

## 目录结构

```
SOP_Agentic_Graph_LLM/
├── docker-compose.yml      # 服务编排
├── Makefile                # 便捷命令
├── .env.example            # 环境变量模板
├── .gitignore
├── README.md
├── monitoring/
│   └── prometheus.yml
├── scripts/
│   ├── check_prerequisites.sh
│   ├── setup_environment.sh
│   ├── network_check.sh
│   ├── start_services.sh
│   └── stop_services.sh
├── mymupdf/
├── mineru/
├── graphify/
├── agentic-rag/
└── agentic-wiki/
```

## 健康检查

所有服务暴露 `GET /health` 端点：

```bash
# 批量检查
for port in 8001 8002 8003 8004 8005 8006; do
  echo -n "Port $port: "
  curl -sf "http://localhost:$port/health" && echo "OK" || echo "FAIL"
done
```

## 数据流

```
上传文档 → MinIO → MyMuPDF (OCR+布局)
                              ↓
                       MinerU (深度理解)
                              ↓
                  ┌───────────┴───────────┐
                  ↓                       ↓
            Graphify (图谱)          向量化 (embedding)
                  ↓                       ↓
            Neo4j                  Qdrant
                  └───────────┬───────────┘
                              ↓
                    Agentic RAG (检索)
                              ↓
                      Agentic Wiki (前端)
```

## 故障排查

```bash
# 检查 NVIDIA 驱动
nvidia-smi

# 测试 GPU 容器
docker run --rm --gpus all nvidia/cuda:12.2.0-base nvidia-smi

# 查看所有容器日志
docker-compose logs -f

# 进入容器调试
docker exec -it pipeline-mymupdf bash

# 检查网络连通性
bash scripts/network_check.sh

# 强制清理（紧急）
make stop
bash scripts/stop_services.sh --force
```

## 验证环境

- **OS**: RHEL 8.9
- **Docker**: 19.03.15
- **NVIDIA Driver**: 535.161.08 (CUDA 12.2)
- **GPU**: NVIDIA Tesla L2 (24GB)

---

**最后更新**: 2026-06-05