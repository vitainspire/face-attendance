"""Abstraction over the face model so the backend can run either:
  - MODEL_BACKEND=local  : model runs in-process (laptop) — uses engines + OpenCV.
  - MODEL_BACKEND=remote : model lives elsewhere (the "school" laptop) and is reached
                           over HTTP (the cloud instance uses this; it has NO model).

Either way the backend only deals with: name, threshold, embed_largest(bytes),
detect_embed(bytes) -> [{bbox, embedding, crop}].
"""
import base64
import io
import os
import ipaddress
import socket
from urllib.parse import urlparse

MODE = os.environ.get("MODEL_BACKEND", "local")          # 'local' | 'remote'
MODEL_URL = os.environ.get("MODEL_URL", "http://localhost:9100")
MODEL_KEY = os.environ.get("LOCAL_MODEL_KEY", "change-me")


if MODE == "local":
    import cv2
    import numpy as np
    import engines

    def _decode(contents: bytes):
        img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            try:
                from PIL import Image
                # Explicit, intentional cap rather than relying only on Pillow's own
                # default decompression-bomb threshold — a legitimate enrollment/group
                # photo never needs anywhere near this many pixels, and the upload is
                # already size-capped upstream (6MB), but a small, highly-compressed
                # file could still declare an enormous pixel count.
                Image.MAX_IMAGE_PIXELS = 40_000_000
                pil = Image.open(io.BytesIO(contents)).convert("RGB")
                img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
            except Exception:
                img = None
        return img

    def _crop_b64(img, bbox):
        h, w = img.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        ok, buf = cv2.imencode(".jpg", img[y1:y2, x1:x2])
        return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None

    # model_url/model_key are accepted (and ignored) for signature compatibility with
    # the "remote" mode below — local mode runs one shared in-process engine and can't
    # be tenant-specific, but callers in main.py pass these uniformly either way.
    def engine_name(model_url=None, model_key=None):
        return engines.get_engine().name

    def engine_threshold(model_url=None, model_key=None):
        return engines.get_engine().threshold

    def embed_largest(contents: bytes, model_url=None, model_key=None):
        img = _decode(contents)
        if img is None:
            return None
        v = engines.get_engine().embed_largest(img)
        return v.tolist() if v is not None else None

    def embed_largest_variants(contents: bytes, model_url=None, model_key=None):
        img = _decode(contents)
        if img is None:
            return []
        eng = engines.get_engine()
        out = []
        for variant_name, variant_img in engines.generate_lighting_variants(img):
            v = eng.embed_largest(variant_img)
            if v is not None:
                out.append({"variant": variant_name, "embedding": v.tolist()})
        return out

    def detect_embed(contents: bytes, model_url=None, model_key=None):
        img = _decode(contents)
        if img is None:
            return []
        out = []
        for d in engines.get_engine().detect_and_embed_all(img):
            emb = d["embedding"]
            out.append({
                "bbox": [int(x) for x in d["bbox"]],
                "embedding": emb.tolist() if hasattr(emb, "tolist") else list(emb),
                "crop": _crop_b64(img, d["bbox"]),
            })
        return out

    def detect_embed_from_path(path: str, model_url=None, model_key=None):
        """Local-mode equivalent of the remote path-based call — just reads the file
        and delegates to detect_embed, since there's no separate network hop to defer."""
        with open(path, "rb") as fh:
            contents = fh.read()
        return detect_embed(contents, model_url=model_url, model_key=model_key)

else:  # remote
    import requests
    import time

    # Cached per model-service URL — a single shared cache would leak one school's
    # engine info into another's response once there's more than one model service.
    _info_cache = {}
    _INFO_TTL_SECONDS = 30  # re-check /health periodically so an engine swap on the
                            # school instance is picked up without restarting webapp

    def _get_info(url, key):
        entry = _info_cache.get(url)
        if not entry or (time.monotonic() - entry["fetched_at"]) > _INFO_TTL_SECONDS:
            # /health now requires the API key too (previously it was open — this port is
            # reachable from the whole internet, not just from us). `key` was already
            # threaded through this function but never actually attached to the request.
            r = requests.get(url + "/health", headers={"x-api-key": key}, timeout=15)
            r.raise_for_status()
            entry = {"data": r.json(), "fetched_at": time.monotonic()}
            _info_cache[url] = entry
        return entry["data"]

    def _is_public_ip_literal(hostname: str) -> bool:
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    def _sanitize_model_url(raw_url: str) -> str:
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("MODEL_URL/model_url must use http or https")
        if not parsed.hostname:
            raise ValueError("MODEL_URL/model_url must include a hostname")
        if parsed.username or parsed.password:
            raise ValueError("MODEL_URL/model_url must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("MODEL_URL/model_url must not include query/fragment")

        host = parsed.hostname
        if _is_public_ip_literal(host):
            pass
        else:
            try:
                infos = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ValueError(f"Unable to resolve model host: {host}") from exc
            for info in infos:
                resolved_ip = info[4][0]
                ip = ipaddress.ip_address(resolved_ip)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                ):
                    raise ValueError("MODEL_URL/model_url resolves to a non-public address")

        netloc = host
        if parsed.port is not None:
            netloc = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{netloc}"

    def _resolve(model_url, model_key):
        """Every function below takes an optional per-school override — the multi-tenant
        routes in main.py pass the CURRENT school's own model-service address (from
        auth.get_model_config) instead of the original school's global one. Omitting
        both keeps exactly the original single-tenant behavior."""
        return _sanitize_model_url(model_url or MODEL_URL), model_key or MODEL_KEY

    def engine_name(model_url=None, model_key=None):
        return _get_info(*_resolve(model_url, model_key)).get("engine")

    def engine_threshold(model_url=None, model_key=None):
        return float(_get_info(*_resolve(model_url, model_key)).get("threshold", 0.45))

    def embed_largest(contents: bytes, model_url=None, model_key=None):
        url, key = _resolve(model_url, model_key)
        r = requests.post(url + "/embed_largest",
                          files={"file": ("image.jpg", contents)},
                          headers={"x-api-key": key}, timeout=60)
        r.raise_for_status()
        return r.json().get("embedding")

    def embed_largest_variants(contents: bytes, model_url=None, model_key=None):
        url, key = _resolve(model_url, model_key)
        r = requests.post(url + "/embed_largest_variants",
                          files={"file": ("image.jpg", contents)},
                          headers={"x-api-key": key}, timeout=60)
        r.raise_for_status()
        return r.json().get("variants", [])

    def detect_embed(contents: bytes, model_url=None, model_key=None):
        url, key = _resolve(model_url, model_key)
        r = requests.post(url + "/detect_embed",
                          files={"file": ("image.jpg", contents)},
                          headers={"x-api-key": key}, timeout=120)
        r.raise_for_status()
        return r.json().get("faces", [])

    def detect_embed_from_path(path: str, model_url=None, model_key=None):
        """Same as detect_embed, but reads the file only when actually forwarding it —
        used by the recognize queue worker so a queued job's bytes aren't held in memory
        until a worker slot is actually free to process it. Timeout is 90s (not 120s)
        since the Cloudflare quick tunnel in front of this server already times out
        around 100s — failing here first means a clean error instead of an edge 524."""
        url, key = _resolve(model_url, model_key)
        with open(path, "rb") as fh:
            r = requests.post(url + "/detect_embed",
                              files={"file": ("image.jpg", fh)},
                              headers={"x-api-key": key}, timeout=90)
        r.raise_for_status()
        return r.json().get("faces", [])
