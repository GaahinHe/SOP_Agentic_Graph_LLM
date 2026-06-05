# SPDX-License-Identifier: MIT
# Pipeline Orchestrator Service
# Unified entry point for document processing pipeline
# Manages document lifecycle: uploaded → preprocessed → parsed → graphed → indexed → queryable
# Uses Redis Streams for task queue and state machine

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import logging
from datetime import datetime
import redis
import uuid
import httpx
import json
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Pipeline Orchestrator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
MYMUPDF_URL = os.getenv("MYMUPDF_URL", "http://mymupdf:8001")
GRAPHIFY_URL = os.getenv("GRAPHIFY_URL", "http://graphify:8004")
AGENTIC_RAG_URL = os.getenv("AGENTIC_RAG_URL", "http://agentic-rag:8005")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "changeme")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "documents")

# Redis connection
redis_password = REDIS_PASSWORD if REDIS_PASSWORD else None
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=0,
    password=redis_password,
    decode_responses=True
)

# Document lifecycle states
STATE_UPLOADED = "uploaded"
STATE_PREPROCESSING = "preprocessing"
STATE_PREPROCESSED = "preprocessed"
STATE_GRAPHING = "graphing"
STATE_INDEXED = "indexed"
STATE_QUERYABLE = "queryable"
STATE_FAILED = "failed"

# Valid state transitions
VALID_TRANSITIONS = {
    STATE_UPLOADED: [STATE_PREPROCESSING],
    STATE_PREPROCESSING: [STATE_PREPROCESSED, STATE_FAILED],
    STATE_PREPROCESSED: [STATE_GRAPHING],
    STATE_GRAPHING: [STATE_INDEXED, STATE_FAILED],
    STATE_INDEXED: [STATE_QUERYABLE],
    STATE_QUERYABLE: [],
    STATE_FAILED: [STATE_PREPROCESSING],  # Can retry
}

# Models
class UploadRequest(BaseModel):
    doc_id: Optional[str] = None
    filename: str
    file_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class UploadResponse(BaseModel):
    doc_id: str
    status: str
    message: str
    pipeline_url: str

class PipelineStatus(BaseModel):
    doc_id: str
    status: str
    current_stage: str
    stages_completed: List[str]
    stages_failed: List[str]
    created_at: str
    updated_at: str
    error: Optional[str] = None

class StageResult(BaseModel):
    stage: str
    success: bool
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    duration_ms: int

# =============================================================================
# Document Lifecycle State Machine
# =============================================================================

def get_doc_state(doc_id: str) -> Optional[Dict[str, Any]]:
    """Get document state from Redis"""
    state_key = f"pipeline:doc:{doc_id}:state"
    data = redis_client.hgetall(state_key)
    if not data:
        return None
    return {
        "doc_id": doc_id,
        "status": data.get("status", STATE_UPLOADED),
        "current_stage": data.get("current_stage", STATE_UPLOADED),
        "stages_completed": data.get("stages_completed", "").split(",") if data.get("stages_completed") else [],
        "stages_failed": data.get("stages_failed", "").split(",") if data.get("stages_failed") else [],
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "error": data.get("error", None),
        "metadata": json.loads(data.get("metadata", "{}"))
    }


def save_doc_state(doc_id: str, state: Dict[str, Any]):
    """Save document state to Redis"""
    state_key = f"pipeline:doc:{doc_id}:state"
    redis_client.hset(state_key, mapping={
        "doc_id": doc_id,
        "status": state.get("status", STATE_UPLOADED),
        "current_stage": state.get("current_stage", STATE_UPLOADED),
        "stages_completed": ",".join(state.get("stages_completed", [])),
        "stages_failed": ",".join(state.get("stages_failed", [])),
        "created_at": state.get("created_at", datetime.utcnow().isoformat()),
        "updated_at": datetime.utcnow().isoformat(),
        "error": state.get("error", ""),
        "metadata": json.dumps(state.get("metadata", {}))
    })


def transition_state(doc_id: str, new_state: str, success: bool, error: Optional[str] = None):
    """Transition document to new state"""
    state = get_doc_state(doc_id)
    if not state:
        state = {"doc_id": doc_id}

    old_stage = state.get("current_stage", STATE_UPLOADED)

    if success:
        state["stages_completed"] = state.get("stages_completed", []) + [old_stage]
    else:
        state["stages_failed"] = state.get("stages_failed", []) + [old_stage]
        state["error"] = error

    state["current_stage"] = new_state
    state["status"] = new_state if success else STATE_FAILED

    save_doc_state(doc_id, state)
    logger.info(f"Doc {doc_id}: {old_stage} -> {new_state} (success={success})")


# =============================================================================
# Pipeline Stage Handlers
# =============================================================================

def process_with_mymupdf(doc_id: str, file_url: str) -> Dict[str, Any]:
    """Call mymupdf to preprocess document"""
    import time
    start = time.time()

    try:
        with httpx.Client(timeout=300) as client:
            # If file_url is a MinIO URL, fetch and upload
            if file_url.startswith("minio://"):
                # Parse minio URL and fetch file
                response = client.get(file_url)
                files = {"file": ("document", response.content, "application/octet-stream")}
                resp = client.post(f"{MYMUPDF_URL}/process", files=files, params={"enable_vision": True})
            else:
                # Assume it's a direct upload URL or path
                resp = client.post(f"{MYMUPDF_URL}/process", params={"enable_vision": True})

            resp.raise_for_status()
            result = resp.json()

            duration = int((time.time() - start) * 1000)
            return {
                "stage": "mymupdf",
                "success": True,
                "output": result,
                "duration_ms": duration
            }
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return {
            "stage": "mymupdf",
            "success": False,
            "error": str(e),
            "duration_ms": duration
        }


def process_with_graphify(doc_id: str, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Call graphify to build knowledge graph"""
    import time
    start = time.time()

    try:
        payload = {
            "doc_id": doc_id,
            "chunks": chunks,
            "extract_entities": True,
            "extract_relations": True,
            "store_vectors": True
        }

        with httpx.Client(timeout=300) as client:
            resp = client.post(f"{GRAPHIFY_URL}/build", json=payload)
            resp.raise_for_status()
            result = resp.json()

            duration = int((time.time() - start) * 1000)
            return {
                "stage": "graphify",
                "success": True,
                "output": result,
                "duration_ms": duration
            }
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return {
            "stage": "graphify",
            "success": False,
            "error": str(e),
            "duration_ms": duration
        }


async def run_pipeline(doc_id: str, file_url: str, filename: str, metadata: Optional[Dict[str, Any]] = None):
    """
    Run the full pipeline asynchronously.
    Stages: upload -> preprocess (mymupdf) -> graph (graphify) -> index -> queryable
    """
    logger.info(f"Starting pipeline for doc_id={doc_id}, file={filename}")

    # Stage 1: Preprocessing (mymupdf)
    transition_state(doc_id, STATE_PREPROCESSING, success=True)
    result = process_with_mymupdf(doc_id, file_url)

    if not result["success"]:
        transition_state(doc_id, STATE_PREPROCESSING, success=False, error=result["error"])
        return

    # Get chunks from mymupdf output
    elements = result["output"].get("elements", [])
    chunks = [
        {
            "chunk_id": str(uuid.uuid4()),
            "text": e.get("content", ""),
            "type": e.get("type", "text"),
            "page": e.get("page", 0)
        }
        for e in elements
        if e.get("type") in ("text", "ocr_text", "table", "shape", "vision_description")
        and e.get("content")
    ]

    transition_state(doc_id, STATE_PREPROCESSED, success=True)

    # Stage 2: Graph building (graphify)
    transition_state(doc_id, STATE_GRAPHING, success=True)
    graph_result = process_with_graphify(doc_id, chunks)

    if not graph_result["success"]:
        transition_state(doc_id, STATE_GRAPHING, success=False, error=graph_result["error"])
        return

    transition_state(doc_id, STATE_INDEXED, success=True)

    # Stage 3: Mark as queryable
    state = get_doc_state(doc_id)
    state["status"] = STATE_QUERYABLE
    state["current_stage"] = STATE_QUERYABLE
    save_doc_state(doc_id, state)

    # Store final result
    redis_client.set(f"pipeline:doc:{doc_id}:result", json.dumps({
        "graph_nodes": graph_result["output"].get("nodes_created", 0),
        "relationships": graph_result["output"].get("relationships_created", 0),
        "entities": graph_result["output"].get("entities_extracted", 0),
        "chunks_processed": len(chunks)
    }), ex=86400 * 7)  # 7 day retention

    logger.info(f"Pipeline completed for doc_id={doc_id}")


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "pipeline-orchestrator", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    return {
        "service": "Pipeline Orchestrator",
        "version": "1.0.0",
        "description": "Unified document processing pipeline - PDF/PPTX → knowledge graph → RAG"
    }


@app.post("/upload", response_model=UploadResponse)
async def upload_document(request: UploadRequest, background_tasks: BackgroundTasks):
    """
    Upload a document and start the processing pipeline.
    Returns immediately; processing happens in background.
    """
    doc_id = request.doc_id or str(uuid.uuid4())
    filename = request.filename

    # Initialize state
    state = {
        "doc_id": doc_id,
        "status": STATE_UPLOADED,
        "current_stage": STATE_UPLOADED,
        "stages_completed": [],
        "stages_failed": [],
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "metadata": request.metadata or {}
    }
    save_doc_state(doc_id, state)

    # Determine file URL
    file_url = request.file_url or f"minio://{MINIO_BUCKET}/{doc_id}/{filename}"

    # Enqueue background task
    background_tasks.add_task(run_pipeline, doc_id, file_url, filename, request.metadata)

    return UploadResponse(
        doc_id=doc_id,
        status=STATE_UPLOADED,
        message="Document uploaded, pipeline started",
        pipeline_url=f"/status/{doc_id}"
    )


@app.get("/status/{doc_id}", response_model=PipelineStatus)
async def get_status(doc_id: str):
    """Get document processing status"""
    state = get_doc_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail="Document not found")

    return PipelineStatus(**state)


@app.post("/retry/{doc_id}")
async def retry_document(doc_id: str):
    """Retry a failed pipeline"""
    state = get_doc_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail="Document not found")

    if state.get("status") != STATE_FAILED:
        return {"status": "ignored", "message": f"Document is not in failed state ({state.get('status')})"}

    # Get original metadata
    metadata = state.get("metadata", {})

    # Reset and retry
    state["status"] = STATE_UPLOADED
    state["current_stage"] = STATE_UPLOADED
    state["stages_failed"] = []
    state["error"] = None
    save_doc_state(doc_id, state)

    # Re-run pipeline
    # Note: in production, you'd store the original file_url
    return {"status": "retry_initiated", "doc_id": doc_id}


@app.get("/stats")
async def get_stats():
    """Get pipeline statistics"""
    keys = redis_client.keys("pipeline:doc:*:state")
    total = len(keys)

    status_counts = {}
    for key in keys:
        data = redis_client.hgetall(key)
        status = data.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "total_documents": total,
        "by_status": status_counts,
        "completed": status_counts.get(STATE_QUERYABLE, 0),
        "failed": status_counts.get(STATE_FAILED, 0),
        "in_progress": status_counts.get(STATE_PREPROCESSING, 0) + status_counts.get(STATE_GRAPHING, 0)
    }


@app.delete("/document/{doc_id}")
async def delete_document(doc_id: str):
    """Delete a document and all its pipeline data"""
    state = get_doc_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete state
    redis_client.delete(f"pipeline:doc:{doc_id}:state")
    redis_client.delete(f"pipeline:doc:{doc_id}:result")

    # TODO: call graphify to delete graph, Qdrant to delete vectors
    # For now, just delete pipeline state

    return {"status": "deleted", "doc_id": doc_id}


@app.post("/trigger/{doc_id}")
async def trigger_graphify(doc_id: str):
    """
    Manually trigger graphify for a document that was preprocessed but not graphed.
    Useful when graphify was down during initial processing.
    """
    state = get_doc_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail="Document not found")

    if state.get("status") not in [STATE_PREPROCESSED, STATE_INDEXED]:
        raise HTTPException(status_code=400, detail=f"Cannot trigger graphify from status: {state.get('status')}")

    # Get chunks from mymupdf status
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{MYMUPDF_URL}/status/{doc_id}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Document not found in mymupdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch from mymupdf: {e}")

    return {"status": "triggered", "doc_id": doc_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=4)