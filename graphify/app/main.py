# SPDX-License-Identifier: MIT
# Graphify Service - Knowledge Graph Construction
# Extracts entities and relationships from documents and builds Neo4j graph

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Graphify", version="1.0.0")

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
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.minimax.io/anthropic")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "MiniMax-M3")

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

class GraphBuildResponse(BaseModel):
    doc_id: str
    status: str
    nodes_created: int
    relationships_created: int

class EntityResponse(BaseModel):
    entity_type: str
    entity_name: str
    properties: Dict[str, Any]

class RelationResponse(BaseModel):
    from_entity: str
    to_entity: str
    relation_type: str
    properties: Dict[str, Any]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "graphify", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return {
        "service": "Graphify",
        "version": "1.0.0",
        "description": "Knowledge graph construction service - extracts entities and relationships"
    }

@app.post("/build", response_model=GraphBuildResponse)
async def build_graph(request: GraphBuildRequest):
    """Build knowledge graph from document chunks"""
    logger.info(f"Building graph for document {request.doc_id}")

    nodes_created = 0
    relationships_created = 0

    with neo4j_driver.session() as session:
        # Create document node
        session.run(
            "MERGE (d:Document {doc_id: $doc_id})",
            doc_id=request.doc_id
        )
        nodes_created += 1

        for chunk in request.chunks:
            chunk_id = chunk.get("chunk_id", str(uuid.uuid4()))
            chunk_text = chunk.get("text", "")

            # Create chunk node
            session.run(
                """
                MERGE (c:Chunk {chunk_id: $chunk_id})
                SET c.text = $text,
                    c.doc_id = $doc_id
                WITH c
                MATCH (d:Document {doc_id: $doc_id})
                MERGE (d)-[:CONTAINS]->(c)
                """,
                chunk_id=chunk_id,
                text=chunk_text[:GRAPHIFY_CHUNK_SIZE],
                doc_id=request.doc_id
            )
            nodes_created += 1

            # Extract entities (simplified - would use LLM in production)
            if request.extract_entities:
                entities = extract_entities(chunk_text)
                for entity in entities[:GRAPHIFY_MAX_ENTITIES]:
                    session.run(
                        """
                        MERGE (e:Entity {name: $name})
                        SET e.type = $type
                        WITH e, $chunk_id as cid
                        MATCH (c:Chunk {chunk_id: cid})
                        MERGE (c)-[:HAS_ENTITY]->(e)
                        """,
                        name=entity["name"],
                        type=entity["type"],
                        chunk_id=chunk_id
                    )
                    nodes_created += 1

            # Extract relationships (simplified - would use LLM in production)
            if request.extract_relations:
                relations = extract_relations(chunk_text)
                for rel in relations[:GRAPHIFY_MAX_RELATIONS]:
                    session.run(
                        """
                        MATCH (e1:Entity {name: $from}), (e2:Entity {name: $to})
                        MERGE (e1)-[r:RELATES_TO {type: $type}]->(e2)
                        """,
                        from_=rel["from"],
                        to=rel["to"],
                        type=rel["type"]
                    )
                    relationships_created += 1

    # Store in Redis
    redis_client.hset(f"graph:{request.doc_id}", mapping={
        "status": "built",
        "nodes": nodes_created,
        "relationships": relationships_created,
        "completed_at": datetime.utcnow().isoformat()
    })

    return GraphBuildResponse(
        doc_id=request.doc_id,
        status="built",
        nodes_created=nodes_created,
        relationships_created=relationships_created
    )

def extract_entities(text: str) -> List[Dict[str, str]]:
    """Extract entities from text - simplified placeholder"""
    # In production, this would call LLM API
    return []

def extract_relations(text: str) -> List[Dict[str, str]]:
    """Extract relationships from text - simplified placeholder"""
    # In production, this would call LLM API
    return []

@app.get("/graph/{doc_id}")
async def get_graph(doc_id: str):
    """Retrieve knowledge graph for a document"""
    with neo4j_driver.session() as session:
        result = session.run(
            """
            MATCH (d:Document {doc_id: $doc_id})-[r]->(n)
            RETURN d, r, n
            """,
            doc_id=doc_id
        )
        nodes = []
        for record in result:
            nodes.append({
                "from": str(record["d"]),
                "relationship": str(record["r"]),
                "to": str(record["n"])
            })
        return {"doc_id": doc_id, "graph": nodes}

@app.get("/entity/{name}")
async def get_entity(name: str):
    """Get entity details"""
    with neo4j_driver.session() as session:
        result = session.run(
            "MATCH (e:Entity {name: $name}) RETURN e",
            name=name
        )
        entities = [dict(record["e"]) for record in result]
        if not entities:
            raise HTTPException(status_code=404, detail="Entity not found")
        return entities[0]

@app.get("/stats")
async def get_stats():
    """Get graph statistics"""
    with neo4j_driver.session() as session:
        node_count = session.run("MATCH (n) RETURN count(n) as count").single()["count"]
        rel_count = session.run("MATCH ()-[r]->() RETURN count(r) as count").single()["count"]
        return {
            "total_nodes": node_count,
            "total_relationships": rel_count
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004, workers=GRAPHIFY_WORKER_COUNT)