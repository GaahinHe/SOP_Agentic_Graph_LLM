# SPDX-License-Identifier: MIT
# Agentic RAG Service - Multi-Agent Retrieval System
# Coordinates vector search, graph traversal, and LLM generation

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import logging
from datetime import datetime
import redis
import neo4j
import qdrant_client
from qdrant_client.models import Distance, VectorParams, SearchParams
import uuid
import json
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agentic RAG", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_GRPC_PORT = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "document_chunks")
NEO4J_HOST = os.getenv("NEO4J_HOST", "neo4j")
NEO4J_PORT = int(os.getenv("NEO4J_PORT", "7687"))
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "changeme")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "agentic_graph")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "changeme")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "3"))
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "10"))
MAX_RETRIEVAL_COUNT = int(os.getenv("MAX_RETRIEVAL_COUNT", "20"))
CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "3600"))
LLM_API_BASE = os.getenv("AGENTIC_RAG_MODEL_BASE_URL", "https://api.minimax.io/anthropic")
LLM_API_KEY = os.getenv("AGENTIC_RAG_MODEL_API_KEY", "")
LLM_MODEL = os.getenv("AGENTIC_RAG_MODEL", "MiniMax-M3")

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

neo4j_driver = neo4j.GraphDatabase.driver(
    f"bolt://{NEO4J_HOST}:{NEO4J_PORT}",
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

qdrant = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_GRPC_PORT)

# Models
class QueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = 10
    max_hops: Optional[int] = 3
    use_graph: bool = True
    use_vector: bool = True
    use_rerank: bool = True

class QueryResponse(BaseModel):
    query_id: str
    answer: str
    sources: List[Dict[str, Any]]
    metadata: Dict[str, Any]

class SourceDocument(BaseModel):
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source: str  # 'vector' or 'graph'

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentic-rag", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return {
        "service": "Agentic RAG",
        "version": "1.0.0",
        "description": "Multi-agent retrieval system with vector + graph search"
    }

@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """Process a RAG query with multi-agent retrieval"""
    query_id = str(uuid.uuid4())
    logger.info(f"Processing query {query_id}: {request.query[:50]}...")

    try:
        # Check cache
        cache_key = f"rag:query:{hash(request.query)}"
        cached = redis_client.get(cache_key)
        if cached:
            return QueryResponse(**json.loads(cached))

        # Vector retrieval
        vector_results = []
        if request.use_vector:
            vector_results = await vector_retrieve(request.query, request.top_k)

        # Graph retrieval
        graph_results = []
        if request.use_graph:
            graph_results = await graph_retrieve(request.query, request.max_hops)

        # Merge and dedupe sources
        sources = merge_sources(vector_results, graph_results)

        # Rerank if enabled
        if request.use_rerank and sources:
            sources = await rerank_sources(request.query, sources)

        # Generate answer
        answer = await generate_answer(request.query, sources[:request.top_k])

        response = QueryResponse(
            query_id=query_id,
            answer=answer,
            sources=sources,
            metadata={
                "vector_sources": len(vector_results),
                "graph_sources": len(graph_results),
                "total_sources": len(sources)
            }
        )

        # Cache response
        redis_client.setex(cache_key, CACHE_TTL, json.dumps(response.model_dump()))

        return response

    except Exception as e:
        logger.error(f"Error processing query: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def vector_retrieve(query: str, top_k: int) -> List[SourceDocument]:
    """Retrieve from vector database"""
    try:
        search_results = qdrant.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=[0.0] * 3072,  # Placeholder - would use real embedding
            limit=top_k,
            search_params=SearchParams(hnsw_ef=128)
        )
        return [
            SourceDocument(
                doc_id=str(hit.id),
                chunk_id=str(hit.id),
                text=hit.payload.get("text", ""),
                score=hit.score,
                source="vector"
            )
            for hit in search_results
        ]
    except Exception as e:
        logger.warning(f"Vector retrieval failed: {e}")
        return []

async def graph_retrieve(query: str, max_hops: int) -> List[SourceDocument]:
    """Retrieve from knowledge graph"""
    try:
        with neo4j_driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity)-[:RELATES_TO*1..{hops}]->(e2:Entity)
                WHERE e.name CONTAINS $query OR e2.name CONTAINS $query
                MATCH (c:Chunk)-[:HAS_ENTITY]->(e)
                RETURN c.text as text, c.chunk_id as chunk_id, e.name as entity
                LIMIT $limit
                """,
                query=query[:50],
                hops=max_hops,
                limit=top_k
            )
            return [
                SourceDocument(
                    doc_id="",
                    chunk_id=record["chunk_id"],
                    text=record["text"],
                    score=1.0,
                    source="graph"
                )
                for record in result
            ]
    except Exception as e:
        logger.warning(f"Graph retrieval failed: {e}")
        return []

def merge_sources(vector_results: List[SourceDocument], graph_results: List[SourceDocument]) -> List[SourceDocument]:
    """Merge and deduplicate sources from different retrievers"""
    seen = {}
    merged = []

    for source in vector_results + graph_results:
        key = source.chunk_id
        if key not in seen:
            seen[key] = True
            merged.append(source)

    return merged

async def rerank_sources(query: str, sources: List[SourceDocument]) -> List[SourceDocument]:
    """Rerank sources using LLM"""
    # Placeholder - would call LLM for reranking
    return sources[:MAX_RETRIEVAL_COUNT]

async def generate_answer(query: str, sources: List[SourceDocument]) -> str:
    """Generate answer using LLM"""
    # Placeholder - would call LLM API
    context = "\n\n".join([s.text for s in sources[:5]])
    return f"Based on the retrieved documents: {context[:200]}..."

@app.websocket("/ws")
async def websocket_handler(websocket: WebSocket):
    """WebSocket endpoint for streaming responses"""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Echo: {data}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")

@app.get("/stats")
async def get_stats():
    """Get RAG statistics"""
    try:
        with neo4j_driver.session() as session:
            entity_count = session.run("MATCH (e:Entity) RETURN count(e) as count").single()["count"]
        return {
            "vector_db": QDRANT_COLLECTION,
            "graph_entities": entity_count,
            "cache_ttl": CACHE_TTL
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, workers=4)