"""Swappable face-recognition engines behind one interface.

Each engine provides:
    name      -> str identifier stored in Student.embedding_model
    dim       -> embedding dimensionality
    metric    -> 'cosine' | 'euclidean'
    threshold -> match cutoff on the unified similarity score (higher = more similar)
    embed_largest(img_bgr)        -> np.float32[dim] | None   (enrollment: biggest face)
    detect_and_embed_all(img_bgr) -> list[{bbox:[x1,y1,x2,y2], det_score, embedding}]
    match_batch(queries, refs, ref_ids) -> per-query {student_id, closest_id, score, status}

Engines:
  - DlibYuNetEngine     : YuNet (Apache-2.0) detect + dlib ResNet (Boost) recognize. CPU, COMMERCIAL-OK.
  - InsightFaceEngine   : buffalo_l SCRFD + ArcFace R50. Higher accuracy, NON-commercial weights.

Selected via env RECOGNITION_ENGINE ('dlib' default).
"""
import os
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
# Default engine: TinyFaceMatch (MIT, commercial-OK, ultra-lightweight).
ENGINE_NAME = os.environ.get("RECOGNITION_ENGINE", "tinyfacematch").lower()

# Model file locations (overridable via env)
_MC = os.path.join(_HERE, "models_commercial")
YUNET_ONNX = os.environ.get("YUNET_ONNX", os.path.join(_MC, "yunet.onnx"))
AURAFACE_ONNX = os.environ.get("AURAFACE_ONNX", os.path.join(_MC, "auraface_glintr100.onnx"))
TINYFACEMATCH_ONNX = os.environ.get("TINYFACEMATCH_ONNX", os.path.join(_MC, "tinyfacematch-128-pretrained.onnx"))
SFACE_ONNX = os.environ.get("SFACE_ONNX", os.path.join(_MC, "face_recognition_sface.onnx"))
DLIB_REC = os.environ.get("DLIB_REC", os.path.join(_MC, "dlib_face_recognition_resnet_model_v1.dat"))
DLIB_LMK = os.environ.get("DLIB_LMK", os.path.join(_MC, "shape_predictor_5_face_landmarks.dat"))

MIN_FACE_PX = int(os.environ.get("MIN_FACE_PX", "14"))
# Lower = more sensitive to small/distant/blurry faces, at the cost of more false
# detections (harmless downstream — a false "face" just won't match anyone in the gallery).
YUNET_SCORE_THRESHOLD = float(os.environ.get("YUNET_SCORE_THRESHOLD", "0.4"))
# Hard cap on faces actually embedded per photo — protects small/low-RAM instances from
# an OOM if a low score threshold lets through a flood of low-confidence candidates on a
# busy/textured photo. Keeps the highest-scoring (most likely genuine) faces first.
MAX_FACES_PER_IMAGE = int(os.environ.get("MAX_FACES_PER_IMAGE", "60"))


def _l2(v):
    return v / (np.linalg.norm(v) + 1e-9)


def _ort_providers():
    """CUDA on a GPU box (onnxruntime-gpu), else CPU."""
    try:
        import onnxruntime as ort
        avail = ort.get_available_providers()
    except Exception:
        return ["CPUExecutionProvider"]
    prov = []
    if "CUDAExecutionProvider" in avail:
        prov.append("CUDAExecutionProvider")
    prov.append("CPUExecutionProvider")
    return prov


def _make_yunet(cv2):
    return cv2.FaceDetectorYN.create(YUNET_ONNX, "", (320, 320),
                                     score_threshold=YUNET_SCORE_THRESHOLD, nms_threshold=0.3, top_k=5000)


# Mild, realistic lighting variants for gallery enrichment — NOT training data augmentation.
# Each variant becomes its own real gallery entry (via embed_largest_variants), giving the
# distance-based matcher more reference points across lighting conditions from the SAME
# single parent capture. Kept deliberately mild: the degradation experiment already proved
# extreme distortion collapses match quality rather than improving robustness.
def generate_lighting_variants(img_bgr):
    """Returns [(variant_name, image), ...] including the unmodified original first."""
    import cv2
    variants = [("original", img_bgr)]
    variants.append(("bright", cv2.convertScaleAbs(img_bgr, alpha=1.0, beta=30)))
    variants.append(("dark", cv2.convertScaleAbs(img_bgr, alpha=1.0, beta=-30)))
    variants.append(("high_contrast", cv2.convertScaleAbs(img_bgr, alpha=1.3, beta=0)))
    variants.append(("low_contrast", cv2.convertScaleAbs(img_bgr, alpha=0.8, beta=15)))
    return variants


class BaseEngine:
    name = "base"
    dim = 512
    metric = "cosine"
    threshold = 0.45

    def match_batch(self, queries, refs, ref_ids):
        """Match each detected face to at most ONE enrolled student (unique assignment).

        Each student appears at most once in a class photo, so we forbid two faces from
        claiming the same student: pairs are assigned greedily by descending score, and a
        face/student is consumed once used. This eliminates duplicate recognitions.
        """
        n = len(queries)
        if n == 0:
            return []
        if refs is None or len(refs) == 0:
            return [{"student_id": None, "closest_id": None, "score": 0.0, "status": "unknown"}] * n

        q = np.asarray(queries, dtype="float32")
        r = np.asarray(refs, dtype="float32")
        if self.metric == "cosine":
            qn = q / (np.linalg.norm(q, axis=1, keepdims=True) + 1e-9)
            rn = r / (np.linalg.norm(r, axis=1, keepdims=True) + 1e-9)
            sims = qn @ rn.T                                  # [N,M] cosine, higher=better
        else:  # euclidean -> distance converted to a similarity (higher=better)
            d = np.linalg.norm(q[:, None, :] - r[None, :, :], axis=2)
            sims = 1.0 - d

        # Greedy unique assignment over all (face, student) pairs above threshold.
        pairs = [(float(sims[i, j]), i, j) for i in range(n) for j in range(len(ref_ids))]
        pairs.sort(reverse=True)
        used_face, used_ref, assigned = set(), set(), {}
        for score, i, j in pairs:
            if score < self.threshold:
                break
            if i in used_face or j in used_ref:
                continue
            assigned[i] = (j, score)
            used_face.add(i)
            used_ref.add(j)

        out = []
        for i in range(n):
            bj = int(np.argmax(sims[i]))                      # closest overall (for display)
            if i in assigned:
                j, score = assigned[i]
                out.append({"student_id": int(ref_ids[j]), "closest_id": int(ref_ids[j]),
                            "score": score, "status": "recognized"})
            else:
                out.append({"student_id": None, "closest_id": int(ref_ids[bj]),
                            "score": float(sims[i, bj]), "status": "unknown"})
        return out


class DlibYuNetEngine(BaseEngine):
    """YuNet detection + dlib ResNet recognition. Commercial-friendly, CPU-only."""
    name = "dlib_resnet_v1"
    dim = 128
    metric = "euclidean"
    threshold = 0.4          # similarity = 1 - euclidean_distance; >=0.4 ~= dlib distance <= 0.6

    def __init__(self):
        import cv2
        import dlib
        self.cv2 = cv2
        # YuNet detector; input size is set per-image in _detect().
        self.det = cv2.FaceDetectorYN.create(YUNET_ONNX, "", (320, 320),
                                              score_threshold=YUNET_SCORE_THRESHOLD, nms_threshold=0.3, top_k=5000)
        self.sp = dlib.shape_predictor(DLIB_LMK)
        self.rec = dlib.face_recognition_model_v1(DLIB_REC)
        self._dlib = dlib

    def _detect(self, img_bgr):
        h, w = img_bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img_bgr)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    # Jitter = dlib re-samples the aligned face N times with small augmentations and
    # averages the descriptors. More jitter -> more stable embedding (slower).
    ENROLL_JITTERS = int(os.environ.get("DLIB_ENROLL_JITTERS", "10"))  # enrollment: accuracy
    QUERY_JITTERS = int(os.environ.get("DLIB_QUERY_JITTERS", "0"))     # group photo: speed

    def _descriptor(self, rgb, x1, y1, x2, y2, jitters=0):
        rect = self._dlib.rectangle(int(x1), int(y1), int(x2), int(y2))
        shape = self.sp(rgb, rect)                       # 5-point landmarks
        desc = self.rec.compute_face_descriptor(rgb, shape, jitters)  # aligns internally -> 128-d
        return np.asarray(desc, dtype="float32")

    def embed_largest(self, img_bgr):
        faces = self._detect(img_bgr)
        if faces.shape[0] == 0:
            return None
        # largest by area (w*h are cols 2,3)
        i = int(np.argmax(faces[:, 2] * faces[:, 3]))
        x, y, w, h = faces[i, :4]
        rgb = self.cv2.cvtColor(img_bgr, self.cv2.COLOR_BGR2RGB)
        return self._descriptor(rgb, x, y, x + w, y + h, jitters=self.ENROLL_JITTERS)

    def detect_and_embed_all(self, img_bgr):
        faces = self._detect(img_bgr)
        rgb = self.cv2.cvtColor(img_bgr, self.cv2.COLOR_BGR2RGB)
        out = []
        for f in faces:
            x, y, w, h = f[:4]
            if w < MIN_FACE_PX or h < MIN_FACE_PX:
                continue
            x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)
            out.append({
                "bbox": [x1, y1, x2, y2],
                "det_score": float(f[14]),
                "embedding": self._descriptor(rgb, x1, y1, x2, y2, jitters=self.QUERY_JITTERS),
            })
        return out


class AuraFaceEngine(BaseEngine):
    """YuNet detect (Apache) + AuraFace-v1 ResNet100 recognition (Apache).

    Fully commercial-clean, high accuracy (CFP-FP ~95%, AgeDB-30 ~96%), 512-d.
    Runs on CPU (onnxruntime) or GPU (onnxruntime-gpu, auto-selected).
    """
    name = "auraface_v1"
    dim = 512
    metric = "cosine"
    # Tunable: higher = stricter (fewer false matches, more faces sent to manual review).
    # ~0.38 captures clear matches; raise toward 0.45 for higher precision.
    threshold = float(os.environ.get("AURAFACE_THRESHOLD", "0.38"))

    def __init__(self):
        import cv2
        from insightface.model_zoo import get_model
        from insightface.utils import face_align
        self.cv2 = cv2
        self.face_align = face_align
        self.det = _make_yunet(cv2)
        providers = _ort_providers()
        self.rec = get_model(AURAFACE_ONNX, providers=providers)
        self.rec.prepare(ctx_id=0 if "CUDAExecutionProvider" in providers else -1)

    def _detect(self, img_bgr):
        h, w = img_bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img_bgr)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    def _align(self, img_bgr, face_row):
        # YuNet's 5 landmarks (cols 4..13) align positionally with the ArcFace template.
        lmk = face_row[4:14].reshape(5, 2)
        return self.face_align.norm_crop(img_bgr, lmk, image_size=112)  # 112x112 BGR

    def _embed_crops(self, crops):
        """Flip TTA: embed each aligned crop AND its mirror, average, L2-normalize.

        Mirrored faces give a second view of the same identity; averaging the two
        embeddings yields a more stable vector and higher genuine-match scores.
        Both originals and flips go through one batched forward pass.
        """
        flips = [self.cv2.flip(c, 1) for c in crops]
        feats = np.asarray(self.rec.get_feat(crops + flips), dtype="float32")  # [2N, 512]
        n = len(crops)
        avg = (feats[:n] + feats[n:]) / 2.0
        return np.stack([_l2(v) for v in avg])

    def embed_largest(self, img_bgr):
        faces = self._detect(img_bgr)
        if faces.shape[0] == 0:
            return None
        i = int(np.argmax(faces[:, 2] * faces[:, 3]))   # largest by area
        crop = self._align(img_bgr, faces[i])
        return self._embed_crops([crop])[0]

    def detect_and_embed_all(self, img_bgr):
        faces = self._detect(img_bgr)
        crops, metas = [], []
        for f in faces:
            x, y, w, h = f[:4]
            if w < MIN_FACE_PX or h < MIN_FACE_PX:
                continue
            crops.append(self._align(img_bgr, f))
            metas.append({"bbox": [int(x), int(y), int(x + w), int(y + h)],
                          "det_score": float(f[14])})
        if not crops:
            return []
        embs = self._embed_crops(crops)                  # flip-TTA, batched
        for m, e in zip(metas, embs):
            m["embedding"] = e
        return metas


class InsightFaceEngine(BaseEngine):
    """buffalo_l SCRFD + ArcFace R50. Higher accuracy; weights are NON-commercial."""
    name = "insightface_buffalo_l"
    dim = 512
    metric = "cosine"
    threshold = 0.45

    def __init__(self):
        from insightface.app import FaceAnalysis
        self.app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(640, 640))

    def embed_largest(self, img_bgr):
        faces = self.app.get(img_bgr)
        if not faces:
            return None
        largest = sorted(faces, key=lambda x: x.bbox[2] * x.bbox[3], reverse=True)[0]
        return largest.normed_embedding.astype("float32")

    def detect_and_embed_all(self, img_bgr):
        out = []
        for f in self.app.get(img_bgr):
            if (f.bbox[2] - f.bbox[0]) < MIN_FACE_PX or (f.bbox[3] - f.bbox[1]) < MIN_FACE_PX:
                continue
            out.append({
                "bbox": [int(v) for v in f.bbox],
                "det_score": float(f.det_score),
                "embedding": f.normed_embedding.astype("float32"),
            })
        return out


class DeepFaceEngine(BaseEngine):
    """DeepFace using FaceNet512 and MTCNN detector. Commercial-friendly (MIT)."""
    name = "deepface_facenet512"
    dim = 512
    metric = "cosine"
    threshold = 0.45

    def __init__(self):
        from deepface import DeepFace
        self.DeepFace = DeepFace
        try:
            self.DeepFace.build_model("Facenet512")
        except Exception:
            pass

    def embed_largest(self, img_bgr):
        rgb = img_bgr[..., ::-1] # BGR to RGB
        try:
            results = self.DeepFace.represent(img_path=rgb, model_name="Facenet512", detector_backend="mtcnn", enforce_detection=True)
            if not results: return None
            largest = sorted(results, key=lambda x: x['facial_area']['w'] * x['facial_area']['h'], reverse=True)[0]
            emb = np.array(largest['embedding'], dtype="float32")
            return _l2(emb)
        except Exception as e:
            print(f"DeepFace enrollment error: {e}")
            return None

    def detect_and_embed_all(self, img_bgr):
        rgb = img_bgr[..., ::-1] # BGR to RGB
        try:
            results = self.DeepFace.represent(img_path=rgb, model_name="Facenet512", detector_backend="mtcnn", enforce_detection=True)
            out = []
            for f in results:
                fa = f['facial_area']
                x, y, w, h = fa['x'], fa['y'], fa['w'], fa['h']
                if w < MIN_FACE_PX or h < MIN_FACE_PX: continue
                emb = np.array(f['embedding'], dtype="float32")
                out.append({
                    "bbox": [int(x), int(y), int(x+w), int(y+h)],
                    "det_score": float(f.get('face_confidence', 1.0)),
                    "embedding": _l2(emb),
                })
            return out
        except Exception as e:
            print(f"DeepFace recognize error: {e}")
            return []


class TinyFaceMatchEngine(BaseEngine):
    """YuNet detect (Apache) + TinyFaceMatch ResNet recognition (MIT).
    
    Commercial-clean, ultra-lightweight (13MB), 128-d.
    """
    name = "tinyfacematch"
    dim = 128
    metric = "cosine"
    threshold = 0.2856  # Default threshold specified in README

    def __init__(self):
        import cv2
        from insightface.utils import face_align
        from tinyfacematch import OnnxFaceEmbedder
        
        self.cv2 = cv2
        self.face_align = face_align
        self.det = _make_yunet(cv2)
        self.rec = OnnxFaceEmbedder(TINYFACEMATCH_ONNX)

    def _detect(self, img_bgr):
        h, w = img_bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img_bgr)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    def _align(self, img_bgr, face_row):
        lmk = face_row[4:14].reshape(5, 2)
        return self.face_align.norm_crop(img_bgr, lmk, image_size=112)

    def embed_largest(self, img_bgr):
        faces = self._detect(img_bgr)
        if faces.shape[0] == 0:
            return None
        i = int(np.argmax(faces[:, 2] * faces[:, 3]))   # largest by area
        crop_bgr = self._align(img_bgr, faces[i])
        crop_rgb = crop_bgr[:, :, ::-1] # Convert BGR to RGB
        
        emb = self.rec.embed_image(crop_rgb)
        return np.asarray(emb, dtype="float32")

    def detect_and_embed_all(self, img_bgr):
        faces = self._detect(img_bgr)
        if len(faces) > MAX_FACES_PER_IMAGE:
            # Keep the highest-confidence candidates only — bounds worst-case memory/CPU
            # on small instances regardless of how permissive the score threshold is.
            order = np.argsort(faces[:, 14])[::-1][:MAX_FACES_PER_IMAGE]
            faces = faces[order]

        out = []
        for f in faces:
            x, y, w, h = f[:4]
            if w < MIN_FACE_PX or h < MIN_FACE_PX:
                continue

            crop_bgr = self._align(img_bgr, f)
            crop_rgb = crop_bgr[:, :, ::-1]
            emb = self.rec.embed_image(crop_rgb)

            out.append({
                "bbox": [int(x), int(y), int(x + w), int(y + h)],
                "det_score": float(f[14]),
                "embedding": np.asarray(emb, dtype="float32"),
            })
        return out


class SFaceEngine(BaseEngine):
    """YuNet detect (Apache) + OpenCV SFace recognition (Apache-2.0, OpenCV Zoo).

    Same detector as TinyFaceMatchEngine — only the embedding model differs, so this
    is a direct, apples-to-apples experiment against tinyfacematch. Uses OpenCV's
    built-in FaceRecognizerSF, which aligns+embeds in one call from the raw YuNet
    detection row (no separate landmark-alignment step needed).
    """
    name = "sface"
    dim = 128
    metric = "cosine"
    threshold = float(os.environ.get("SFACE_THRESHOLD", "0.363"))  # OpenCV Zoo's documented cosine threshold

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.det = _make_yunet(cv2)
        self.rec = cv2.FaceRecognizerSF.create(SFACE_ONNX, "", backend_id=0, target_id=0)

    def _detect(self, img_bgr):
        h, w = img_bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img_bgr)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    def _embed(self, img_bgr, face_row):
        aligned = self.rec.alignCrop(img_bgr, face_row)
        feat = self.rec.feature(aligned)
        return np.asarray(feat, dtype="float32").reshape(-1)

    def embed_largest(self, img_bgr):
        faces = self._detect(img_bgr)
        if faces.shape[0] == 0:
            return None
        i = int(np.argmax(faces[:, 2] * faces[:, 3]))   # largest by area
        return self._embed(img_bgr, faces[i])

    def detect_and_embed_all(self, img_bgr):
        faces = self._detect(img_bgr)
        if len(faces) > MAX_FACES_PER_IMAGE:
            order = np.argsort(faces[:, 14])[::-1][:MAX_FACES_PER_IMAGE]
            faces = faces[order]

        out = []
        for f in faces:
            x, y, w, h = f[:4]
            if w < MIN_FACE_PX or h < MIN_FACE_PX:
                continue
            emb = self._embed(img_bgr, f)
            out.append({
                "bbox": [int(x), int(y), int(x + w), int(y + h)],
                "det_score": float(f[14]),
                "embedding": emb,
            })
        return out


_ENGINE = None


def get_engine() -> BaseEngine:
    """Lazily build and cache the configured engine."""
    global _ENGINE
    if _ENGINE is None:
        if ENGINE_NAME == "insightface":
            _ENGINE = InsightFaceEngine()
        elif ENGINE_NAME == "dlib":
            _ENGINE = DlibYuNetEngine()
        elif ENGINE_NAME == "deepface":
            _ENGINE = DeepFaceEngine()
        elif ENGINE_NAME == "tinyfacematch":
            _ENGINE = TinyFaceMatchEngine()
        elif ENGINE_NAME == "sface":
            _ENGINE = SFaceEngine()
        else:  # default
            _ENGINE = AuraFaceEngine()
        print(f"[engine] using '{_ENGINE.name}' (dim={_ENGINE.dim}, metric={_ENGINE.metric}, "
              f"threshold={_ENGINE.threshold})")
    return _ENGINE
