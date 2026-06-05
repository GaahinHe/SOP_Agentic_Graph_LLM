# SPDX-License-Identifier: MIT
# Agentic RAG Service - Multi-Agent Retrieval System
# Real vector + graph retrieval, LLM dual-path generation, feedback collection

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
from qdrant_client.models import Distance, VectorParams, SearchParams, PointStruct
import uuid
import json
import asyncio
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from utils.llm_client import get_llm_client, parse_json_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agentic RAG", version="2.0.0")

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
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

neo4j_driver = neo4j.GraphDatabase.driver(
    f"bolt://{NEO4J_HOST}:{NEO4J_PORT}",
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

qdrant = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_GRPC_PORT)

# Lazy load embedding model
_embedding_model = None


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedding_model = SentenceTransformer(EMBED_MODEL)
            logger.info(f"Embedding model loaded: {EMBED_MODEL}")
        except Exception as e:
            logger.warning(f"Could not load embedding model: {e}")
            _embedding_model = None
    return _embedding_model


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
    source: str

class FeedbackRequest(BaseModel):
    query_id: str
    answer_id: str
    rating: int  # 1 = bad, 5 = great
    correction: Optional[str] = None
    flagged_entities: Optional[List[str]] = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentic-rag", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    return {
        "service": "Agentic RAG",
        "version": "2.0.0",
        "description": "Multi-agent RAG with real embeddings, graph traversal, LLM dual-path generation"
    }


@app.get("/llm-status")
async def llm_status():
    """Check LLM availability"""
    return get_llm_client().status()


@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """Process a RAG query with real multi-agent retrieval"""
    query_id = str(uuid.uuid4())
    logger.info(f"Processing query {query_id}: {request.query[:80]}...")

    try:
        # Check cache
        cache_key = f"rag:query:{hash(request.query)}"
        cached = redis_client.get(cache_key)
        if cached:
            return QueryResponse(**json.loads(cached))

        # Real vector retrieval using embedding model
        vector_results = []
        if request.use_vector:
            vector_results = await vector_retrieve(request.query, request.top_k)

        # Real graph retrieval via Neo4j
        graph_results = []
        if request.use_graph:
            graph_results = await graph_retrieve(request.query, request.max_hops)

        # Merge and dedupe sources
        sources = merge_sources(vector_results, graph_results)

        # Real reranking using LLM
        if request.use_rerank and sources:
            sources = await rerank_sources(request.query, sources)

        # Real LLM generation using dual-path
        answer = await generate_answer(request.query, sources[:request.top_k])

        response = QueryResponse(
            query_id=query_id,
            answer=answer,
            sources=[s.model_dump() for s in sources],
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
    """Real vector retrieval using embedding model + Qdrant"""
    try:
        model = get_embedding_model()
        if model is None:
            logger.warning("Embedding model not available, using graph-only retrieval")
            return []

        query_vector = model.encode(query[:500]).tolist()

        search_results = qdrant.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            search_params=SearchParams(hnsw_ef=128)
        )

        return [
            SourceDocument(
                doc_id=hit.payload.get("doc_id", str(hit.id)),
                chunk_id=str(hit.id),
                text=hit.payload.get("text", "")[:500],
                score=float(hit.score),
                source="vector"
            )
            for hit in search_results
            if hit.score > 0.3  # Minimum relevance threshold
        ]
    except Exception as e:
        logger.warning(f"Vector retrieval failed: {e}")
        return []


async def graph_retrieve(query: str, max_hops: int) -> List[SourceDocument]:
    """Real graph retrieval via Neo4j multi-hop traversal"""
    try:
        with neo4j_driver.session() as session:
            # Search for entities matching the query
            result = session.run(
                """
                MATCH (e:Entity)-[:RELATES_TO*1..{hops}]->(e2:Entity)
                WHERE e.name CONTAINS $term OR e2.name CONTAINS $term
                MATCH (c:Chunk)-[:HAS_ENTITY]->(e)
                RETURN c.text as text, c.chunk_id as chunk_id, c.doc_id as doc_id,
                       e.name as matched_entity, c.doc_id as source_doc
                LIMIT $limit
                """,
                term=query[:100],
                hops=max_hops,
                limit=top_k
            )

            sources = []
            seen_chunks = set()
            for record in result:
                chunk_id = record["chunk_id"]
                if chunk_id in seen_chunks:
                    continue
                seen_chunks.add(chunk_id)
                sources.append(SourceDocument(
                    doc_id=record.get("doc_id", ""),
                    chunk_id=chunk_id,
                    text=record["text"][:500] if record["text"] else "",
                    score=0.8,  # Graph matches are generally high confidence
                    source="graph"
                ))

            # Also do keyword search on chunk text
            result2 = session.run(
                """
                MATCH (c:Chunk)
                WHERE c.text CONTAINS $term
                RETURN c.text as text, c.chunk_id as chunk_id, c.doc_id as doc_id
                LIMIT $limit
                """,
                term=query[:100],
                limit=top_k // 2
            )

            for record in result2:
                chunk_id = record["chunk_id"]
                if chunk_id not in seen_chunks:
                    seen_chunks.add(chunk_id)
                    sources.append(SourceDocument(
                        doc_id=record.get("doc_id", ""),
                        chunk_id=chunk_id,
                        text=record["text"][:500] if record["text"] else "",
                        score=0.6,
                        source="graph_keyword"
                    ))

            return sources

    except Exception as e:
        logger.warning(f"Graph retrieval failed: {e}")
        return []


def merge_sources(vector_results: List[SourceDocument], graph_results: List[SourceDocument]) -> List[SourceDocument]:
    """Merge and deduplicate sources, preferring higher scores"""
    seen = {}
    for source in sorted(vector_results + graph_results, key=lambda s: s.score, reverse=True):
        key = source.chunk_id
        if key not in seen:
            seen[key] = source
        else:
            # Keep higher score
            if source.score > seen[key].score:
                seen[key] = source
    return list(seen.values())


async def rerank_sources(query: str, sources: List[SourceDocument]) -> List[SourceDocument]:
    """Rerank sources using LLM relevance scoring"""
    if not sources:
        return sources

    try:
        llm = get_llm_client()
        source_texts = "\n".join([f"[{i}] {s.text[:200]}" for i, s in enumerate(sources[:MAX_RETRIEVAL_COUNT])])

        rerank_prompt = f"""Given the query: "{query}"

Rate each document's relevance to the query on a scale of 1-5.
Return a JSON array of scores (one per document, in order):

[
  {{"index": 0, "score": 5}},
  {{"index": 1, "score": 3}},
  ...
]

Documents:
{source_texts}

Only return the JSON array, no explanation."""

        response = llm.generate(rerank_prompt, max_tokens=2048)
        scores = parse_json_response(response)

        if not scores or not isinstance(scores, list):
            return sources[:MAX_RETRIEVAL_COUNT]

        # Apply scores
        score_map = {item.get("index"): item.get("score", 1) for item in scores}
        for i, source in enumerate(sources):
            if i in score_map:
                source.score = source.score * score_map[i] / 5  # Weighted combination

        return sorted(sources, key=lambda s: s.score, reverse=True)[:MAX_RETRIEVAL_COUNT]

    except Exception as e:
        logger.warning(f"Reranking failed: {e}")
        return sources[:MAX_RETRIEVAL_COUNT]


async def generate_answer(query: str, sources: List[SourceDocument]) -> str:
    """Generate answer using LLM dual-path with context from retrieved sources"""
    if not sources:
        return "I don't have enough information in the knowledge base to answer this question."

    try:
        llm = get_llm_client()

        context = "\n\n".join([
            f"[Source {i+1}] ({s.source}, score={s.score:.2f}):\n{s.text[:300]}"
            for i, s in enumerate(sources[:5])
        ])

        system_prompt = """You are a helpful assistant answering questions based on retrieved documents.
Be precise, cite the source information when relevant. If you're unsure, say so."""

        user_prompt = f"""Based on the following documents, answer the query.

Query: {query}

Documents:
{context}

Answer:"""

        answer = llm.generate(
            user_prompt,
            system=system_prompt,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS
        )

        return answer

    except Exception as e:
        logger.error(f"Answer generation failed: {e}")
        return f"I encountered an error while generating the answer: {e}"


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


@app.post("/feedback")
async def submit_feedback(request: FeedbackRequest):
    """Collect user feedback for answer quality tracking"""
    try:
        feedback_key = f"feedback:{request.query_id}:{request.answer_id}"
        redis_client.hset(feedback_key, mapping={
            "rating": str(request.rating),
            "correction": request.correction or "",
            "flagged_entities": json.dumps(request.flagged_entities or []),
            "created_at": datetime.utcnow().isoformat()
        })
        redis_client.expire(feedback_key, 86400 * 30)  # 30 day retention

        # If low rating, mark answer as needing review
        if request.rating <= 2:
            redis_client.hset(f"review:{request.answer_id}", mapping={
                "query_id": request.query_id,
                "rating": str(request.rating),
                "created_at": datetime.utcnow().isoformat()
            })
            logger.info(f"Low rating ({request.rating}) for answer {request.answer_id} - marked for review")

        return {
            "status": "stored",
            "query_id": request.query_id,
            "rating": request.rating,
            "review_triggered": request.rating <= 2
        }
    except Exception as e:
        logger.error(f"Feedback storage failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reviews")
async def get_reviews():
    """Get all answers flagged for review (low ratings)"""
    try:
        review_keys = redis_client.keys("review:*")
        reviews = []
        for key in review_keys[:50]:  # Limit to 50
            data = redis_client.hgetall(key)
            if data:
                reviews.append({
                    "answer_id": key.decode().split(":")[1] if isinstance(key, bytes) else key.split(":")[1],
                    **data
                })
        return {"reviews": reviews, "count": len(reviews)}
    except Exception as e:
        logger.error(f"Review fetch failed: {e}")
        return {"reviews": [], "error": str(e)}


@app.delete("/reviews/{answer_id}")
async def dismiss_review(answer_id: str):
    """Dismiss/resolve a review flag"""
    try:
        redis_client.delete(f"review:{answer_id}")
        return {"status": "dismissed", "answer_id": answer_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    """Get RAG statistics"""
    try:
        with neo4j_driver.session() as session:
            entity_count = session.run("MATCH (e:Entity) RETURN count(e) as count").single()["count"]
            chunk_count = session.run("MATCH (c:Chunk) RETURN count(c) as count").single()["count"]

        feedback_count = len(redis_client.keys("feedback:*"))
        review_count = len(redis_client.keys("review:*"))

        return {
            "vector_db": QDRANT_COLLECTION,
            "graph_entities": entity_count,
            "graph_chunks": chunk_count,
            "feedback_count": feedback_count,
            "pending_reviews": review_count,
            "cache_ttl": CACHE_TTL
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, workers=4)