"""Student photo storage in S3 — keeps large image blobs out of the database.

Each school brings and manages their own S3 bucket + credentials (stored encrypted in
their own tenant database, via the S3Config model) — same ownership pattern as their
EC2 server and Supabase project. Falls back to the legacy DB-blob path
(Student.image_data) for students enrolled before this existed, and for any school that
hasn't configured a bucket yet — including the original school, which still uses the
env-var + EC2-instance-role setup this module had before per-school buckets existed.
"""
import os
from typing import Optional

from sqlalchemy.orm import Session

# Original/default bucket — the EC2 instance's own IAM role provides credentials
# implicitly (no explicit access key), unlike per-school buckets configured below.
_DEFAULT_BUCKET = os.environ.get("S3_BUCKET_NAME")
_default_client = None


def _get_default_client():
    global _default_client
    if _default_client is None:
        import boto3
        _default_client = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "eu-north-1"))
    return _default_client


def _tenant_config(db: Optional[Session]):
    """Returns the current tenant's S3Config row, or None if they haven't set one up
    (or no db was given, e.g. an old call site not yet passing one)."""
    if db is None:
        return None
    import models
    cfg = db.query(models.S3Config).first()
    if cfg and cfg.access_key_encrypted and cfg.bucket_name:
        return cfg
    return None


def _client_and_bucket(db: Optional[Session]):
    cfg = _tenant_config(db)
    if cfg is None:
        return _get_default_client(), _DEFAULT_BUCKET
    import boto3
    import provisioning
    client = boto3.client(
        "s3",
        aws_access_key_id=provisioning.decrypt_secret(cfg.access_key_encrypted),
        aws_secret_access_key=provisioning.decrypt_secret(cfg.secret_key_encrypted),
        region_name=cfg.region or "us-east-1",
    )
    return client, cfg.bucket_name


def configured(db: Optional[Session] = None) -> bool:
    return _tenant_config(db) is not None or bool(_DEFAULT_BUCKET)


def _namespace(school_id: Optional[int]) -> str:
    """Every school that hasn't configured its OWN bucket falls back to the shared
    default bucket (see module docstring) — without a per-school prefix, two schools'
    identically-numbered students (each tenant's Student.id starts from 1 independently)
    would silently overwrite each other's enrollment photos there. Harmless, but applied
    unconditionally (even for schools with their own dedicated bucket) so this can never
    regress if bucket configuration ever changes."""
    return f"school_{school_id}" if school_id is not None else "default"


def upload_photo(student_id: int, contents: bytes, school_id: Optional[int] = None, db: Optional[Session] = None) -> str:
    """Uploads a student's photo to S3, returns the object key to store on the row."""
    client, bucket = _client_and_bucket(db)
    key = f"{_namespace(school_id)}/students/{student_id}/photo.jpg"
    client.put_object(Bucket=bucket, Key=key, Body=contents, ContentType="image/jpeg")
    return key


def upload_photo_frame(student_id: int, index: int, contents: bytes, school_id: Optional[int] = None, db: Optional[Session] = None) -> str:
    """Uploads one frame of a multi-frame capture (burst/video-derived enrollment)."""
    client, bucket = _client_and_bucket(db)
    key = f"{_namespace(school_id)}/students/{student_id}/frame_{index}.jpg"
    client.put_object(Bucket=bucket, Key=key, Body=contents, ContentType="image/jpeg")
    return key


def fetch_photo(key: str, db: Optional[Session] = None) -> bytes:
    """Downloads a photo's bytes from S3 by its stored key."""
    client, bucket = _client_and_bucket(db)
    resp = client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def get_photo_bytes(student, db: Optional[Session] = None) -> Optional[bytes]:
    """The student's photo bytes, from S3 if available, else the legacy DB blob."""
    if student.photo_s3_key and configured(db):
        return fetch_photo(student.photo_s3_key, db)
    return student.image_data


def has_photo(student, db: Optional[Session] = None) -> bool:
    """Mirrors get_photo_bytes' actual ability to RETURN those bytes — a photo_s3_key
    with no active S3 config (e.g. the school's bucket credentials were since removed)
    is not actually retrievable, so it must not be reported as "has a photo" here
    either; otherwise a real, existing photo becomes silently invisible downstream with
    no error (has_photo says yes, get_photo_bytes then returns None)."""
    if student.photo_s3_key:
        return configured(db)
    return bool(student.image_data)
