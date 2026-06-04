# SPDX-License-Identifier: MIT
# MyMuPDF Service - Document Preprocessing Service
# Handles OCR, layout detection, and text extraction

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import pymupdf
import pytesseract
from PIL import Image
import io
import uuid
import redis
import minio
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MyMuPDF", version="1.0.0")

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

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

minio_client = minio.Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)

class DocumentStatus(BaseModel):
    doc_id: str
    status: str
    pages: int
    created_at: str

class ProcessRequest(BaseModel):
    file_url: Optional[str] = None
    use_ocr: bool = True
    extract_images: bool = False

class ProcessResponse(BaseModel):
    doc_id: str
    status: str
    pages: int
    elements: List[dict]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mymupdf", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return {
        "service": "MyMuPDF",
        "version": "1.0.0",
        "description": "Document preprocessing service - OCR, layout detection, text extraction"
    }

@app.post("/process", response_model=ProcessResponse)
async def process_document(
    file: UploadFile = File(...),
    use_ocr: bool = True,
    extract_images: bool = False
):
    """Process a PDF or image document"""
    doc_id = str(uuid.uuid4())

    try:
        contents = await file.read()
        doc = pymupdf.open(stream=contents, filetype="pdf" if file.content_type == "application/pdf" else None)

        pages = []
        elements = []

        for page_num, page in enumerate(doc):
            page_text = ""
            blocks = page.get_text("blocks")

            for block in blocks:
                x0, y0, x1, y1, text, block_no, block_type = block

                if block_type == 0:
                    element = {
                        "type": "text",
                        "content": text.strip(),
                        "bbox": [x0, y0, x1, y1],
                        "page": page_num
                    }
                    elements.append(element)
                    page_text += text + "\n"

            if use_ocr and (not page_text.strip() or len(page_text.strip()) < 50):
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                ocr_text = pytesseract.image_to_string(img, lang="+".join(OCR_LANGUAGES))
                elements.append({
                    "type": "ocr_text",
                    "content": ocr_text.strip(),
                    "page": page_num,
                    "source": "tesseract"
                })

            pages.append({"page_num": page_num, "text_length": len(page_text)})

        redis_client.hset(f"doc:{doc_id}", mapping={
            "status": "processed",
            "pages": len(pages),
            "elements": len(elements),
            "created_at": datetime.utcnow().isoformat()
        })

        return ProcessResponse(doc_id=doc_id, status="processed", pages=len(pages), elements=elements)

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{doc_id}", response_model=DocumentStatus)
async def get_status(doc_id: str):
    data = redis_client.hgetall(f"doc:{doc_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentStatus(**data)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=WORKER_COUNT)