# SOP Agentic Graph LLM — 生产部署清单

> 目标服务器: RHEL 8/9, Docker 19.03.15, NVIDIA Tesla L2, 200GB RAM

---

## 阶段 0: 本地准备（你的开发机）

### 0.1 克隆仓库到目标服务器

```bash
# 方式A: 如果你要通过中转机器部署，先打包再上传
tar -czvf sop_pipeline.tar.gz SOP_Agentic_Graph_LLM/
scp sop_pipeline.tar.gz user@target-server:/data/

# 方式B: Git 克隆（需要 GitHub 认证）
git clone https://github.com/YOUR_USERNAME/SOP_Agentic_Graph_LLM.git /data/SOP_Agentic_Graph_LLM
cd /data/SOP_Agentic_Graph_LLM
```

### 0.2 创建 GitHub 仓库（本地操作）

```bash
# 首次需要在 GitHub 创建 Personal Access Token:
# GitHub → Settings → Developer settings → Personal access tokens → Generate new token
# 需要的权限: repo (Full repository), read:org

# 创建仓库
export GITHUB_TOKEN="ghp_xxxxxxxxxxxxx"
export GITHUB_USER="your-github-username"
export REPO_NAME="SOP_Agentic_Graph_LLM"

# 创建空 GitHub 仓库（不初始化 README）
curl -s -X POST "https://api.github.com/user/repos" \
  -H "Authorization: token $GITHUB_TOKEN" \
  -d "{\"name\":\"$REPO_NAME\",\"description\":\"SOP知识库自维护自提升系统 - 多服务RAG管线\",\"private\":false}" \
  | jq -r .html_url

# 添加 remote 并推送
cd /data/SOP_Agentic_Graph_LLM
git remote add origin "https://github.com/$GITHUB_USER/$REPO_NAME.git"
git remote set-url origin "https://$GITHUB_TOKEN@github.com/$GITHUB_USER/$REPO_NAME.git"
git branch -M main
git push -u origin main --force
```

---

## 阶段 1: 服务器环境检查

### 1.1 基础环境检查

```bash
cd /data/SOP_Agentic_Graph_LLM

# 检查操作系统
cat /etc/redhat-release
# 期望: RHEL 8.x 或 RHEL 9.x

# 检查内核版本
uname -r
# 期望: >= 5.4

# 检查 NVIDIA 驱动
nvidia-smi
# 期望: Driver Version: 535.161.08, CUDA: 12.2

# 检查 Docker 版本
docker --version
# 期望: Docker version 19.03.15 or higher

# 检查磁盘空间（需 >100GB 可用）
df -BG / | tail -1
```

### 1.2 运行自动检查脚本

```bash
# 需要 sudo 权限
sudo bash scripts/check_prerequisites.sh

# 预期输出: 所有项 [OK]，如果有 [FAIL] 则需要修复
```

---

## 阶段 2: 环境配置

### 2.1 安装 nvidia-container-toolkit（如未安装）

```bash
# 检查是否已安装
which nvidia-docker || docker run --rm --gpus all nvidia/cuda:12.2.0-base nvidia-smi

# 如果 nvidia-smi 在容器内失败，执行安装：
sudo dnf config-manager --add-repo=https://nvidia.github.io/libnvidia-container/rhel$(cat /etc/redhat-release | grep -oE '[0-9]+' | head -1)/nvidia-docker.repo
sudo dnf install -y nvidia-docker2
sudo systemctl restart docker

# 验证
docker run --rm --gpus all nvidia/cuda:12.2.0-base nvidia-smi --query-gpu=name,memory.total --format=csv
```

### 2.2 配置 pip 镜像（JFrog）

```bash
# 编辑 pip 配置
mkdir -p ~/.pip
cat > ~/.pip/pip.conf << 'EOF'
[global]
index-url = http://jfrogreader:AP7SoAxHBehQfx7oGp1VSeCqzm6@jfrog.catlbattery.com/pypi/simple
trusted-host = jfrog.catlbattery.com
timeout = 120
retries = 3
EOF

# 验证
pip config list
```

### 2.3 配置防火墙（如需要）

```bash
# 查看当前防火墙状态
sudo firewall-cmd --state

# 如需开放端口（交互式）
sudo bash scripts/setup_environment.sh

# 或手动开放关键端口：
for port in 9000 9001 5432 6379 6333 6334 7474 7687 8000 8001 8002 8003 8004 8005 8006 8007; do
  sudo firewall-cmd --permanent --add-port=$port/tcp 2>/dev/null
done
sudo firewall-cmd --reload
```

---

## 阶段 3: 配置环境变量

```bash
cd /data/SOP_Agentic_Graph_LLM

# 从模板创建 .env
cp .env.example .env

# 编辑 .env（必填项）
vim .env
```

**`.env` 必填配置项：**

```bash
# =============================================================================
# 公司 LLM API（主路 - CATL 内网服务）
# =============================================================================
COMPANY_LLM_API_BASE=https://llm.catlbattery.com/v1    # CATL Kimi K2.5 或同类
COMPANY_LLM_API_KEY=your-company-api-key-here
COMPANY_LLM_MODEL=kimi-k2.5

# =============================================================================
# 本地 LLM（回落路 - 需先部署 vLLM）
# =============================================================================
LOCAL_LLM_API_BASE=http://localhost:8000/v1            # vLLM 服务地址
LOCAL_LLM_MODEL=qwen2.5-7b-instruct                   # 本地模型名称

# =============================================================================
# MinIO
# =============================================================================
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=changeme_minio_password

# =============================================================================
# PostgreSQL
# =============================================================================
POSTGRES_DB=agentic_graph
POSTGRES_USER=pipeline_user
POSTGRES_PASSWORD=changeme_postgres_password

# =============================================================================
# Neo4j
# =============================================================================
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme_neo4j_password

# =============================================================================
# GPU 配置
# =============================================================================
ENABLE_GPU=true
CUDA_VISIBLE_DEVICES=0
```

---

## 阶段 4: 启动服务

### 4.1 启动基础设施服务（第一步）

```bash
cd /data/SOP_Agentic_Graph_LLM

# 先启动基础设施（MinIO/PostgreSQL/Redis/Qdrant/Neo4j）
make start-infra

# 检查状态
make status

# 预期输出: 所有基础设施容器 running + healthy
# 如果有容器 unhealthy，等待 2-3 分钟后再检查
```

### 4.2 健康检查基础设施

```bash
# 检查所有基础设施端口
for port in 9000 9001 5432 6379 6333 7474 7687; do
  echo -n "Port $port: "
  nc -z localhost $port && echo "OK" || echo "FAIL"
done

# 或运行网络检查脚本
bash scripts/network_check.sh
```

### 4.3 启动全部服务

```bash
# 启动全部微服务
make start-full

# 实时查看启动日志（另一个终端）
make logs-follow

# 检查所有服务健康状态
for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
  echo -n "Service on $port: "
  curl -sf "http://localhost:$port/health" && echo "OK" || echo "FAIL"
done
```

---

## 阶段 5: 验证部署

### 5.1 服务端点验证

```bash
# Pipeline orchestrator (统一入口)
curl http://localhost:8000/
# 期望: {"service": "Pipeline Orchestrator", ...}

# MyMuPDF (PDF/PPTX 解析)
curl http://localhost:8001/
# 期望: {"service": "MyMuPDF", ...}

# Graphify (知识图谱)
curl http://localhost:8004/llm-status
# 期望: {"primary_available": true/false, "fallback_available": true/false, ...}

# Agentic RAG (RAG 查询)
curl http://localhost:8005/llm-status
# 期望: {"primary_available": true/false, "fallback_available": true/false, ...}

# Agentic Wiki
curl http://localhost:8006/
# 期望: {"service": "Agentic Wiki", ...}

# Knowledge Manager
curl http://localhost:8007/
# 期望: {"service": "Knowledge Manager", ...}
```

### 5.2 上传测试文档（PDF）

```bash
# 测试 PDF 处理
curl -X POST http://localhost:8001/process \
  -F "file=@/path/to/test.pdf" \
  -F "enable_vision=true"

# 预期返回: {"doc_id": "xxx", "status": "processed", "pages": N, "elements": [...]}
```

### 5.3 测试端到端管线

```bash
# 通过 pipeline orchestrator 上传文档
curl -X POST http://localhost:8000/upload \
  -H "Content-Type: application/json" \
  -d '{"filename": "test.pdf"}'

# 检查处理状态
curl http://localhost:8000/status/{doc_id}

# 等待几分钟后测试 RAG 查询
curl -X POST http://localhost:8005/query \
  -H "Content-Type: application/json" \
  -d '{"query": "这个文档的主要内容是什么？"}'
```

---

## 阶段 6: 已知问题和解决方案

### 问题 1: nvidia-smi 在容器内失败

```bash
# 检查 NVIDIA runtime
docker info | grep -i nvidia

# 如果没有，重启 Docker
sudo systemctl restart docker

# 再次测试
docker run --rm --gpus all nvidia/cuda:12.2.0-base nvidia-smi
```

### 问题 2: 端口被占用

```bash
# 查找占用端口的进程
sudo ss -tulpn | grep :8001

# 杀掉或修改 docker-compose.yml 中的端口映射
```

### 问题 3: LLM API 调用失败（主路+回落路都失败）

```bash
# 检查主路（公司内网）
curl -I https://llm.catlbattery.com/v1/models

# 检查回落路（本地 vLLM）
curl -I http://localhost:8000/v1/models

# 查看服务日志
docker logs pipeline-graphify --tail 50
```

### 问题 4: Qdrant 向量检索无结果

```bash
# 检查 Qdrant collection
curl http://localhost:6333/collections

# 如果 collection 不存在，重启 graphify 会自动创建
docker-compose restart graphify
```

---

## 阶段 7: 停止和清理

```bash
# 停止所有服务（优雅停止）
make stop

# 停止并删除容器（保留数据卷）
make stop
bash scripts/stop_services.sh --cleanup

# 完全清理（删除所有数据）
bash scripts/stop_services.sh --deep
```

---

## 快速命令汇总

```bash
# 首次部署
sudo bash scripts/check_prerequisites.sh          # 检查环境
sudo bash scripts/setup_environment.sh             # 安装 nvidia-docker2 等
cp .env.example .env && vim .env                  # 配置环境变量
make start-infra                                   # 启动基础设施
make start-full                                    # 启动全部服务

# 日常操作
make status          # 查看状态
make logs            # 查看日志
make logs SERVICE=mymupdf  # 查看指定服务日志
make stop           # 停止服务

# 故障排查
bash scripts/check_prerequisites.sh               # 环境检查
bash scripts/network_check.sh                     # 网络连通性
docker-compose ps                                  # 容器状态
docker-compose logs -f <service-name>             # 服务日志
```

---

## 目录结构参考

```
/data/SOP_Agentic_Graph_LLM/
├── docker-compose.yml      # 服务编排
├── Makefile               # 一键命令
├── .env                   # 环境变量（敏感）
├── .env.example           # 环境变量模板
├── README.md
├── monitoring/
│   └── prometheus.yml
├── scripts/
│   ├── check_prerequisites.sh   # 环境检查
│   ├── setup_environment.sh      # 环境配置
│   ├── network_check.sh          # 网络检查
│   ├── start_services.sh         # 启动服务
│   └── stop_services.sh           # 停止服务
├── utils/
│   └── llm_client.py              # LLM 双路调用
├── mymupdf/           # PDF/PPTX 解析 + Qwen-VL
├── mineru/            # 深度文档理解
├── graphify/          # 知识图谱构建
├── agentic-rag/       # 多Agent RAG
├── agentic-wiki/      # 知识库 UI
├── pipeline/          # 管线编排器 (8000)
└── knowledge-manager/ # 知识管理 (8007)
```

---

**部署检查完成标准：**
- `make status` 显示所有容器 running
- `curl http://localhost:8000/health` 返回 `{"status": "ok"}`
- 6 个微服务 + 2 个新服务全部 `health` 返回 OK
- 上传测试 PDF 后，`GET /status/{doc_id}` 显示 `status: queryable`

---

*最后更新: 2026-06-05*