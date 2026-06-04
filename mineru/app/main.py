# SPDX-License-Identifier: MIT
# MinerU Wrapper Service - HTTP API Frontend
# Coordinates between MinerU Core and other services

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import uuid
import redis
import minio
import os
import logging
from datetime import datetime
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MinerU Wrapper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
MINERU_CORE_URL = os.getenv("MINERU_CORE_URL", "http://mineru-core:8003")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "1"))
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "changeme")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "documents")
WORKER_COUNT = int(os.getenv("MINERU_WORKER_COUNT", "4"))
MAX_PAGE_COUNT = int(os.getenv("MAX_PAGE_COUNT", "500"))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

minio_client = minio.Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)

class ProcessResponse(BaseModel):
    doc_id: str
    status: str
    pages: int
    elements: List[Dict[str, Any]]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mineru-wrapper", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return {
        "service": "MinerU Wrapper",
        "version": "1.0.0",
        "description": "Document structure extraction service - coordinates MinerU Core"
    }

@app.post("/process", response_model=ProcessResponse)
async def process_document(
    file: UploadFile = File(...),
    backend: str = "hybrid-auto-engine",
    lang: str = "ch",
    formula_enable: bool = True,
    table_enable: bool = True,
    ocr_enable: bool = True
):
    """Process document and extract structured content"""
    doc_id = str(uuid.uuid4())

    try:
        contents = await file.read()

        # Check page count
        if len(contents) > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 100MB)")

        # Store in Redis for tracking
        redis_client.hset(f"mineru:{doc_id}", mapping={
            "status": "processing",
            "backend": backend,
            "lang": lang,
            "created_at": datetime.utcnow().isoformat()
        })

        # Call MinerU Core
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{MINERU_CORE_URL}/process",
                json={
                    "doc_id": doc_id,
                    "pages": [{"page_num": 0, "text": ""}],  # Simplified
                    "options": {
                        "formula_enable": formula_enable,
                        "table_enable": table_enable,
                        "ocr_enable": ocr_enable
                    }
                }
            )
            response.raise_for_status()
            result = response.json()

        return ProcessResponse(
            doc_id=doc_id,
            status="processed",
            pages=1,
            elements=result.get("elements", [])
        )

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{doc_id}")
async def get_status(doc_id: str):
    """Get document processing status"""
    data = redis_client.hgetall(f"mineru:{doc_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Document not found")
    return data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002, workers=WORKER_COUNT)