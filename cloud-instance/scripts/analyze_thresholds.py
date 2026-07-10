"""Analyze genuine vs impostor cosine-score distributions per section, to decide
whether thresholds should be per-section or per-student, and what values fall out."""
import sys
import numpy as np
import database, models

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "sface"

db = database.SessionLocal()


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def load_section(section_id):
    students = db.query(models.Student).filter(models.Student.section_id == section_id).all()
    gallery = {}  # student_id -> list of vectors
    for s in students:
        rows = db.query(models.StudentEmbedding).filter(
            models.StudentEmbedding.student_id == s.id,
            models.StudentEmbedding.embedding_model == ENGINE,
        ).all()
        vecs = [np.frombuffer(r.vector, dtype=np.float32) for r in rows]
        if vecs:
            gallery[s.id] = (s.name, vecs)
    return gallery


def analyze(section_id):
    gallery = load_section(section_id)
    if len(gallery) < 2:
        print(f"  Section {section_id}: <2 students with {ENGINE} embeddings, skipping")
        return

    all_gen, all_imp = [], []
    per_student = {}  # id -> (name, gen_scores, imp_scores)

    for sid, (name, vecs) in gallery.items():
        gen, imp = [], []
        for i, v in enumerate(vecs):
            # genuine: best match to this student's OTHER photos (leave-one-out)
            others = [vecs[j] for j in range(len(vecs)) if j != i]
            if others:
                gen.append(max(cos(v, o) for o in others))
            # impostor: best match to ANY other student's photos
            best_imp = -1.0
            for oid, (_, ovecs) in gallery.items():
                if oid == sid:
                    continue
                for ov in ovecs:
                    best_imp = max(best_imp, cos(v, ov))
            imp.append(best_imp)
        per_student[sid] = (name, gen, imp)
        all_gen += gen
        all_imp += imp

    all_gen = np.array(all_gen)
    all_imp = np.array(all_imp)

    print(f"\n=== Section {section_id} ({ENGINE}) — {len(gallery)} students, "
          f"{len(all_gen)} genuine / {len(all_imp)} impostor scores ===")
    print(f"  GENUINE  : min={all_gen.min():.3f}  mean={all_gen.mean():.3f}  max={all_gen.max():.3f}")
    print(f"  IMPOSTOR : min={all_imp.min():.3f}  mean={all_imp.mean():.3f}  max={all_imp.max():.3f}")

    # Best section-level threshold: value maximizing (genuine>=t) + (impostor<t) accuracy
    cands = np.unique(np.concatenate([all_gen, all_imp]))
    best_t, best_acc = None, -1
    for t in cands:
        acc = (np.mean(all_gen >= t) + np.mean(all_imp < t)) / 2
        if acc > best_acc:
            best_acc, best_t = acc, t
    far = np.mean(all_imp >= best_t)   # impostors wrongly accepted
    frr = np.mean(all_gen < best_t)    # genuine wrongly rejected
    print(f"  -> best SECTION threshold = {best_t:.3f}  (balanced-acc={best_acc:.3f}, "
          f"FAR={far:.3f}, FRR={frr:.3f})")
    print(f"     (currently deployed cutoff for {ENGINE}: sface=0.363 / magface=0.30)")

    # Per-student viability check
    print("  Per-student separation (min genuine vs max impostor):")
    clean, overlap = 0, 0
    for sid, (name, gen, imp) in per_student.items():
        mg = min(gen) if gen else float("nan")
        xi = max(imp) if imp else float("nan")
        margin = mg - xi
        flag = "OK  " if margin > 0 else "OVERLAP"
        if margin > 0:
            clean += 1
        else:
            overlap += 1
        print(f"    {flag}  {name:20s} min_gen={mg:.3f}  max_imp={xi:.3f}  margin={margin:+.3f}  (n_photos={len(gen)+1 if gen else 0})")
    print(f"  Summary: {clean} students cleanly separable, {overlap} with genuine/impostor OVERLAP")


for sec in [1, 2, 3]:
    analyze(sec)
