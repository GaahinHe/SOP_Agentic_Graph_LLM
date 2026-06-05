# SPDX-License-Identifier: MIT
# Graphify Service - Knowledge Graph Construction
# Extracts entities and relationships from documents using LLM dual-path
# and builds Neo4j knowledge graph + Qdrant vector index

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import logging
from datetime import datetime
import redis
import neo4j
import qdrant_client
from qdrant_client.models import Distance, VectorParams, PointStruct
import uuid
import json
import sys

# Add project root to path for utils
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from utils.llm_client import (
    get_llm_client,
    build_entity_extraction_prompt,
    build_relation_extraction_prompt,
    parse_json_response
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Graphify", version="2.0.0")

# Configuration
NEO4J_HOST = os.getenv("NEO4J_HOST", "neo4j")
NEO4J_PORT = int(os.getenv("NEO4J_PORT", "7687"))
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "changeme")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_GRPC_PORT = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "document_chunks")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "2"))
GRAPHIFY_WORKER_COUNT = int(os.getenv("GRAPHIFY_WORKER_COUNT", "4"))
GRAPHIFY_CHUNK_SIZE = int(os.getenv("GRAPHIFY_CHUNK_SIZE", "512"))
GRAPHIFY_MAX_ENTITIES = int(os.getenv("GRAPHIFY_MAX_ENTITIES_PER_CHUNK", "20"))
GRAPHIFY_MAX_RELATIONS = int(os.getenv("GRAPHIFY_MAX_RELATIONS_PER_CHUNK", "15"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

# Neo4j driver
neo4j_driver = neo4j.GraphDatabase.driver(
    f"bolt://{NEO4J_HOST}:{NEO4J_PORT}",
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

# Qdrant client
qdrant_client_instance = qdrant_client.QdrantClient(
    host=QDRANT_HOST,
    port=QDRANT_GRPC_PORT
)

# Models
class GraphBuildRequest(BaseModel):
    doc_id: str
    chunks: List[Dict[str, Any]]
    extract_entities: bool = True
    extract_relations: bool = True
    store_vectors: bool = True

class GraphBuildResponse(BaseModel):
    doc_id: str
    status: str
    nodes_created: int
    relationships_created: int
    entities_extracted: int
    processing_time_ms: int

class EntityResponse(BaseModel):
    entity_type: str
    entity_name: str
    properties: Dict[str, Any]

class RelationResponse(BaseModel):
    from_entity: str
    to_entity: str
    relation_type: str
    properties: Dict[str, Any]

# System prompt for knowledge extraction
ENTITY_EXTRACTION_SYSTEM = """You are an expert at extracting structured entities from technical SOP documents.
Return ONLY JSON array, no markdown, no explanation. Each entity must have name and type."""

RELATION_EXTRACTION_SYSTEM = """You are an expert at extracting relationships between entities from technical SOP documents.
Return ONLY JSON array, no markdown, no explanation. Each relation must have from, to, and type."""


@app.get("/health")
async def health():
    return {"status": "ok", "service": "graphify", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    return {
        "service": "Graphify",
        "version": "2.0.0",
        "description": "Knowledge graph construction - entity/relation extraction via LLM dual-path"
    }


@app.get("/llm-status")
async def llm_status():
    """Check LLM availability (both paths)"""
    client = get_llm_client()
    return client.status()


@app.post("/build", response_model=GraphBuildResponse)
async def build_graph(request: GraphBuildRequest):
    """Build knowledge graph from document chunks using LLM extraction"""
    import time
    start_time = time.time()

    logger.info(f"Building graph for document {request.doc_id}, chunks: {len(request.chunks)}")

    nodes_created = 0
    relationships_created = 0
    entities_extracted = 0
    llm = get_llm_client()

    with neo4j_driver.session() as session:
        # Create or update document node
        session.run(
            "MERGE (d:Document {doc_id: $doc_id}) "
            "SET d.created_at = COALESCE(d.created_at, $now), d.updated_at = $now",
            doc_id=request.doc_id,
            now=datetime.utcnow().isoformat()
        )
        nodes_created += 1

        for chunk in request.chunks:
            chunk_id = chunk.get("chunk_id", str(uuid.uuid4()))
            text = chunk.get("text", "")
            text_type = chunk.get("type", "text")

            if not text or len(text.strip()) < 10:
                continue

            # Truncate to chunk size
            text = text[:GRAPHIFY_CHUNK_SIZE * 2]

            # Create chunk node
            session.run(
                """
                MERGE (c:Chunk {chunk_id: $chunk_id})
                SET c.text = $text,
                    c.doc_id = $doc_id,
                    c.type = $type,
                    c.updated_at = $now
                WITH c
                MATCH (d:Document {doc_id: $doc_id})
                MERGE (d)-[:CONTAINS]->(c)
                """,
                chunk_id=chunk_id,
                text=text[:GRAPHIFY_CHUNK_SIZE],
                doc_id=request.doc_id,
                type=text_type,
                now=datetime.utcnow().isoformat()
            )
            nodes_created += 1

            # Extract and create entities via LLM
            if request.extract_entities:
                entities = extract_entities_with_llm(text, llm)
                for entity in entities[:GRAPHIFY_MAX_ENTITIES]:
                    entity_name = entity.get("name", "")
                    entity_type = entity.get("type", "CONCEPT")
                    if not entity_name:
                        continue

                    session.run(
                        """
                        MERGE (e:Entity {name: $name})
                        SET e.type = $type,
                            e.updated_at = $now
                        WITH e
                        MATCH (c:Chunk {chunk_id: $chunk_id})
                        MERGE (c)-[:HAS_ENTITY]->(e)
                        """,
                        name=entity_name,
                        type=entity_type.upper(),
                        chunk_id=chunk_id,
                        now=datetime.utcnow().isoformat()
                    )
                    nodes_created += 1
                    entities_extracted += 1

            # Extract and create relationships via LLM
            if request.extract_relations:
                relations = extract_relations_with_llm(text, llm)
                for rel in relations[:GRAPHIFY_MAX_RELATIONS]:
                    from_entity = rel.get("from", "")
                    to_entity = rel.get("to", "")
                    rel_type = rel.get("type", "RELATES_TO")
                    if not from_entity or not to_entity:
                        continue

                    # Create entity nodes if they don't exist
                    session.run(
                        "MERGE (e:Entity {name: $name})",
                        name=from_entity
                    )
                    session.run(
                        "MERGE (e:Entity {name: $name})",
                        name=to_entity
                    )

                    # Create relationship
                    session.run(
                        """
                        MATCH (e1:Entity {name: $from}), (e2:Entity {name: $to})
                        MERGE (e1)-[r:RELATES_TO {type: $type}]->(e2)
                        SET r.updated_at = $now
                        """,
                        from=from_entity,
                        to=to_entity,
                        type=rel_type.upper(),
                        now=datetime.utcnow().isoformat()
                    )
                    relationships_created += 1

            # Store vector in Qdrant
            if request.store_vectors and text_type in ("text", "table", "shape"):
                try:
                    store_chunk_vector(chunk_id, request.doc_id, text[:GRAPHIFY_CHUNK_SIZE], {
                        "type": text_type,
                        "chunk_id": chunk_id
                    })
                except Exception as e:
                    logger.warning(f"Vector storage failed for chunk {chunk_id}: {e}")

    # Update Redis status
    redis_client.hset(f"graph:{request.doc_id}", mapping={
        "status": "built",
        "nodes": nodes_created,
        "relationships": relationships_created,
        "entities": entities_extracted,
        "completed_at": datetime.utcnow().isoformat()
    })

    processing_time = int((time.time() - start_time) * 1000)

    return GraphBuildResponse(
        doc_id=request.doc_id,
        status="built",
        nodes_created=nodes_created,
        relationships_created=relationships_created,
        entities_extracted=entities_extracted,
        processing_time_ms=processing_time
    )


def extract_entities_with_llm(text: str, llm) -> List[Dict[str, str]]:
    """
    Extract entities using LLM dual-path client.
    Calls company API first, falls back to local model.
    """
    try:
        prompt = build_entity_extraction_prompt(text, GRAPHIFY_CHUNK_SIZE)
        response = llm.generate(
            prompt,
            system=ENTITY_EXTRACTION_SYSTEM,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS
        )
        entities = parse_json_response(response)
        if not isinstance(entities, list):
            logger.warning(f"Entity extraction returned non-list: {type(entities)}")
            return []
        return entities
    except Exception as e:
        logger.error(f"Entity extraction failed: {e}")
        return []


def extract_relations_with_llm(text: str, llm) -> List[Dict[str, str]]:
    """
    Extract relations using LLM dual-path client.
    """
    try:
        prompt = build_relation_extraction_prompt(text, GRAPHIFY_CHUNK_SIZE)
        response = llm.generate(
            prompt,
            system=RELATION_EXTRACTION_SYSTEM,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS
        )
        relations = parse_json_response(response)
        if not isinstance(relations, list):
            logger.warning(f"Relation extraction returned non-list: {type(relations)}")
            return []
        return relations
    except Exception as e:
        logger.error(f"Relation extraction failed: {e}")
        return []


def store_chunk_vector(chunk_id: str, doc_id: str, text: str, metadata: dict):
    """Store chunk vector in Qdrant"""
    # Import here to avoid circular deps
    from sentence_transformers import SentenceTransformer

    model_name = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    model = SentenceTransformer(model_name)
    vector = model.encode(text[:1000]).tolist()

    # Ensure collection exists
    collections = [c.name for c in qdrant_client_instance.get_collections().collections]
    if QDRANT_COLLECTION not in collections:
        dim = len(vector)
        qdrant_client_instance.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
        )

    point = PointStruct(
        id=chunk_id,
        vector=vector,
        payload={
            "doc_id": doc_id,
            "text": text,
            "metadata": metadata
        }
    )
    qdrant_client_instance.upsert(collection_name=QDRANT_COLLECTION, points=[point])


@app.get("/graph/{doc_id}")
async def get_graph(doc_id: str):
    """Retrieve knowledge graph for a document"""
    with neo4j_driver.session() as session:
        result = session.run(
            """
            MATCH path = (d:Document {doc_id: $doc_id})-[:CONTAINS*1..3]->(n)
            RETURN d, path
            """,
            doc_id=doc_id
        )
        nodes = []
        for record in result:
            nodes.append({
                "document": dict(record["d"]),
                "path": str(record["path"])
            })
        return {"doc_id": doc_id, "nodes": nodes, "count": len(nodes)}


@app.get("/entity/{name}")
async def get_entity(name: str):
    """Get entity details with connected entities"""
    with neo4j_driver.session() as session:
        result = session.run(
            """
            MATCH (e:Entity {name: $name})-[:RELATES_TO]->(e2:Entity)
            RETURN e, collect({name: e2.name, type: e2.type}) as neighbors
            """,
            name=name
        )
        records = list(result)
        if not records:
            raise HTTPException(status_code=404, detail="Entity not found")
        record = records[0]
        return {
            "entity": dict(record["e"]),
            "neighbors": record["neighbors"]
        }


@app.get("/stats")
async def get_stats():
    """Get graph statistics"""
    with neo4j_driver.session() as session:
        try:
            node_count = session.run("MATCH (n) RETURN count(n) as count").single()["count"]
            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) as count").single()["count"]
            entity_count = session.run("MATCH (e:Entity) RETURN count(e) as count").single()["count"]
            chunk_count = session.run("MATCH (c:Chunk) RETURN count(c) as count").single()["count"]
        except Exception as e:
            logger.error(f"Stats query failed: {e}")
            return {"error": str(e)}
        return {
            "total_nodes": node_count,
            "total_relationships": rel_count,
            "entity_count": entity_count,
            "chunk_count": chunk_count
        }


@app.post("/entity/{name}/merge")
async def merge_entity(name: str, merge_into: str):
    """Merge duplicate entity into another entity"""
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (e1:Entity {name: $name})
            MATCH (e2:Entity {name: $merge_into})
            MATCH (e1)-[r]->(e2)
            DELETE r
            WITH e1
            MATCH (c:Chunk)-[:HAS_ENTITY]->(e1)
            MERGE (c)-[:HAS_ENTITY]->(e2)
            DELETE e1
            """,
            name=name,
            merge_into=merge_into
        )
        return {"status": "merged", "from": name, "into": merge_into}


@app.delete("/graph/{doc_id}")
async def delete_graph(doc_id: str):
    """Delete all graph data for a document"""
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (d:Document {doc_id: $doc_id}) DETACH DELETE d",
            doc_id=doc_id
        )
    redis_client.delete(f"graph:{doc_id}")
    return {"status": "deleted", "doc_id": doc_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004, workers=GRAPHIFY_WORKER_COUNT)