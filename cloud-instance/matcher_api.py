"""Cloud matcher instance — proprietary cosine + KNN matching.

STATELESS: stores no student data. Each request carries the group-photo face
embeddings (queries) PLUS the enrolled gallery (multiple embeddings per student).
This instance trains a fresh KNeighborsClassifier (cosine, distance-weighted) for the
class VOTE, then uses the actual cosine SIMILARITY to (a) reject far-away faces and
(b) rank the unique assignment so the most-similar face wins a shared identity.

Run:
    MATCHER_API_KEY=key python3 -m uvicorn matcher_api:app --host 0.0.0.0 --port 8000
"""
import hmac
import os
from typing import List, Optional
from collections import defaultdict

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from sklearn.neighbors import KNeighborsClassifier

API_KEY = os.environ.get("MATCHER_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "MATCHER_API_KEY is not set — generate one with "
        "`python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
        "and put it in the server's env file. Refusing to run with auth as a no-op."
    )
PROBA_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.50"))   # KNN vote confidence floor
SIM_GATE = float(os.environ.get("MATCH_SIM_GATE", "0.40"))          # cosine similarity floor
N_NEIGHBORS = int(os.environ.get("KNN_NEIGHBORS", "2"))
# A legitimate class photo never has more than a few dozen faces, and no school's own
# gallery has more than a few thousand reference embeddings — well under these caps.
# Without a bound, any caller with the API key could force an arbitrarily large
# allocation/KNN fit as a memory-exhaustion DoS against this process.
MAX_QUERIES = 200
MAX_GALLERY = 20000

app = FastAPI(title="Face Match Instance")


class MatchRequest(BaseModel):
    queries: List[List[float]]
    gallery: List[List[float]]
    gallery_ids: List[int]
    k: Optional[int] = None
    threshold: Optional[float] = None      # proba threshold override
    sim_gate: Optional[float] = None       # cosine similarity gate override (section baseline)
    # Per-student cosine gates (student_id -> cutoff). A candidate match for a student
    # must clear THAT student's gate; anyone absent here falls back to sim_gate.
    student_gates: Optional[dict] = None


def _l2(a):
    a = np.asarray(a, dtype="float32")
    return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)


@app.get("/health")
def health():
    return {"status": "ok", "service": "matcher",
            "algo": "KNeighborsClassifier(cosine, distance-weighted) + cosine gate",
            "sim_gate": SIM_GATE}


@app.post("/match")
def match(req: MatchRequest, x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    if len(req.queries) > MAX_QUERIES or len(req.gallery) > MAX_GALLERY:
        raise HTTPException(status_code=400, detail="Request exceeds the maximum allowed queries/gallery size")

    n = len(req.queries)
    proba_thr = req.threshold if req.threshold is not None else PROBA_THRESHOLD
    sim_gate = req.sim_gate if req.sim_gate is not None else SIM_GATE
    # Normalize per-student gates to int keys (JSON object keys arrive as strings).
    student_gates = {int(k): float(v) for k, v in (req.student_gates or {}).items()}

    def gate_for(cls_id):
        return student_gates.get(int(cls_id), sim_gate)

    if n == 0 or len(req.gallery) == 0:
        return {"matches": [
            {"query_index": i, "student_id": None, "closest_id": None,
             "score": 0.0, "sim": 0.0, "status": "unknown"} for i in range(n)
        ], "k": 0, "sim_gate": sim_gate}

    X = np.asarray(req.gallery, dtype="float32")
    y = np.asarray(req.gallery_ids)
    Q = np.asarray(req.queries, dtype="float32")

    # Cosine similarity matrix: query x gallery.
    sims = _l2(Q) @ _l2(X).T               # [n, n_gallery], in [-1, 1]

    # KNN vote for the predicted class (honours k, distance-weighted).
    k = max(1, min(req.k or N_NEIGHBORS, len(X)))
    clf = KNeighborsClassifier(n_neighbors=k, metric="cosine", weights="distance")
    clf.fit(X, y)
    probas = clf.predict_proba(Q)
    classes = list(clf.classes_)

    # gallery rows per class id (to get each query's best cosine sim to a class)
    cls_idx = defaultdict(list)
    for gi, sid in enumerate(y.tolist()):
        cls_idx[int(sid)].append(gi)

    # Per query: predicted class (vote), its vote proba, and best cosine sim to that class.
    pred = []
    for i in range(n):
        cj = int(classes[int(np.argmax(probas[i]))])
        proba = float(np.max(probas[i]))
        sim = float(max(sims[i][gi] for gi in cls_idx[cj]))
        pred.append({"i": i, "cls": cj, "proba": proba, "sim": sim})

    # Greedy UNIQUE assignment ranked by cosine similarity (most similar face wins),
    # gated by both the vote proba and the cosine similarity floor.
    eligible = sorted(
        [p for p in pred if p["proba"] >= proba_thr and p["sim"] >= gate_for(p["cls"])],
        key=lambda p: -p["sim"],
    )
    used_face, used_cls, assigned = set(), set(), {}
    for p in eligible:
        if p["i"] in used_face or p["cls"] in used_cls:
            continue
        assigned[p["i"]] = p
        used_face.add(p["i"])
        used_cls.add(p["cls"])

    matches = []
    for p in pred:
        i = p["i"]
        if i in assigned:
            matches.append({"query_index": i, "student_id": p["cls"], "closest_id": p["cls"],
                            "score": round(p["proba"], 4), "sim": round(p["sim"], 4),
                            "status": "recognized"})
        else:
            # not matched (failed a gate, or its identity was claimed by a closer face)
            matches.append({"query_index": i, "student_id": None, "closest_id": p["cls"],
                            "score": round(p["sim"], 4), "sim": round(p["sim"], 4),
                            "status": "unknown"})
    return {"matches": matches, "k": k, "sim_gate": sim_gate, "gallery_size": len(X)}
