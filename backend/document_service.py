import os
import uuid
from datetime import datetime
from fastapi import HTTPException, UploadFile

UPLOAD_DIR     = "uploads"
MAX_SIZE_BYTES = 5 * 1024 * 1024   # 5 MB
ALLOWED_TYPES  = {
    "application/pdf": "pdf",
    "image/jpeg":      "jpg",
    "image/png":       "png",
}
ALLOWED_DOC_TYPES = [
    # Identity documents
    "pan_individual",    # PAN card for individuals (land owner / auditor)
    "pan_organization",  # PAN card for organizations / NGOs
    "aadhaar",           # Legacy: individual ID
    "auditor_id",        # Legacy: auditor government ID
    "auditor_cert",      # Auditor professional certification

    # Land / organization documents
    "land_deed",         # Land owner - land ownership proof
    "lease_agreement",   # Land owner - lease agreement
    "ngo_cert",          # Land owner - NGO registration
    "gst",               # Organization - GST certificate
    "cin",               # Organization - Company Identification Number
    "incorporation",     # Organization - Incorporation certificate
]

os.makedirs(UPLOAD_DIR, exist_ok=True)


async def save_document(file: UploadFile, user_id: int, doc_type: str) -> dict:
    """
    Validate and save an uploaded document.
    Returns dict with file_path, file_name, file_type.
    """
    # Validate doc_type
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid doc_type. Must be one of: {ALLOWED_DOC_TYPES}"
        )

    # Validate content type
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only PDF, JPG, PNG allowed."
        )

    # Read file and check size
    contents = await file.read()
    if len(contents) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size is 5MB."
        )

    # Generate unique filename
    ext       = ALLOWED_TYPES[file.content_type]
    unique_id = uuid.uuid4().hex[:12]
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename  = f"user_{user_id}_{doc_type}_{timestamp}_{unique_id}.{ext}"
    filepath  = os.path.join(UPLOAD_DIR, filename)

    # Save file
    with open(filepath, "wb") as f:
        f.write(contents)

    return {
        "file_path": filepath,
        "file_name": filename,
        "file_type": ext,
    }


def delete_document(file_path: str):
    """Delete a document file from disk."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        print(f"[DOC] Failed to delete {file_path}: {e}")


def get_doc_types_for_role(role: str) -> list:
    """Return allowed document types for a given role."""
    mapping = {
        "land_owner":   ["pan_individual", "land_deed", "lease_agreement", "ngo_cert"],
        "organization": ["pan_organization", "gst", "cin", "incorporation"],
        "auditor":      ["pan_individual", "auditor_id", "auditor_cert"],
    }
    return mapping.get(role, [])
