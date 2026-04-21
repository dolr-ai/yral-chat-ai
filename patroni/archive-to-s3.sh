#!/bin/bash
# ---------------------------------------------------------------------------
# archive-to-s3.sh — Archive PostgreSQL WAL files to S3 for PITR.
#
# PostgreSQL calls this for every completed WAL file (16 MB).
# We compress and upload to Hetzner Object Storage (S3-compatible).
# Enables Point-In-Time Recovery: restore to any second in the past.
#
# Called by Patroni as: archive_command = '/scripts/archive-to-s3.sh %p %f'
#   %p = full path to WAL file
#   %f = WAL filename
#
# Exit 0 = success (PG deletes local WAL). Non-zero = retry later.
# ---------------------------------------------------------------------------

set -euo pipefail

WAL_PATH="$1"
WAL_NAME="$2"

S3_BUCKET="${BACKUP_S3_BUCKET:-rishi-yral}"
S3_PREFIX="${PROJECT_REPO:-yral-chat-ai}/wal"

# Use Python + boto3 for reliable S3 upload (both installed in Patroni image)
python3 -c "
import gzip, os, sys
import boto3
from botocore.config import Config

# Read and compress the WAL file (16MB → ~2-4MB compressed)
with open('${WAL_PATH}', 'rb') as f:
    compressed = gzip.compress(f.read(), compresslevel=1)

# Upload to S3
s3 = boto3.client('s3',
    endpoint_url='https://hel1.your-objectstorage.com',
    aws_access_key_id=os.environ.get('BACKUP_S3_ACCESS_KEY', ''),
    aws_secret_access_key=os.environ.get('BACKUP_S3_SECRET_KEY', ''),
    region_name='eu-central-1',
    config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
)

key = '${S3_PREFIX}/${WAL_NAME}.gz'
s3.put_object(Bucket='${S3_BUCKET}', Key=key, Body=compressed, ContentType='application/gzip')
print(f'WAL archived: {key} ({len(compressed)} bytes)')
" && exit 0

echo "WAL archive FAILED for ${WAL_NAME}" >&2
exit 1
