"""Bulk-enroll (or re-enroll) student face embeddings from either:
  - the school's own S3 bucket (each student's photos under students/{id}/), or
  - a local folder structure (one subfolder per student, folder name matched
    case-insensitively against Student.name)

Useful when an admin already has a batch of student photos ready and doesn't want to
capture them one at a time through the parent-portal camera flow — e.g. onboarding a
whole class at once from an existing photo archive.

Configure via environment variables before running (nothing is hardcoded — this is
meant to be reusable for any school, not tied to one specific deployment):

  DATABASE_URL           the target school's own tenant database connection string
  MODEL_URL              that school's model-service URL, e.g. http://<elastic-ip>:9100
  MODEL_KEY              that school's model-service API key
  SECTION_IDS            comma-separated section ids to enroll, e.g. "1,2,3"
  ENGINE_NAME            must match the engine the model-service is running (default: sface)
  SOURCE                 "s3" or "local"
  S3_BUCKET_NAME         required if SOURCE=s3
  AWS_DEFAULT_REGION     optional, defaults to eu-north-1, only used if SOURCE=s3
  LOCAL_DATASET_PATH     required if SOURCE=local
  ADD_LIGHTING_VARIANTS  "true" to also generate bright/dark/contrast variants, matching
                         the enrichment the parent capture flow does (default: false)
  SKIP_ALREADY_ENROLLED  "true" to skip students who already have embeddings for
                         ENGINE_NAME, so it's safe to re-run without duplicating work
                         (default: true)

Example:
  DATABASE_URL=postgresql://... MODEL_URL=http://1.2.3.4:9100 MODEL_KEY=abc123 \\
  SECTION_IDS=1,2 SOURCE=s3 S3_BUCKET_NAME=my-school-bucket \\
  python bulk_enroll_students.py
"""
import os
import sys

import requests
import numpy as np

import database
import models

MODEL_URL = os.environ["MODEL_URL"]
MODEL_KEY = os.environ["MODEL_KEY"]
ENGINE_NAME = os.environ.get("ENGINE_NAME", "sface")
SECTION_IDS = [int(x) for x in os.environ["SECTION_IDS"].split(",")]
SOURCE = os.environ.get("SOURCE", "s3").lower()
ADD_LIGHTING_VARIANTS = os.environ.get("ADD_LIGHTING_VARIANTS", "false").lower() == "true"
SKIP_ALREADY_ENROLLED = os.environ.get("SKIP_ALREADY_ENROLLED", "true").lower() == "true"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

if SOURCE == "s3":
    import boto3
    BUCKET = os.environ["S3_BUCKET_NAME"]
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "eu-north-1"))
elif SOURCE == "local":
    DATASET = os.environ["LOCAL_DATASET_PATH"]
else:
    sys.exit(f"SOURCE must be 's3' or 'local', got: {SOURCE!r}")

db = database.SessionLocal()


def gather_photos(student):
    """[(filename, bytes), ...] for a student, from whichever SOURCE is configured."""
    if SOURCE == "s3":
        prefix = f"students/{student.id}/"
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        keys = sorted(o["Key"] for o in resp.get("Contents", []))
        return [(k.rsplit("/", 1)[-1], s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()) for k in keys]

    target = student.name.strip().lower()
    for entry in os.listdir(DATASET):
        full = os.path.join(DATASET, entry)
        if os.path.isdir(full) and entry.strip().lower() == target:
            return [
                (fn, open(os.path.join(full, fn), "rb").read())
                for fn in sorted(f for f in os.listdir(full) if f.lower().endswith(IMG_EXT))
            ]
    return []


def embed(fn, contents):
    """[(variant_name, embedding_vector), ...] — one 'original' entry normally, or
    several (including 'original') if ADD_LIGHTING_VARIANTS is on."""
    endpoint = "embed_largest_variants" if ADD_LIGHTING_VARIANTS else "embed_largest"
    r = requests.post(f"{MODEL_URL}/{endpoint}", files={"file": (fn, contents)},
                       headers={"x-api-key": MODEL_KEY}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if ADD_LIGHTING_VARIANTS:
        return [(v["variant"], v["embedding"]) for v in data.get("variants", [])]
    vec = data.get("embedding")
    return [("original", vec)] if vec is not None else []


students = db.query(models.Student).filter(models.Student.section_id.in_(SECTION_IDS)).all()
total_embeddings = 0
skipped_already, skipped_no_photos = 0, 0

for s in students:
    if SKIP_ALREADY_ENROLLED:
        already = db.query(models.StudentEmbedding).filter(
            models.StudentEmbedding.student_id == s.id,
            models.StudentEmbedding.embedding_model == ENGINE_NAME,
        ).first()
        if already:
            skipped_already += 1
            print(f"  [SKIP-already-enrolled] {s.name} (section {s.section_id})")
            continue

    photos = gather_photos(s)
    if not photos:
        skipped_no_photos += 1
        print(f"  [SKIP-no-photos] {s.name} (section {s.section_id}): no photos found")
        continue

    first_vec, count = None, 0
    for fn, contents in photos:
        for variant_name, vec in embed(fn, contents):
            if vec is None:
                continue
            db.add(models.StudentEmbedding(
                student_id=s.id,
                vector=np.asarray(vec, dtype="float32").tobytes(),
                embedding_model=ENGINE_NAME,
                source=fn if variant_name == "original" else f"{fn}_{variant_name}",
            ))
            if first_vec is None and variant_name == "original":
                first_vec = vec
            count += 1

    if first_vec is not None:
        s.embedding_vector = np.asarray(first_vec, dtype="float32").tobytes()
        s.embedding_model = ENGINE_NAME
    db.commit()
    total_embeddings += count
    print(f"  [OK] {s.name:20s} (section {s.section_id}) -> {count} embeddings from {len(photos)} photos")

print(f"\nDone. {total_embeddings} embeddings stored across {len(students)} students "
      f"({skipped_already} already enrolled, {skipped_no_photos} had no photos found).")
