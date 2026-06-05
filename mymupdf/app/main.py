# SPDX-License-Identifier: MIT
# MyMuPDF Service - Document Preprocessing Service
# Handles PDF/PPTX parsing, OCR, layout detection, and text extraction
# Includes Qwen-VL vision processing for complex charts and diagrams

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import pymupdf
import pytesseract
from PIL import Image
import io
import uuid
import redis
import minio
import os
import logging
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MyMuPDF", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "changeme")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "documents")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "eng,chi_sim,chi_tra").split(",")
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "4"))
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_GRPC_PORT = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "document_chunks")
ENABLE_VISION = os.getenv("ENABLE_VISION", "true").lower() == "true"
VISION_CHUNK_SIZE = int(os.getenv("VISION_CHUNK_SIZE", "512"))
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_DIM = int(os.getenv("EMBED_DIM", "384"))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

minio_client = minio.Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)

executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)

# Models
class DocumentStatus(BaseModel):
    doc_id: str
    status: str
    pages: int
    created_at: str

class ProcessRequest(BaseModel):
    file_url: Optional[str] = None
    use_ocr: bool = True
    extract_images: bool = True
    enable_vision: bool = True

class ProcessResponse(BaseModel):
    doc_id: str
    status: str
    pages: int
    elements: List[dict]
    vision_results: List[dict] = []

class VisionResult(BaseModel):
    image_id: str
    chart_type: str
    description: str
    key_data_points: List[str]
    entities: List[dict]
    relations: List[dict]

# =============================================================================
# Vision Processing (Qwen-VL local)
# =============================================================================

def get_vision_client():
    """Lazy import Qwen-VL when needed"""
    try:
        from qwen_vl import QwenVL
        base_url = os.getenv("LOCAL_VLM_API_BASE", "http://localhost:8000/v1")
        model_name = os.getenv("LOCAL_VLM_MODEL", "Qwen2-VL-7B-Instruct")
        return QwenVL(base_url, model_name)
    except ImportError:
        logger.warning("Qwen-VL not available, using OCR fallback")
        return None


def process_image_with_vision(image_bytes: bytes, image_id: str) -> VisionResult:
    """
    Send image to Qwen-VL for chart/diagram understanding.
    Falls back to OCR-based description if VLM unavailable.
    """
    try:
        vl_client = get_vision_client()
        if vl_client:
            prompt = """Analyze this image in detail. Identify:
1. Chart type (flowchart, bar chart, line chart, org chart, diagram, screenshot, etc.)
2. Key information and data points
3. Any named entities mentioned (machines, processes, people, locations)

Return structured JSON:
{
  "chart_type": "...",
  "description": "detailed text description of what this image shows",
  "key_data_points": ["point 1", "point 2"],
  "entities": [{"name": "...", "type": "..."}],
  "relations": [{"from": "...", "to": "...", "type": "..."}]
}"""
            result = vl_client.analyze(image_bytes, prompt=prompt)
            return VisionResult(
                image_id=image_id,
                chart_type=result.get("chart_type", "unknown"),
                description=result.get("description", ""),
                key_data_points=result.get("key_data_points", []),
                entities=result.get("entities", []),
                relations=result.get("relations", [])
            )
    except Exception as e:
        logger.warning(f"Vision processing failed for {image_id}: {e}")

    # Fallback: OCR + heuristics
    return ocr_image_fallback(image_bytes, image_id)


def ocr_image_fallback(image_bytes: bytes, image_id: str) -> VisionResult:
    """Fallback when VLM unavailable: OCR + basic chart classification"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="+".join(OCR_LANGUAGES))

        # Simple heuristics to guess chart type
        text_lower = text.lower()
        chart_type = "unknown"
        if any(kw in text_lower for kw in ["流程", "flow", "步骤", "step"]):
            chart_type = "flowchart"
        elif any(kw in text_lower for kw in ["销售", "revenue", "销量", "sales"]):
            chart_type = "bar_chart"
        elif any(kw in text_lower for kw in ["趋势", "trend", "增长", "increase"]):
            chart_type = "line_chart"
        elif any(kw in text_lower for kw in ["组织", "org", "团队", "team"]):
            chart_type = "org_chart"

        return VisionResult(
            image_id=image_id,
            chart_type=chart_type,
            description=text.strip()[:500],
            key_data_points=[text.strip()[:200]],
            entities=[],
            relations=[]
        )
    except Exception as e:
        logger.error(f"OCR fallback also failed: {e}")
        return VisionResult(
            image_id=image_id,
            chart_type="unknown",
            description="[Image processing failed]",
            key_data_points=[],
            entities=[],
            relations=[]
        )

# =============================================================================
# Embedding (sentence-transformers, local)
# =============================================================================

_embedding_model = None


def get_embedding_model():
    """Lazy load sentence-transformers model"""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            model_name = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
            _embedding_model = SentenceTransformer(model_name)
            logger.info(f"Embedding model loaded: {model_name}")
        except Exception as e:
            logger.warning(f"Could not load embedding model: {e}")
            _embedding_model = None
    return _embedding_model


def encode_text(text: str) -> Optional[List[float]]:
    """Get embedding vector for text"""
    model = get_embedding_model()
    if model is None:
        return None
    try:
        embedding = model.encode(text[:1000])  # Truncate long texts
        return embedding.tolist()
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None


def upsert_to_qdrant(doc_id: str, chunk_id: str, text: str, vector: List[float], metadata: dict):
    """Upsert a chunk to Qdrant vector DB"""
    try:
        import qdrant_client
        from qdrant_client.models import Distance, VectorParams, PointStruct

        client = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_GRPC_PORT)

        # Ensure collection exists
        collections = [c.name for c in client.get_collections().collections]
        if QDRANT_COLLECTION not in collections:
            client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=len(vector), distance=Distance.COSINE)
            )
            logger.info(f"Created Qdrant collection: {QDRANT_COLLECTION}")

        # Upsert point
        point = PointStruct(
            id=chunk_id,
            vector=vector,
            payload={
                "doc_id": doc_id,
                "text": text[:2000],
                "metadata": metadata
            }
        )
        client.upsert(collection_name=QDRANT_COLLECTION, points=[point])
        logger.info(f"Upserted chunk {chunk_id} to Qdrant")
    except Exception as e:
        logger.error(f"Qdrant upsert failed: {e}")

# =============================================================================
# PPTX Processing
# =============================================================================

PPTX_NAMESPACES = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
}


def process_pptx(contents: bytes) -> List[Dict[str, Any]]:
    """
    Process PPTX file (ZIP of XMLs).
    Extracts text, tables, shapes, notes, and embedded images.
    """
    elements = []

    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as zf:
            # Get slide list
            slide_files = sorted([f for f in zf.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")])

            for slide_path in slide_files:
                slide_num = int(slide_path.split("slide")[-1].split(".")[0])
                slide_elements = parse_pptx_slide(zf, slide_path, slide_num)
                elements.extend(slide_elements)

            # Extract images
            image_files = [f for f in zf.namelist() if f.startswith("ppt/media/")]
            for img_path in image_files:
                try:
                    img_data = zf.read(img_path)
                    img_id = f"pptx_img_{uuid.uuid4().hex[:8]}"
                    # Store image reference
                    elements.append({
                        "type": "image",
                        "content": f"data:image;base64,{img_data.hex()[:100]}...",
                        "image_id": img_id,
                        "source_file": img_path,
                        "page": 0
                    })
                except Exception as e:
                    logger.warning(f"Failed to extract image {img_path}: {e}")

    except Exception as e:
        logger.error(f"PPTX processing failed: {e}")
        raise HTTPException(status_code=500, detail=f"PPTX processing failed: {e}")

    return elements


def parse_pptx_slide(zf: zipfile.ZipFile, slide_path: str, slide_num: int) -> List[Dict[str, Any]]:
    """Parse a single PPTX slide XML"""
    elements = []

    try:
        xml_content = zf.read(slide_path)
        root = ET.fromstring(xml_content)

        # Extract text from all text elements
        for t_elem in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
            if t_elem.text and t_elem.text.strip():
                text = t_elem.text.strip()
                if len(text) > 2:  # Filter garbage
                    elements.append({
                        "type": "text",
                        "content": text,
                        "page": slide_num,
                        "source": "pptx"
                    })

        # Extract shapes (autoshapes)
        for sp in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}sp"):
            shape_text = extract_shape_text(sp)
            if shape_text:
                elements.append({
                    "type": "shape",
                    "content": shape_text,
                    "page": slide_num,
                    "source": "pptx"
                })

        # Extract tables
        for tbl in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}tbl"):
            table_text = extract_table_text(tbl)
            if table_text:
                elements.append({
                    "type": "table",
                    "content": table_text,
                    "page": slide_num,
                    "source": "pptx"
                })

    except Exception as e:
        logger.warning(f"Failed to parse slide {slide_num}: {e}")

    return elements


def extract_shape_text(sp_elem) -> str:
    """Extract text from a shape element"""
    texts = []
    for t in sp_elem.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
        if t.text:
            texts.append(t.text)
    return " ".join(texts)


def extract_table_text(tbl_elem) -> str:
    """Extract text from table element"""
    rows = []
    for row in tbl_elem.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}tr"):
        cells = []
        for cell in row.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}tc"):
            cell_text = []
            for t in cell.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
                if t.text:
                    cell_text.append(t.text)
            cells.append(" ".join(cell_text))
        rows.append(" | ".join(cells))
    return "\n".join(rows)


# =============================================================================
# PDF Processing (existing logic, enhanced)
# =============================================================================

def process_pdf(contents: bytes, use_ocr: bool, extract_images: bool, enable_vision: bool) -> tuple:
    """Process PDF, extract text blocks and images"""
    doc = pymupdf.open(stream=contents, filetype="pdf")
    pages_info = []
    elements = []
    vision_results = []

    for page_num, page in enumerate(doc):
        page_text = ""
        blocks = page.get_text("blocks")

        for block in blocks:
            x0, y0, x1, y1, text, block_no, block_type = block

            if block_type == 0:  # Text block
                element = {
                    "type": "text",
                    "content": text.strip(),
                    "bbox": [x0, y0, x1, y1],
                    "page": page_num
                }
                elements.append(element)
                page_text += text + "\n"

            elif block_type == 1:  # Image block
                if extract_images:
                    try:
                        img_id = f"pdf_img_{uuid.uuid4().hex[:8]}"
                        # Extract image
                        img_list = page.get_images(full=True)
                        for img_index, img in enumerate(img_list):
                            xref = img[0]
                            base_image = doc.extract_image(xref)
                            image_bytes = base_image["image"]

                            element = {
                                "type": "image",
                                "image_id": img_id,
                                "bbox": [x0, y0, x1, y1],
                                "page": page_num,
                                "width": base_image.get("width"),
                                "height": base_image.get("height"),
                                "ext": base_image.get("ext", "png")
                            }
                            elements.append(element)

                            # Process with vision if enabled
                            if enable_vision and ENABLE_VISION:
                                future = executor.submit(process_image_with_vision, image_bytes, img_id)
                                # Will be collected after loop

                    except Exception as e:
                        logger.warning(f"Failed to extract image on page {page_num}: {e}")

        # OCR fallback for low-text pages
        if use_ocr and (not page_text.strip() or len(page_text.strip()) < 50):
            try:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                ocr_text = pytesseract.image_to_string(img, lang="+".join(OCR_LANGUAGES))

                # Also process with vision
                if enable_vision and ENABLE_VISION:
                    img_id = f"pdf_ocr_{uuid.uuid4().hex[:8]}"
                    future = executor.submit(process_image_with_vision, img_data, img_id)
                    vision_results.append(future)

                elements.append({
                    "type": "ocr_text",
                    "content": ocr_text.strip(),
                    "page": page_num,
                    "source": "tesseract"
                })
            except Exception as e:
                logger.warning(f"OCR failed on page {page_num}: {e}")

        pages_info.append({"page_num": page_num, "text_length": len(page_text)})

    # Collect vision results
    for f in vision_results:
        try:
            vr = f.result(timeout=30)
            if vr:
                vision_results_dict = vr if isinstance(vr, dict) else vr.__dict__
                # Add as text element for graphify
                if vision_results_dict.get("description"):
                    elements.append({
                        "type": "vision_description",
                        "image_id": vision_results_dict.get("image_id", ""),
                        "chart_type": vision_results_dict.get("chart_type", ""),
                        "content": vision_results_dict.get("description", ""),
                        "key_data_points": vision_results_dict.get("key_data_points", []),
                        "entities": vision_results_dict.get("entities", []),
                        "relations": vision_results_dict.get("relations", [])
                    })
        except Exception as e:
            logger.warning(f"Vision result collection failed: {e}")

    return pages_info, elements


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mymupdf", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    return {
        "service": "MyMuPDF",
        "version": "2.0.0",
        "description": "Document preprocessing - PDF/PPTX, OCR, layout, vision (Qwen-VL), embedding"
    }


def detect_file_type(filename: str, content_type: str, contents: bytes) -> str:
    """Detect if file is PDF or PPTX"""
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return "pdf"
    if content_type in ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "application/powerpoint",
                        "application/vnd.ms-powerpoint") or \
       filename.lower().endswith((".pptx", ".ppt")):
        return "pptx"
    # Magic bytes check
    if contents[:4] == b"%PDF":
        return "pdf"
    if contents[:2] == b"PK":  # PPTX is a ZIP
        return "pptx"
    return "pdf"  # default


@app.post("/process", response_model=ProcessResponse)
async def process_document(
    file: UploadFile = File(...),
    use_ocr: bool = True,
    extract_images: bool = True,
    enable_vision: bool = True
):
    """Process a PDF or PPTX document"""
    doc_id = str(uuid.uuid4())

    try:
        contents = await file.read()
        file_type = detect_file_type(file.filename or "", file.content_type or "", contents)

        if file_type == "pptx":
            logger.info(f"Processing PPTX: {file.filename}")
            elements = process_pptx(contents)
            pages_info = [{"page_num": i, "text_length": 0} for i in range(len(set(e.get("page", 0) for e in elements)))]

        else:  # PDF
            logger.info(f"Processing PDF: {file.filename}")
            pages_info, elements = process_pdf(contents, use_ocr, extract_images, enable_vision)

        # Store metadata in Redis
        redis_client.hset(f"doc:{doc_id}", mapping={
            "status": "processed",
            "pages": len(pages_info),
            "elements": len(elements),
            "created_at": datetime.utcnow().isoformat(),
            "file_type": file_type,
            "filename": file.filename or "unknown"
        })

        # Async: generate embeddings and upsert to Qdrant
        for element in elements:
            if element.get("type") in ("text", "ocr_text", "vision_description", "table", "shape"):
                text = element.get("content", "")[:1000]
                if text:
                    chunk_id = str(uuid.uuid4())
                    vector = encode_text(text)
                    if vector:
                        executor.submit(upsert_to_qdrant, doc_id, chunk_id, text, vector, {
                            "type": element.get("type"),
                            "page": element.get("page", 0),
                            "source": file.filename or ""
                        })

        return ProcessResponse(
            doc_id=doc_id,
            status="processed",
            pages=len(pages_info),
            elements=elements,
            vision_results=[]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing document: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{doc_id}", response_model=DocumentStatus)
async def get_status(doc_id: str):
    data = redis_client.hgetall(f"doc:{doc_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentStatus(**data)


@app.post("/query")
async def query_documents(query: str, top_k: int = 10):
    """Query processed documents using vector similarity"""
    vector = encode_text(query)
    if not vector:
        return {"error": "Embedding not available"}

    try:
        import qdrant_client
        from qdrant_client.models import SearchParams

        client = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_GRPC_PORT)
        results = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vector,
            limit=top_k,
            search_params=SearchParams(hnsw_ef=128)
        )

        return {
            "query": query,
            "results": [
                {
                    "doc_id": hit.payload.get("doc_id"),
                    "chunk_id": str(hit.id),
                    "text": hit.payload.get("text", ""),
                    "score": hit.score
                }
                for hit in results
            ]
        }
    except Exception as e:
        logger.error(f"Query failed: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=WORKER_COUNT)