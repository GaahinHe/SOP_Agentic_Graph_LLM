# SPDX-License-Identifier: MIT
# Knowledge Manager Dockerfile

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY utils/ ./utils/

EXPOSE 8007
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
    CMD curl -fsS http://localhost:8007/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007", "--workers", "2"]