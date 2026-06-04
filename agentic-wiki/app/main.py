# SPDX-License-Identifier: MIT
# Agentic Wiki Service - Knowledge Base Management
# Interactive knowledge base with semantic search and auto-rebuild

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import logging
from datetime import datetime
import redis
import neo4j
import qdrant_client
from qdrant_client.models import Filter, FieldCondition, MatchValue
import json
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agentic Wiki", version="1.0.0")

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
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "wiki_knowledge")
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
REDIS_DB = int(os.getenv("REDIS_DB", "4"))
WIKI_LANGUAGES = os.getenv("WIKI_LANGUAGES", "en,zh").split(",")
WIKI_UPDATE_INTERVAL = int(os.getenv("WIKI_UPDATE_INTERVAL", "86400"))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

neo4j_driver = neo4j.GraphDatabase.driver(
    f"bolt://{NEO4J_HOST}:{NEO4J_PORT}",
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

qdrant = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_GRPC_PORT)

WIKI_TITLE = os.getenv("WIKI_TITLE", "Knowledge Base")
WIKI_PORT = int(os.getenv("WIKI_PORT", "8006"))

# Models
class WikiPage(BaseModel):
    page_id: str
    title: str
    content: str
    tags: List[str]
    created_at: str
    updated_at: str

class SearchRequest(BaseModel):
    query: str
    tags: Optional[List[str]] = None
    limit: int = 20

class SearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]
    total: int

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentic-wiki", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return {
        "service": "Agentic Wiki",
        "version": "1.0.0",
        "title": WIKI_TITLE,
        "description": "Interactive knowledge base management system"
    }

@app.get("/wiki")
async def list_pages(limit: int = 100, offset: int = 0):
    """List all wiki pages"""
    try:
        with neo4j_driver.session() as session:
            result = session.run(
                """
                MATCH (p:WikiPage)
                RETURN p.page_id as page_id, p.title as title, p.tags as tags,
                       p.created_at as created_at, p.updated_at as updated_at
                ORDER BY p.updated_at DESC
                SKIP $offset LIMIT $limit
                """,
                offset=offset,
                limit=limit
            )
            pages = [dict(record) for record in result]
            return {"pages": pages, "offset": offset, "limit": limit}
    except Exception as e:
        logger.error(f"Error listing pages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/wiki/{page_id}")
async def get_page(page_id: str):
    """Get a specific wiki page"""
    try:
        with neo4j_driver.session() as session:
            result = session.run(
                "MATCH (p:WikiPage {page_id: $page_id}) RETURN p",
                page_id=page_id
            )
            record = result.single()
            if not record:
                raise HTTPException(status_code=404, detail="Page not found")
            return dict(record["p"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting page: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/wiki", response_model=WikiPage)
async def create_page(title: str, content: str, tags: List[str] = None):
    """Create a new wiki page"""
    page_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    try:
        with neo4j_driver.session() as session:
            session.run(
                """
                CREATE (p:WikiPage {
                    page_id: $page_id,
                    title: $title,
                    content: $content,
                    tags: $tags,
                    created_at: $created_at,
                    updated_at: $updated_at
                })
                """,
                page_id=page_id,
                title=title,
                content=content,
                tags=tags or [],
                created_at=now,
                updated_at=now
            )

        return WikiPage(
            page_id=page_id,
            title=title,
            content=content,
            tags=tags or [],
            created_at=now,
            updated_at=now
        )
    except Exception as e:
        logger.error(f"Error creating page: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/wiki/{page_id}")
async def update_page(page_id: str, title: str = None, content: str = None, tags: List[str] = None):
    """Update an existing wiki page"""
    now = datetime.utcnow().isoformat()
    updates = []
    params = {"page_id": page_id, "updated_at": now}

    if title is not None:
        updates.append("p.title = $title")
        params["title"] = title
    if content is not None:
        updates.append("p.content = $content")
        params["content"] = content
    if tags is not None:
        updates.append("p.tags = $tags")
        params["tags"] = tags

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    updates.append("p.updated_at = $updated_at")

    try:
        with neo4j_driver.session() as session:
            result = session.run(
                f"""
                MATCH (p:WikiPage {{page_id: $page_id}})
                SET {', '.join(updates)}
                RETURN p
                """,
                **params
            )
            if not result.single():
                raise HTTPException(status_code=404, detail="Page not found")

        return {"status": "updated", "page_id": page_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating page: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search", response_model=SearchResponse)
async def search_wiki(request: SearchRequest):
    """Search wiki pages using vector similarity"""
    try:
        # Search in Qdrant
        search_results = qdrant.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=[0.0] * 3072,  # Placeholder
            limit=request.limit,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="tags",
                        match=MatchValue(any=request.tags) if request.tags else None
                    )
                ] if request.tags else None
            )
        )

        results = [
            {
                "page_id": str(hit.id),
                "score": hit.score,
                "content": hit.payload.get("content", ""),
                "title": hit.payload.get("title", "")
            }
            for hit in search_results
        ]

        return SearchResponse(
            query=request.query,
            results=results,
            total=len(results)
        )
    except Exception as e:
        logger.error(f"Error searching wiki: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
async def get_stats():
    """Get wiki statistics"""
    try:
        with neo4j_driver.session() as session:
            page_count = session.run("MATCH (p:WikiPage) RETURN count(p) as count").single()["count"]
            tag_count = session.run("MATCH (p:WikiPage) UNWIND p.tags as tag RETURN count(DISTINCT tag) as count").single()["count"]
        return {
            "total_pages": page_count,
            "total_tags": tag_count,
            "collection": QDRANT_COLLECTION
        }
    except Exception as e:
        return {"error": str(e)}

# Web UI
@app.get("/ui")
async def wiki_ui():
    """Simple wiki web interface"""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{WIKI_TITLE}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
            .search-box {{ margin: 20px 0; }}
            input[type="text"] {{ width: 70%; padding: 10px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ padding: 10px 20px; font-size: 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }}
            .page-list {{ background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .page-item {{ padding: 15px; border-bottom: 1px solid #eee; }}
            .page-item:last-child {{ border-bottom: none; }}
            .page-title {{ font-size: 18px; font-weight: bold; color: #007bff; }}
            .page-meta {{ color: #666; font-size: 12px; margin-top: 5px; }}
            .tags {{ margin-top: 5px; }}
            .tag {{ display: inline-block; background: #e9ecef; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-right: 5px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>{WIKI_TITLE}</h1>
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Search knowledge base...">
                <button onclick="search()">Search</button>
            </div>
            <div id="results"></div>
            <div class="page-list" id="pages"></div>
        </div>
        <script>
            async function loadPages() {{
                const res = await fetch('/wiki');
                const data = await res.json();
                const pagesDiv = document.getElementById('pages');
                pagesDiv.innerHTML = data.pages.map(p => `
                    <div class="page-item">
                        <div class="page-title">${{p.title}}</div>
                        <div class="page-meta">Created: ${{p.created_at}}</div>
                        <div class="tags">${{(p.tags||[]).map(t=>'<span class="tag">'+t+'</span>').join('')}}</div>
                    </div>
                `).join('');
            }}
            async function search() {{
                const query = document.getElementById('searchInput').value;
                if (!query) return;
                const res = await fetch('/search', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{query, limit: 20}})
                }});
                const data = await res.json();
                document.getElementById('results').innerHTML = data.results.map(r => `
                    <div class="page-item">
                        <div class="page-title">${{r.title}} <small>(score: ${{r.score.toFixed(3)}})</small></div>
                        <div>${{r.content?.substring(0,200)}}...</div>
                    </div>
                `).join('') || '<p>No results found</p>';
            }}
            loadPages();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

# Import uuid
import uuid

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WIKI_PORT, workers=2)