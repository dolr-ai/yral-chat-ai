# ---------------------------------------------------------------------------
# media.py — Media upload endpoint.
#
# WHAT THIS FILE DOES:
# Handles file uploads (images and audio) from the mobile app.
# Files are uploaded to S3 (Hetzner Object Storage) and a presigned URL
# is returned so the mobile app can display/play the file immediately.
#
# HOW THE MOBILE APP USES THIS:
# 1. User takes a photo or records audio in the chat
# 2. Mobile app uploads the file via POST /api/v1/media/upload
# 3. Server saves the file to S3 and returns a presigned URL + storage key
# 4. Mobile app includes the storage_key in the SendMessageRequest
# 5. When displaying messages, the server converts storage keys to presigned URLs
#
# ENDPOINT:
#   POST /api/v1/media/upload (multipart/form-data)
#     Fields:
#       - file: The file to upload (required)
#       - type: "image" or "audio" (required)
#     Response: { url, storage_key, type, size, mime_type, uploaded_at }
#
# PORTED FROM: yral-ai-chat/src/routes/media.rs
# ---------------------------------------------------------------------------

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form

from auth import get_current_user
from services import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.post("/media/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    type: str = Form(...),
):
    """
    Upload a media file (image or audio) to S3.

    The uploaded file is stored in S3 under the user's principal ID as a
    folder prefix (e.g., "user123/abc-def.jpg"). A presigned URL is
    generated for immediate access.

    PARAMETERS (multipart form):
        file: The file to upload
        type: "image" or "audio"

    RETURNS:
        {
            "url": "https://presigned-s3-url...",
            "storage_key": "user123/abc-def.jpg",
            "type": "image",
            "size": 1234567,
            "mime_type": "image/jpeg",
            "uploaded_at": "2026-04-13T10:30:00+00:00"
        }
    """
    user_id = get_current_user(request)

    # ---------------------------------------------------------------
    # Step 1: Validate the media type
    # ---------------------------------------------------------------
    if type not in ("image", "audio"):
        raise HTTPException(
            status_code=422,
            detail="Invalid type. Must be 'image' or 'audio'",
        )

    # ---------------------------------------------------------------
    # Step 2: Read the file content
    # ---------------------------------------------------------------
    file_bytes = await file.read()
    file_name = file.filename or "upload"
    file_size = len(file_bytes)

    if file_size == 0:
        raise HTTPException(status_code=422, detail="Empty file")

    # ---------------------------------------------------------------
    # Step 3: Validate file type and size
    # ---------------------------------------------------------------
    try:
        if type == "image":
            storage.validate_image(file_name, file_size)
        else:
            storage.validate_audio(file_name, file_size)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # ---------------------------------------------------------------
    # Step 4: Determine content type and extension
    # ---------------------------------------------------------------
    ext = storage._get_extension(file_name)
    content_type = file.content_type or storage.mime_from_extension(ext)

    # ---------------------------------------------------------------
    # Step 5: Upload to S3
    # ---------------------------------------------------------------
    try:
        storage_key, size = await storage.upload(
            user_id=user_id,
            file_bytes=file_bytes,
            file_extension=ext,
            content_type=content_type,
        )
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        raise HTTPException(status_code=503, detail="Upload failed — please try again")

    # ---------------------------------------------------------------
    # Step 6: Generate presigned URL for immediate access
    # ---------------------------------------------------------------
    presigned_url = storage.generate_presigned_url(storage_key)

    # ---------------------------------------------------------------
    # Step 7: Return the upload result
    # ---------------------------------------------------------------
    return {
        "url": presigned_url,
        "storage_key": storage_key,
        "type": type,
        "size": size,
        "mime_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
