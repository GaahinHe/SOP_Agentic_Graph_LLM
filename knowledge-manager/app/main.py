# SPDX-License-Identifier: MIT
# Knowledge Manager Service
# Graph inspection, conflict detection, incremental update, SOP step ordering
# Supports hybrid self-maintenance: LLM-assisted detection + manual review

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import logging
from datetime import datetime
import redis
import neo4j
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from utils.llm_client import get_llm_client, parse_json_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Knowledge Manager", version="1.0.0")

# Configuration
NEO4J_HOST = os.getenv("NEO4J_HOST", "neo4j")
NEO4J_PORT = int(os.getenv("NEO4J_PORT", "7687"))
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "changeme")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", "4"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

redis_password = REDIS_PASSWORD if REDIS_PASSWORD else None
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    password=redis_password,
    decode_responses=True
)

neo4j_driver = neo4j.GraphDatabase.driver(
    f"bolt://{NEO4J_HOST}:{NEO4J_PORT}",
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

# Models
class ConflictDetectionResult(BaseModel):
    conflicts: List[Dict[str, Any]]
    total_conflicts: int

class GraphInspectionResult(BaseModel):
    orphan_chunks: List[str]  # Chunks without entities
    duplicate_entities: List[Dict[str, Any]]
    circular_relations: List[Dict[str, Any]]
    stats: Dict[str, int]

class MergeEntitiesRequest(BaseModel):
    source_entity: str
    target_entity: str

class UpdateEntityRequest(BaseModel):
    entity_name: str
    new_name: Optional[str] = None
    new_type: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None

class SOPOrderingRequest(BaseModel):
    doc_id: str
    force_reorder: bool = False


@app.get("/health")
async def health():
    return {"status": "ok", "service": "knowledge-manager", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    return {
        "service": "Knowledge Manager",
        "version": "1.0.0",
        "description": "Graph inspection, conflict detection, incremental update, SOP ordering"
    }


@app.post("/inspect", response_model=GraphInspectionResult)
async def inspect_graph(doc_id: Optional[str] = None):
    """
    Inspect graph for structural issues:
    - Orphan chunks (no entity connections)
    - Duplicate entities (same name, different type)
    - Circular relations
    """
    with neo4j_driver.session() as session:
        # Orphan chunks (chunks without HAS_ENTITY relationship)
        orphan_result = session.run(
            """
            MATCH (c:Chunk)
            WHERE NOT EXISTS((c)-[:HAS_ENTITY]->(:Entity))
            RETURN c.chunk_id as chunk_id, c.text as text
            LIMIT 100
            """,
            doc_id=doc_id
        )
        orphan_chunks = [
            {"chunk_id": r["chunk_id"], "text": r["text"][:200] if r["text"] else ""}
            for r in orphan_result
        ]

        # Duplicate entities (same name, different type)
        dup_result = session.run(
            """
            MATCH (e:Entity)
            WITH e.name as name, collect(DISTINCT e.type) as types, count(*) as cnt
            WHERE cnt > 1 AND size(types) > 1
            RETURN name, types, cnt
            ORDER BY cnt DESC
            LIMIT 50
            """
        )
        duplicate_entities = [
            {"name": r["name"], "types": r["types"], "count": r["cnt"]}
            for r in dup_result
        ]

        # Circular relations (A->B->...->A with 3+ hops)
        circular_result = session.run(
            """
            MATCH path = (e1:Entity)-[:RELATES_TO*3..5]->(e1)
            RETURN e1.name as start_entity,
                   [n IN nodes(path)[1..-1] | n.name] as cycle,
                   length(path) as path_length
            LIMIT 20
            """
        )
        circular_relations = [
            {"entity": r["start_entity"], "cycle": r["cycle"], "length": r["path_length"]}
            for r in circular_result
        ]

        # Stats
        try:
            node_count = session.run("MATCH (n) RETURN count(n) as count").single()["count"]
            entity_count = session.run("MATCH (e:Entity) RETURN count(e) as count").single()["count"]
            chunk_count = session.run("MATCH (c:Chunk) RETURN count(c) as count").single()["count"]
            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) as count").single()["count"]
        except Exception as e:
            logger.error(f"Stats query failed: {e}")
            node_count = entity_count = chunk_count = rel_count = -1

        return GraphInspectionResult(
            orphan_chunks=orphan_chunks,
            duplicate_entities=duplicate_entities,
            circular_relations=circular_relations,
            stats={
                "total_nodes": node_count,
                "total_entities": entity_count,
                "total_chunks": chunk_count,
                "total_relationships": rel_count
            }
        )


@app.post("/detect-conflicts", response_model=ConflictDetectionResult)
async def detect_conflicts():
    """
    Use LLM to detect semantic conflicts in the knowledge graph.
    Scans entity definitions and relations for contradictions.
    """
    llm = get_llm_client()

    with neo4j_driver.session() as session:
        # Get all entities with their types and connections
        result = session.run(
            """
            MATCH (e:Entity)
            OPTIONAL MATCH (e)-[:RELATES_TO]->(e2:Entity)
            RETURN e.name as name, e.type as type,
                   collect(DISTINCT e2.name) as neighbors,
                   collect(DISTINCT e.type) as neighbor_types
            LIMIT 200
            """
        )

        entities = [
            {
                "name": r["name"],
                "type": r["type"],
                "neighbors": [n for n in r["neighbors"] if n],
                "neighbor_types": [t for t in r["neighbor_types"] if t]
            }
            for r in result
        ]

    if not entities:
        return ConflictDetectionResult(conflicts=[], total_conflicts=0)

    # Build prompt for LLM conflict detection
    entity_list = "\n".join([
        f"- {e['name']} ({e['type']}) -> [{', '.join(e['neighbors']) or 'none'}]"
        for e in entities[:100]
    ])

    prompt = f"""You are an expert at detecting semantic conflicts in a knowledge graph.

Scan the following entities and their relationships. Identify conflicts such as:
- Entity defined as both A and B (contradictory types)
- Circular dependencies that don't make sense
- Relations that contradict entity types (e.g., a "PERSON" entity that "PRODUCES" a "PROCESS")
- Same entity with different definitions in different contexts

Entities:
{entity_list}

Return a JSON list of conflicts found (max 20):
[
  {{"entity1": "...", "entity2": "...", "conflict_type": "...", "description": "..."}},
  ...
]

If no conflicts found, return []. No markdown, no explanation."""

    try:
        response = llm.generate(prompt, max_tokens=LLM_MAX_TOKENS)
        conflicts = parse_json_response(response)

        if not isinstance(conflicts, list):
            conflicts = []

        return ConflictDetectionResult(
            conflicts=conflicts[:20],  # Limit to 20
            total_conflicts=len(conflicts)
        )
    except Exception as e:
        logger.error(f"Conflict detection failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM conflict detection failed: {e}")


@app.post("/merge-entities")
async def merge_entities(request: MergeEntitiesRequest):
    """
    Merge source entity into target entity.
    All relationships are redirected; source entity is deleted.
    """
    with neo4j_driver.session() as session:
        # Verify both exist
        check = session.run(
            "MATCH (e1:Entity {name: $source}), (e2:Entity {name: $target}) RETURN e1, e2",
            source=request.source_entity,
            target=request.target_entity
        )
        records = list(check)
        if len(records) < 1:
            raise HTTPException(status_code=404, detail="One or both entities not found")

        # Redirect all incoming relations to target
        session.run(
            """
            MATCH (e1:Entity {name: $source})-[r]->(e2:Entity)
            WHERE e1 <> e2
            MERGE (e2)-[r2:RELATES_TO {type: r.type}]->(e2)
            ON CREATE SET r2 = properties(r)
            DELETE r
            """,
            source=request.source_entity,
            target=request.target_entity
        )

        # Redirect outgoing relations from source to target
        session.run(
            """
            MATCH (e1:Entity {name: $source})-[r]->(e2:Entity)
            WHERE e1 <> e2
            MERGE (e1)-[r2:RELATES_TO {type: r.type}]->(e2)
            ON CREATE SET r2 = properties(r)
            DELETE r
            """,
            source=request.source_entity,
            target=request.target_entity
        )

        # Update chunk relationships
        session.run(
            """
            MATCH (c:Chunk)-[:HAS_ENTITY]->(e1:Entity {name: $source})
            MERGE (c)-[:HAS_ENTITY]->(e2:Entity {name: $target})
            DELETE e1
            """,
            source=request.source_entity,
            target=request.target_entity
        )

    # Log in Redis for audit trail
    audit_key = f"audit:merge:{datetime.utcnow().isoformat()}"
    redis_client.hset(audit_key, mapping={
        "source": request.source_entity,
        "target": request.target_entity,
        "timestamp": datetime.utcnow().isoformat()
    })

    return {
        "status": "merged",
        "from": request.source_entity,
        "into": request.target_entity
    }


@app.put("/entity")
async def update_entity(request: UpdateEntityRequest):
    """Update entity properties (name, type, etc.)"""
    with neo4j_driver.session() as session:
        updates = []
        params = {"name": request.entity_name}

        if request.new_name:
            updates.append("e.name = $new_name")
            params["new_name"] = request.new_name

        if request.new_type:
            updates.append("e.type = $new_type")
            params["new_type"] = request.new_type

        if request.properties:
            for k, v in request.properties.items():
                updates.append(f"e.{k} = ${k}")
                params[k] = v

        if not updates:
            return {"status": "unchanged", "message": "No updates provided"}

        updates.append("e.updated_at = $now")
        params["now"] = datetime.utcnow().isoformat()

        result = session.run(
            f"MATCH (e:Entity {{name: $name}}) SET {', '.join(updates)} RETURN e",
            **params
        )

        if not result.consume().counters.nodes_set:
            raise HTTPException(status_code=404, detail="Entity not found")

    return {"status": "updated", "entity": request.new_name or request.entity_name}


@app.delete("/entity/{name}")
async def delete_entity(name: str):
    """Delete an entity and its relationships"""
    with neo4j_driver.session() as session:
        result = session.run(
            "MATCH (e:Entity {name: $name}) DETACH DELETE e",
            name=name
        )
        if result.consume().counters.nodes_deleted == 0:
            raise HTTPException(status_code=404, detail="Entity not found")

    return {"status": "deleted", "entity": name}


@app.post("/sop-order/{doc_id}")
async def order_sop_steps(doc_id: str, force_reorder: bool = False):
    """
    Analyze SOP document and order steps correctly.
    Uses LLM to understand step dependencies and sequence.
    """
    llm = get_llm_client()

    with neo4j_driver.session() as session:
        # Get all chunks for this document
        result = session.run(
            """
            MATCH (d:Document {doc_id: $doc_id})-[:CONTAINS]->(c:Chunk)
            OPTIONAL MATCH (c)-[:HAS_ENTITY]->(e:Entity)
            RETURN c.chunk_id as chunk_id, c.text as text, collect(e.name) as entities
            ORDER BY c.chunk_id
            """,
            doc_id=doc_id
        )

        chunks = [
            {
                "id": r["chunk_id"],
                "text": r["text"],
                "entities": [e for e in r["entities"] if e]
            }
            for r in result
        ]

    if not chunks:
        return {"status": "no_chunks", "doc_id": doc_id}

    # Build context for LLM
    step_list = "\n".join([
        f"Step {i+1} (id={c['id'][:8]}...): {c['text'][:150]}... | Entities: {', '.join(c['entities'][:5]) or 'none'}"
        for i, c in enumerate(chunks)
    ])

    prompt = f"""You are analyzing an SOP (Standard Operating Procedure) document.
The steps below are extracted from the document but may be out of order.

Your task:
1. Identify the correct sequence of steps (there may be parallel steps)
2. Identify dependencies (Step X must come before Step Y)
3. Group steps that can run in parallel

Return a JSON object with the ordered steps:
{{
  "sequence": [0, 1, 2, 3, ...],  // chunk indices in correct order
  "parallel_groups": [[0, 1], [2], [3, 4, 5], ...],  // steps that can run together
  "dependencies": [{{"before": "step_id", "after": "step_id", "reason": "..."}}, ...]
}}

Steps:
{step_list}

Only return the JSON object, no explanation."""

    try:
        response = llm.generate(prompt, max_tokens=LLM_MAX_TOKENS)
        ordering = parse_json_response(response)

        if not ordering or not isinstance(ordering, dict):
            return {"status": "parse_failed", "raw": response[:500]}

        # Store ordering in Redis
        ordering_key = f"sop:ordering:{doc_id}"
        redis_client.set(ordering_key, json.dumps({
            "sequence": ordering.get("sequence", []),
            "parallel_groups": ordering.get("parallel_groups", []),
            "dependencies": ordering.get("dependencies", []),
            "generated_at": datetime.utcnow().isoformat()
        }), ex=86400 * 30)  # 30 day retention

        return {
            "status": "ordered",
            "doc_id": doc_id,
            "sequence": ordering.get("sequence", []),
            "parallel_groups": ordering.get("parallel_groups", []),
            "dependencies": ordering.get("dependencies", [])
        }

    except Exception as e:
        logger.error(f"SOP ordering failed: {e}")
        raise HTTPException(status_code=500, detail=f"SOP ordering failed: {e}")


@app.get("/sop-order/{doc_id}")
async def get_sop_order(doc_id: str):
    """Get stored SOP ordering for a document"""
    ordering_key = f"sop:ordering:{doc_id}"
    data = redis_client.get(ordering_key)
    if not data:
        raise HTTPException(status_code=404, detail="No ordering found for this document")

    import json
    return json.loads(data)


@app.get("/audit-log")
async def get_audit_log(limit: int = 50):
    """Get audit log of knowledge management actions"""
    keys = redis_client.keys("audit:*")
    logs = []
    for key in sorted(keys, reverse=True)[:limit]:
        data = redis_client.hgetall(key)
        if data:
            logs.append({
                "key": key.decode() if isinstance(key, bytes) else key,
                **data
            })
    return {"logs": logs, "count": len(logs)}


@app.post("/batch-update")
async def batch_update(operations: List[Dict[str, Any]]):
    """
    Execute a batch of knowledge management operations.
    Operations: merge_entities, update_entity, delete_entity
    """
    results = []
    for op in operations:
        op_type = op.get("type")
        try:
            if op_type == "merge":
                result = await merge_entities(MergeEntitiesRequest(
                    source_entity=op.get("source"),
                    target_entity=op.get("target")
                ))
            elif op_type == "update":
                result = await update_entity(UpdateEntityRequest(
                    entity_name=op.get("entity"),
                    new_name=op.get("new_name"),
                    new_type=op.get("new_type"),
                    properties=op.get("properties")
                ))
            elif op_type == "delete":
                result = await delete_entity(op.get("entity"))
            else:
                result = {"status": "skipped", "reason": f"Unknown type: {op_type}"}
            results.append({"op": op, "result": result, "success": True})
        except Exception as e:
            results.append({"op": op, "result": str(e), "success": False})

    return {
        "total": len(operations),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "results": results
    }


if __name__ == "__main__":
    import uvicorn
    import json
    uvicorn.run(app, host="0.0.0.0", port=8007, workers=2)