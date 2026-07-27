"""LOCAL "school" model service — keeps the face model on the laptop.

The cloud backend never holds the model. For any image operation it calls this service:
  - /detect_embed : group photo -> list of {bbox, embedding, crop} (for recognition)
  - /embed_largest: single face photo -> one embedding (for enrolment / parent capture)

Only embedding numbers (and small face crops) cross the wire; the model + its weights
stay here. Reached from the instance via a reverse SSH tunnel.

Run from backend/:
    LOCAL_MODEL_KEY=yourkey venv/Scripts/python -m uvicorn local_model_service:app --host 127.0.0.1 --port 9100
"""
import base64
import io
import os

import cv2
import numpy as np
from fastapi import FastAPI, File, UploadFile, Header, HTTPException

import engines

API_KEY = os.environ.get("LOCAL_MODEL_KEY", "change-me")

app = FastAPI(title="Local Model Service (school)")


def _decode(contents: bytes):
    img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
    if img is None:                       # AVIF/HEIC/etc. fallback via Pillow
        try:
            from PIL import Image
            # Explicit, intentional cap rather than relying only on Pillow's own
            # default decompression-bomb threshold — a small, highly-compressed file
            # could still declare an enormous pixel count; _cap_size() below only
            # resizes AFTER the full image is already decoded into memory, so it can't
            # prevent the peak-memory spike during decode itself.
            Image.MAX_IMAGE_PIXELS = 40_000_000
            pil = Image.open(io.BytesIO(contents)).convert("RGB")
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        except Exception:
            img = None
    if img is None:
        raise HTTPException(status_code=400, detail="Invalid image file")
    return _cap_size(img)


def _cap_size(img):
    """Downscale very large photos before detection. YuNet processes at whatever
    resolution it's given with no internal cap — a raw 4000px+ phone photo on a
    low-RAM instance can spike memory enough to get OOM-killed. Capping the longest
    side keeps memory bounded while still leaving small/distant faces detectable
    (most phone group photos only need ~2400px for that, not their native 4K+)."""
    max_dim = int(os.environ.get("MAX_INPUT_DIM", "2400"))
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return img
    scale = max_dim / longest
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def _crop_b64(img, bbox):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    ok, buf = cv2.imencode(".jpg", img[y1:y2, x1:x2])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None


def _as_list(v):
    return v.tolist() if hasattr(v, "tolist") else list(v)


@app.get("/health")
def health(x_api_key: str = Header(default=None)):
    """This port is reachable from the open internet, not just from the central
    server — without the API key check, anyone scanning for it could learn the engine
    name/dimension/threshold in use for free. Requiring the key here too (same as every
    other route) closes that off entirely instead of just trimming the response."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    eng = engines.get_engine()
    return {"status": "ok", "engine": eng.name, "dim": eng.dim, "threshold": eng.threshold}


@app.post("/detect_embed")
async def detect_embed(file: UploadFile = File(...), x_api_key: str = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    img = _decode(await file.read())
    eng = engines.get_engine()
    detected = eng.detect_and_embed_all(img)   # [{bbox, det_score, embedding}]
    faces = [{
        "bbox": [int(v) for v in d["bbox"]],
        "embedding": _as_list(d["embedding"]),
        "crop": _crop_b64(img, d["bbox"]),
    } for d in detected]
    return {"engine": eng.name, "threshold": eng.threshold, "faces": faces}


@app.post("/embed_largest")
async def embed_largest(file: UploadFile = File(...), x_api_key: str = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    img = _decode(await file.read())
    eng = engines.get_engine()
    vec = eng.embed_largest(img)
    return {"engine": eng.name, "embedding": _as_list(vec) if vec is not None else None}


@app.post("/embed_largest_variants")
async def embed_largest_variants(file: UploadFile = File(...), x_api_key: str = Header(default=None)):
    """Enrollment-time gallery enrichment: embeds the original photo PLUS a few mild,
    realistic lighting variants (brighter/darker/contrast), so a single parent capture
    gives the matcher reference points across more lighting conditions. A variant whose
    face isn't detectable after the brightness shift is silently skipped, same as a
    blinked/blurry burst frame."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    img = _decode(await file.read())
    eng = engines.get_engine()
    results = []
    for variant_name, variant_img in engines.generate_lighting_variants(img):
        vec = eng.embed_largest(variant_img)
        if vec is not None:
            results.append({"variant": variant_name, "embedding": _as_list(vec)})
    return {"engine": eng.name, "variants": results}
