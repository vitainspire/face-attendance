"""Auto-computed recognition thresholds — per section AND per student, per engine.

Fitted to each section's own genuine-vs-impostor cosine-score distribution, so the
match cutoff reflects how confusable that specific class actually is (never a global
guess). Recomputed whenever a section's embedding gallery changes.

Scoring semantics MATCH the matcher (matcher_api.py): a face's similarity to a
student is the MAX cosine over that student's gallery embeddings. Genuine scores use
leave-one-out (a photo vs the student's OTHER photos) so they're a conservative
estimate of real-world genuine similarity.
"""
import os
import datetime

import numpy as np

import models

# Cutoffs are ANCHORED TO THE IMPOSTOR CEILING, not the midpoint of the gap. Reason:
# these scores come from clean enrollment-vs-enrollment comparisons. A real face in a
# group photo is smaller / blurrier / off-angle, so its GENUINE score to its own gallery
# is LOWER than the enrollment genuine scores (optimistic). The IMPOSTOR scores, by
# contrast, are a conservative (high) estimate of real-world impostors. So the safe cutoff
# sits just above the worst impostor: real genuine queries (which land lower than
# enrollment) still clear it, while impostors (which land lower than enrollment) don't.
IMPOSTOR_MARGIN = float(os.environ.get("THRESHOLD_IMPOSTOR_MARGIN", "0.05"))
# A student whose worst impostor is this high is genuinely confusable with a classmate —
# flagged so an admin can re-capture better photos (no cutoff fully fixes it).
CONFUSABLE_IMP = float(os.environ.get("THRESHOLD_CONFUSABLE_IMP", "0.45"))
# Below this many photos, a per-student cutoff isn't trustworthy -> use section baseline.
MIN_PHOTOS_FOR_STUDENT = int(os.environ.get("THRESHOLD_MIN_PHOTOS", "3"))
# Absolute floor so a pathological section can never open the gate wide.
MIN_THRESHOLD = float(os.environ.get("THRESHOLD_FLOOR", "0.30"))


def _cos_matrix(vecs):
    """Row-normalized cosine similarity matrix for a stack of vectors."""
    m = np.asarray(vecs, dtype="float32")
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    return m @ m.T


def _section_baseline(all_imp):
    """Section fallback cutoff = just above the impostor distribution's high end.

    Uses the 99th percentile (robust to a single anomalous impostor pair) plus the
    safety margin. NOT balanced accuracy against enrollment genuine scores — those are
    optimistic and would set the bar too high for real group-photo queries."""
    if not all_imp:
        return None
    hi = float(np.percentile(np.asarray(all_imp), 99))
    return max(hi + IMPOSTOR_MARGIN, MIN_THRESHOLD)


def compute_and_store(section_id, engine, db):
    """Recompute + persist section and per-student thresholds for one section+engine.

    Returns a summary dict. Reads the StudentEmbedding gallery (the table recognition
    actually matches against). Safe no-op if the section has <2 students enrolled.
    """
    students = db.query(models.Student).filter(models.Student.section_id == section_id).all()
    gallery = {}   # student_id -> (name, list[vec])
    for s in students:
        rows = db.query(models.StudentEmbedding).filter(
            models.StudentEmbedding.student_id == s.id,
            models.StudentEmbedding.embedding_model == engine,
        ).all()
        vecs = [np.frombuffer(r.vector, dtype=np.float32) for r in rows]
        if vecs:
            gallery[s.id] = (s.name, vecs)

    # Clear any prior thresholds for this section+engine (full recompute).
    db.query(models.RecognitionThreshold).filter(
        models.RecognitionThreshold.section_id == section_id,
        models.RecognitionThreshold.embedding_model == engine,
    ).delete()

    if len(gallery) < 2:
        db.commit()
        return {"section_id": section_id, "engine": engine,
                "status": "skipped (need >=2 enrolled students)", "students": len(gallery)}

    # Flatten to one big matrix so cosine is computed once.
    flat_vecs, flat_owner = [], []
    for sid, (_, vecs) in gallery.items():
        for v in vecs:
            flat_vecs.append(v)
            flat_owner.append(sid)
    sims = _cos_matrix(flat_vecs)
    flat_owner = np.asarray(flat_owner)
    N = len(flat_vecs)

    per_student = {}       # sid -> (gen_list, imp_list)
    all_gen, all_imp = [], []
    for idx in range(N):
        sid = int(flat_owner[idx])
        same = (flat_owner == sid)
        same[idx] = False                    # leave-one-out
        other = ~ (flat_owner == sid)
        gen = float(sims[idx][same].max()) if same.any() else None
        imp = float(sims[idx][other].max()) if other.any() else None
        per_student.setdefault(sid, ([], []))
        if gen is not None:
            per_student[sid][0].append(gen)
            all_gen.append(gen)
        if imp is not None:
            per_student[sid][1].append(imp)
            all_imp.append(imp)

    base = _section_baseline(all_imp)
    now = datetime.datetime.utcnow()
    db.add(models.RecognitionThreshold(
        section_id=section_id, student_id=None, embedding_model=engine,
        threshold=base, scope="section", quality="ok",
        n_genuine=len(all_gen), n_impostor=len(all_imp), computed_at=now,
    ))

    counts = {"ok": 0, "confusable": 0, "fallback": 0}
    for sid, (gen, imp) in per_student.items():
        n_photos = len(gen) + 1 if gen else 0
        if not gen or not imp or n_photos < MIN_PHOTOS_FOR_STUDENT:
            thr, quality = base, "fallback"
        else:
            min_gen, max_imp = min(gen), max(imp)
            # Sit just above this student's worst impostor. Never exceed the midpoint of
            # their gap (so it can't creep up toward the optimistic genuine scores).
            gap = max(min_gen - max_imp, 0.0)
            thr = max_imp + min(IMPOSTOR_MARGIN, 0.5 * gap)
            thr = max(thr, MIN_THRESHOLD)
            # Flag genuinely confusable students (a classmate scores high against them) —
            # a real data-quality signal that better enrollment photos are needed.
            quality = "confusable" if max_imp >= CONFUSABLE_IMP else "ok"
        counts[quality] = counts.get(quality, 0) + 1
        db.add(models.RecognitionThreshold(
            section_id=section_id, student_id=sid, embedding_model=engine,
            threshold=float(thr), scope="student", quality=quality,
            n_genuine=len(gen), n_impostor=len(imp), computed_at=now,
        ))

    db.commit()
    return {"section_id": section_id, "engine": engine, "section_threshold": round(base, 4),
            "students": len(gallery), "per_student": counts}


def compute_all_engines(section_id, db):
    """Recompute thresholds for EVERY engine that has a gallery in this section
    (e.g. both the primary 'sface' and the fallback 'magface'). Keeps every engine's
    cutoffs in sync whenever a section's images change."""
    sids = [s.id for s in db.query(models.Student).filter(
        models.Student.section_id == section_id).all()]
    if not sids:
        return []
    engines = [e for (e,) in db.query(models.StudentEmbedding.embedding_model).filter(
        models.StudentEmbedding.student_id.in_(sids)).distinct().all() if e]
    return [compute_and_store(section_id, eng, db) for eng in engines]


def get_thresholds(section_id, engine, db):
    """Return (section_baseline, {student_id: threshold}) for the recognize path.

    section_baseline is None if never computed (caller should fall back to the
    engine/matcher default gate).
    """
    rows = db.query(models.RecognitionThreshold).filter(
        models.RecognitionThreshold.section_id == section_id,
        models.RecognitionThreshold.embedding_model == engine,
    ).all()
    baseline = None
    per_student = {}
    for r in rows:
        if r.scope == "section":
            baseline = r.threshold
        elif r.student_id is not None:
            per_student[r.student_id] = r.threshold
    return baseline, per_student
