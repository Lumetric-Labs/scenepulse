import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Depends,
    Request,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from google.cloud import firestore
from google.cloud import storage

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "scenepulse-prod")
API_KEY = os.getenv("SCENEPULSE_API_KEY", "bobs-your-uncle-001")
UPLOAD_BUCKET = os.getenv(
    "SCENEPULSE_UPLOAD_BUCKET",
    "scenepulse-prod-scenepulse-uploads",
)

# Firestore client
db = firestore.Client(project=PROJECT_ID)

# Storage client
storage_client = storage.Client(project=PROJECT_ID)
upload_bucket = storage_client.bucket(UPLOAD_BUCKET)

app = FastAPI(title="ScenePulse API")

# --------------------------------------------------------------------
# ✅ CORS Configuration
# --------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://portal.lumetriclabs.com",
        "http://localhost:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def require_api_key(x_api_key: str = Header(default="")):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def new_test_id() -> str:
    return "test_" + uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_phone_number(phone: Optional[str]) -> Optional[str]:
    """
    Take a phone string, strip non-digits, and if it is 10 digits
    format it like (555) 987-1234. Otherwise return the original string.
    """
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    return phone


# --------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------

class RunCreateRequest(BaseModel):
    company_name: str
    project_id: str
    contact_name: str
    contact_email: str
    contact_phone: str
    creative_id: str
    variant: str
    optional_label: Optional[str] = None
    internal_notes: Optional[str] = None
    video_filename: str
    doc_filenames: List[str] = []

class SignedURLInfo(BaseModel):
    original_filename: str
    signed_url: str
    storage_path: str
    key: str

class RunCreateResponse(BaseModel):
    run_id: str
    contact_phone: Optional[str]
    upload_urls: List[SignedURLInfo]

class TestCreateRequest(BaseModel):
    project_id: str
    creative_id: str
    variant: str
    notes: Optional[str] = None
    label: Optional[str] = None

class TestResponse(BaseModel):
    test_id: str
    status: str
    message: str
    project_id: str
    creative_id: str
    variant: str


# --------------------------------------------------------------------
# Basic endpoints
# --------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "message": "ScenePulse API running", "project": PROJECT_ID}


@app.get("/secure/ping")
def secure_ping(auth: bool = Depends(require_api_key)):
    return {
        "status": "ok",
        "message": "Authenticated request successful from ScenePulse API.",
    }


# --------------------------------------------------------------------
# Test endpoints
# --------------------------------------------------------------------

@app.post("/api/v1/tests", response_model=TestResponse)
def create_test(payload: TestCreateRequest, auth: bool = Depends(require_api_key)):
    test_id = new_test_id()
    doc = {
        "test_id": test_id,
        "project_id": payload.project_id,
        "creative_id": payload.creative_id,
        "variant": payload.variant,
        "label": payload.label or "",
        "notes": payload.notes or "",
        "status": "queued",
        "created_at": now_iso(),
    }

    db.collection("tests").document(test_id).set(doc)

    return TestResponse(
        test_id=test_id,
        status="queued",
        message="Test created and queued for processing.",
        project_id=payload.project_id,
        creative_id=payload.creative_id,
        variant=payload.variant,
    )


@app.get("/api/v1/tests/{test_id}")
def get_test_status(test_id: str, auth: bool = Depends(require_api_key)):
    doc_ref = db.collection("tests").document(test_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Test not found")
    return snap.to_dict()


# --------------------------------------------------------------------
# Runs endpoints
# --------------------------------------------------------------------

@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, auth: bool = Depends(require_api_key)):
    doc_ref = db.collection("runs").document(run_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Run not found")
    return snap.to_dict()


@app.get("/v1/runs")
def list_runs(
    limit: int = Query(50, ge=1, le=100),
    auth: bool = Depends(require_api_key),
):
    runs_ref = db.collection("runs").order_by(
        "created_at", direction=firestore.Query.DESCENDING
    )
    docs = runs_ref.limit(limit).stream()
    runs: List[dict] = [d.to_dict() for d in docs]
    return {"runs": runs}


# --------------------------------------------------------------------
# Create Run & Signed URLs
# --------------------------------------------------------------------

@app.post("/v1/runs", response_model=RunCreateResponse)
async def create_run_and_get_upload_urls(
    run_request: RunCreateRequest, auth: bool = Depends(require_api_key)
):
    if not upload_bucket:
        raise HTTPException(status_code=500, detail="Storage client not initialized")

    run_id = new_run_id()
    run_ref = db.collection("runs").document(run_id)
    created_at = now_iso()
    
    upload_urls_response = []
    video_storage_path = ""
    doc_storage_paths = []
    extra_docs_list = []

    # Generate Video Signed URL
    try:
        video_blob_name = f"runs/{run_id}/video/{run_request.video_filename}"
        video_blob = upload_bucket.blob(video_blob_name)
        video_storage_path = f"gs://{UPLOAD_BUCKET}/{video_blob_name}"

        video_url = video_blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=30),
            method="PUT",
            content_type="video/*"
        )
        upload_urls_response.append(SignedURLInfo(
            original_filename=run_request.video_filename,
            signed_url=video_url,
            storage_path=video_storage_path,
            key="video_file"
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not generate video URL: {e}")

    # Generate Document Signed URLs
    for i, doc_name in enumerate(run_request.doc_filenames):
        doc_blob_name = f"runs/{run_id}/docs/{doc_name}"
        doc_blob = upload_bucket.blob(doc_blob_name)
        doc_storage_path = f"gs://{UPLOAD_BUCKET}/{doc_blob_name}"
        doc_storage_paths.append(doc_storage_path)

        doc_url = doc_blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=30),
            method="PUT"
        )
        key = f"doc_file_{i}"
        upload_urls_response.append(SignedURLInfo(
            original_filename=doc_name,
            signed_url=doc_url,
            storage_path=doc_storage_path,
            key=key
        ))
        if i > 0:
            extra_docs_list.append({
                "original_filename": doc_name,
                "storage_path": doc_storage_path
            })

    # Create Firestore Document
    try:
        contact_phone_formatted = format_phone_number(run_request.contact_phone)
        run_doc = {
            "run_id": run_id,
            "status": "upload_pending",
            "created_at": created_at,
            "company_name": run_request.company_name,
            "project_id": run_request.project_id,
            "contact_name": run_request.contact_name,
            "contact_email": run_request.contact_email,
            "contact_phone": contact_phone_formatted,
            "contact_phone_raw": run_request.contact_phone,
            "creative_id": run_request.creative_id,
            "variant": run_request.variant,
            "label": run_request.optional_label,
            "notes": run_request.internal_notes,
            "storage_path": video_storage_path,
            "original_filename": run_request.video_filename,
            "doc_storage_path": doc_storage_paths[0] if doc_storage_paths else None,
            "extra_docs": extra_docs_list,
            "score": None,
            "insights": {},
        }
        run_ref.set(run_doc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create Firestore run: {e}")

    return RunCreateResponse(
        run_id=run_id,
        contact_phone=contact_phone_formatted,
        upload_urls=upload_urls_response
    )
