# ---------------------------------------------------------------------------
# storage.py — S3 media storage service.
#
# WHAT THIS FILE DOES:
# Handles uploading and downloading media files (images, audio) to/from
# Hetzner Object Storage (which is S3-compatible). Also generates
# "presigned URLs" — temporary URLs that allow the mobile app to
# download files directly from S3 without going through our server.
#
# KEY CONCEPTS:
#   - S3 KEY: A file path in the bucket, like "user123/abc.jpg".
#     This is what we store in the database.
#   - PRESIGNED URL: A temporary URL that includes authentication.
#     Example: https://bucket.s3.eu.../user123/abc.jpg?X-Amz-Signature=...
#     Valid for 15 minutes (configurable). The mobile app uses these
#     to display images and play audio.
#   - BOTO3: Amazon's Python SDK for S3. Works with any S3-compatible
#     storage (including Hetzner Object Storage).
#
# PORTED FROM: yral-ai-chat/src/services/storage.rs
# ---------------------------------------------------------------------------

import uuid
import logging
from datetime import datetime

import boto3
from botocore.config import Config as BotoConfig

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed file types and their MIME types
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg"}

MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}


def _get_extension(filename: str) -> str:
    """Extract the file extension from a filename (e.g., '.jpg')."""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def mime_from_extension(ext: str) -> str:
    """Get the MIME type for a file extension."""
    return MIME_TYPES.get(ext.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# S3 Client (lazy-initialized)
# ---------------------------------------------------------------------------

_s3_client = None


def _get_s3_client():
    """
    Get or create the S3 client.

    We use boto3 with a CUSTOM ENDPOINT URL pointing to Hetzner Object
    Storage. This makes boto3 talk to Hetzner instead of AWS.
    """
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    if not config.AWS_ACCESS_KEY_ID or not config.AWS_S3_BUCKET:
        logger.warning("S3 not configured — media upload will not work")
        return None

    _s3_client = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT_URL or None,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )
    logger.info("S3 client initialized")
    return _s3_client


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

async def upload(
    user_id: str,
    file_bytes: bytes,
    file_extension: str,
    content_type: str,
) -> tuple[str, int]:
    """
    Upload a file to S3.

    PARAMETERS:
        user_id: The uploader's principal ID (used as folder prefix)
        file_bytes: The raw file content
        file_extension: e.g., ".jpg", ".mp3"
        content_type: e.g., "image/jpeg", "audio/mpeg"

    RETURNS: (storage_key, size)
        - storage_key: The S3 key, like "user123/abc-def-ghi.jpg"
        - size: File size in bytes

    The storage key is what we save in the database. When the mobile app
    needs the file, we generate a presigned URL from this key.
    """
    client = _get_s3_client()
    if not client:
        raise RuntimeError("S3 not configured")

    # Generate a unique filename: UUID + extension
    filename = f"{uuid.uuid4()}{file_extension}"
    key = f"{user_id}/{filename}"
    size = len(file_bytes)

    client.put_object(
        Bucket=config.AWS_S3_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
        ContentLength=size,
    )

    logger.info(f"Uploaded {key} ({size} bytes)")
    return (key, size)


# ---------------------------------------------------------------------------
# Presigned URLs
# ---------------------------------------------------------------------------

def generate_presigned_url(key: str) -> str:
    """
    Generate a temporary presigned URL for an S3 key.

    If the key is already a full URL (starts with http), return it as-is.
    Otherwise, generate a presigned URL valid for S3_URL_EXPIRES_SECONDS.
    """
    if not key:
        return ""

    # Already a full URL — no need to presign
    if key.startswith("http://") or key.startswith("https://"):
        return key

    client = _get_s3_client()
    if not client:
        return key  # Return raw key as fallback

    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.AWS_S3_BUCKET, "Key": key},
            ExpiresIn=config.S3_URL_EXPIRES_SECONDS,
        )
        return url
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {key}: {e}")
        return key


def generate_presigned_urls_batch(keys: list[str]) -> dict[str, str]:
    """
    Generate presigned URLs for multiple S3 keys at once.

    RETURNS: Dict mapping original key -> presigned URL.
    """
    result = {}
    for key in keys:
        result[key] = generate_presigned_url(key)
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_image(filename: str, size: int):
    """
    Validate an image upload.

    Checks:
    1. File extension is allowed (.jpg, .png, .gif, .webp)
    2. File size is within limits (default 10MB)

    RAISES: ValueError if validation fails.
    """
    ext = _get_extension(filename)
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError(
            f"Unsupported image format. Allowed: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )
    if size > config.MAX_IMAGE_SIZE_BYTES:
        raise ValueError(f"Image too large. Max: {config.MAX_IMAGE_SIZE_MB}MB")


def validate_audio(filename: str, size: int):
    """
    Validate an audio upload.

    Checks:
    1. File extension is allowed (.mp3, .m4a, .wav, .ogg)
    2. File size is within limits (default 20MB)

    RAISES: ValueError if validation fails.
    """
    ext = _get_extension(filename)
    if ext not in AUDIO_EXTENSIONS:
        raise ValueError(
            f"Unsupported audio format. Allowed: {', '.join(sorted(AUDIO_EXTENSIONS))}"
        )
    if size > config.MAX_AUDIO_SIZE_BYTES:
        raise ValueError(f"Audio too large. Max: {config.MAX_AUDIO_SIZE_MB}MB")
