# main.py — ScenePulse Backend (one video + optional multiple docs per run)
"""
FastAPI backend for ScenePulse.
- One video per run
- Optional multiple supporting documents
- V4 signed PUT URLs using IAM-based signing (no private key file)
- Firestore "runs" collection for metadata
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr

from google.cloud import firestore
from google.cloud import storage
import google.auth

# -------------------------------------------------------------------
# Configuration / Environment
# -------------------------------------------------------------------

API_KEY_HEADER = "x-api-key"
API_KEY = os.getenv("SCENEPULSE_API_KEY", "changeme")

# Use ADC (Application Default Credentials) and detect project if not set
credentials, project_from_creds = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])

PROJECT_ID = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or project_from_creds

UPLOAD_BUCKET = os.getenv("UPLOAD_BUCKET", "scenepulse-prod-scenepulse-uploads")

# Service account used for signing URLs.
# By default, assume the backend Cloud Run service account.
SIGNING_SERVICE_ACCOUNT = os.getenv(
    "SIGNING_SERVICE_ACCOUNT",
    "scenepulse-backend-sa@scenepulse-prod.iam.gserviceaccount.com",
)

# Initialize clients
firestore_client = firestore.Client(project=PROJECT_ID)
storage_client = storage.Client(project=PROJECT_ID)
upload_bucket = storage_client.bucket(UPLOAD_BUCKET)

# -------------------------------------------------------------------
# Auth dependency
# -------------------------------------------------------------------

def require_api_key(request: Request) -> bool:
    key = request.headers.get(API_KEY_HEADER)
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True

# -------------------------------------------------------------------
# Pydantic models
# -------------------------------------------------------------------

class RunCreateRequest(BaseModel):
    project_id: str = Field(..., description="Customer project identifier")
    company_name: str = Field(..., description="Customer company name")
    contact_name: str = Field(..., description="Primary contact name")
    contact_email: EmailStr = Field(..., description="Primary contact email")
    contact_phone: str = Field(..., description="Primary contact phone (raw)")
    creative_id: str = Field(..., description="Creative identifier")
    variant: str = Field(..., description="Variant label, e.g. A/B")
    video_filename: str = Field(..., description="Name of the video file to upload")
    original_filename: Optional[str] = Field(
        None, description="Original video filename as provided by customer"
    )
    content_type: str = Field(..., description="MIME type of the video")
    label: Optional[str] = Field("", description="Freeform label/tag")
    notes: Optional[str] = Field("", description="Freeform notes")
    doc_filenames: List[str] = Field(
        default_factory=list,
        description="Optional list of supporting document filenames to upload",
    )


class SignedURLInfo(BaseModel):
    original_filename: str
    signed_url: str
    storage_path: str
    key: str  # e.g. "video_file" or "doc_file_0"


class RunCreateResponse(BaseModel):
    run_id: str
    project_id: str
    video_storage_path: str
    doc_storage_paths: List[str]
    upload_urls: List[SignedURLInfo]


# -------------------------------------------------------------------
# FastAPI app & CORS
# -------------------------------------------------------------------

app = FastAPI(title="ScenePulse API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten later for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# Helper: Firestore serialization
# -------------------------------------------------------------------

def _serialize_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "ScenePulse API running",
        "project": PROJECT_ID,
        "upload_bucket": UPLOAD_BUCKET,
    }


@app.get("/secure/ping")
def secure_ping(_: bool = Depends(require_api_key)):
    return {"status": "ok", "message": "secure pong"}


@app.get("/routes", tags=["debug"])
def list_routes(_: bool = Depends(require_api_key)):
    out = []
    for r in app.routes:
        methods = sorted(list(getattr(r, "methods", []) or []))
        out.append({"path": r.path, "methods": methods})
    return {"routes": sorted(out, key=lambda x: x["path"])}


# -------------------------------------------------------------------
# POST /v1/runs — create a run + signed URLs
# -------------------------------------------------------------------

@app.post("/v1/runs", response_model=RunCreateResponse)
def create_run(payload: RunCreateRequest, _: bool = Depends(require_api_key)):
    """
    Create a new run representing ONE video + optional multiple docs.
    Returns:
      - run_id
      - GCS storage paths
      - Signed PUT URLs for video + docs
    """
    # Generate run ID
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    # Normalize filenames
    video_filename = payload.video_filename.strip()
    if not video_filename:
        raise HTTPException(status_code=400, detail="video_filename must not be empty")

    doc_filenames = [f.strip() for f in payload.doc_filenames if f.strip()]

    # ----------------------------------------------------------------
    # Generate signed URL for video using IAM-based signing
    # ----------------------------------------------------------------
    video_blob_name = f"runs/{run_id}/video/{video_filename}"
    video_blob = upload_bucket.blob(video_blob_name)
    video_storage_path = f"gs://{UPLOAD_BUCKET}/{video_blob_name}"

    try:
        video_signed_url = video_blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=30),
            method="PUT",
            content_type=payload.content_type or "video/*",
            credentials=credentials,
            service_account_email=SIGNING_SERVICE_ACCOUNT,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not generate video signed URL: {e}",
        )

    upload_urls: List[SignedURLInfo] = [
        SignedURLInfo(
            original_filename=video_filename,
            signed_url=video_signed_url,
            storage_path=video_storage_path,
            key="video_file",
        )
    ]

    # ----------------------------------------------------------------
    # Generate signed URLs for documents (optional)
    # ----------------------------------------------------------------
    doc_storage_paths: List[str] = []

    for idx, doc_name in enumerate(doc_filenames):
        doc_blob_name = f"runs/{run_id}/docs/{doc_name}"
        doc_blob = upload_bucket.blob(doc_blob_name)
        doc_storage_path = f"gs://{UPLOAD_BUCKET}/{doc_blob_name}"

        try:
            doc_signed_url = doc_blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=30),
                method="PUT",
                credentials=credentials,
                service_account_email=SIGNING_SERVICE_ACCOUNT,
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Could not generate document signed URL for {doc_name}: {e}",
            )

        doc_storage_paths.append(doc_storage_path)
        upload_urls.append(
            SignedURLInfo(
                original_filename=doc_name,
                signed_url=doc_signed_url,
                storage_path=doc_storage_path,
                key=f"doc_file_{idx}",
            )
        )

    # ----------------------------------------------------------------
    # Persist run metadata to Firestore
    # ----------------------------------------------------------------
    run_doc: Dict[str, Any] = {
        "run_id": run_id,
        "project_id": payload.project_id,
        "company_name": payload.company_name,
        "contact_name": payload.contact_name,
        "contact_email": payload.contact_email,
        "contact_phone_raw": payload.contact_phone,
        "contact_phone": payload.contact_phone,  # optional formatting later
        "creative_id": payload.creative_id,
        "variant": payload.variant,
        "label": payload.label or "",
        "notes": payload.notes or "",
        "original_filename": payload.original_filename or video_filename,
        "content_type": payload.content_type,
        "status": "upload_pending",
        "storage_path": video_storage_path,
        "doc_storage_paths": doc_storage_paths,
        "created_at": now,
        "insights": {
            "summary": "No summary provided.",
            "recommended_action": "No recommendation provided.",
            "lift_vs_baseline": 0.0,
        },
    }

    firestore_client.collection("runs").document(run_id).set(run_doc)

    # ----------------------------------------------------------------
    # Return response
    # ----------------------------------------------------------------
    return RunCreateResponse(
        run_id=run_id,
        project_id=payload.project_id,
        video_storage_path=video_storage_path,
        doc_storage_paths=doc_storage_paths,
        upload_urls=upload_urls,
    )


# -------------------------------------------------------------------
# GET /v1/runs — list runs (simple pagination)
# -------------------------------------------------------------------

@app.get("/v1/runs")
def list_runs(
    limit: int = 20,
    _: bool = Depends(require_api_key),
):
    """
    List recent runs, newest first.
    """
    limit = max(1, min(limit, 100))
    docs = (
        firestore_client.collection("runs")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )

    runs = []
    for d in docs:
        data = d.to_dict() or {}
        # Serialize datetime fields
        if isinstance(data.get("created_at"), datetime):
            data["created_at"] = _serialize_datetime(data["created_at"])
        runs.append(data)

    return {"runs": runs}


# -------------------------------------------------------------------
# GET /v1/runs/{run_id} — fetch single run
# -------------------------------------------------------------------

@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, _: bool = Depends(require_api_key)):
    doc = firestore_client.collection("runs").document(run_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Run not found")
    data = doc.to_dict() or {}
    if isinstance(data.get("created_at"), datetime):
        data["created_at"] = _serialize_datetime(data["created_at"])
    return data
