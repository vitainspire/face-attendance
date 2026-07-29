import os
import datetime
import ipaddress
import secrets
import asyncio
import time
import uuid
from typing import Optional, List

import requests
from fastapi import FastAPI, Depends, Request, UploadFile, File, HTTPException, Form, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import numpy as np

import database
import models
import auth
import model_client   # local (in-process model) or remote (model on the school laptop)
import s3_photos      # student photo storage (S3, falling back to legacy DB blob)
import thresholds     # auto-computed per-section / per-student match cutoffs
import provisioning   # school onboarding: encryption, deploy keypairs, SSH provisioning pipeline

# Initialize DB (SQLite file locally, or Postgres on the instance via DATABASE_URL)
models.Base.metadata.create_all(bind=database.engine)


# One-time super-admin bootstrap — only runs if no super-admin account exists yet. This
# account is wholly separate from tenant Users; it only manages school onboarding.
def _bootstrap_super_admin():
    db = database.SessionLocal()
    try:
        if db.query(models.SuperAdminUser).first():
            return
        username = os.environ.get("SUPERADMIN_USERNAME", "superadmin")
        password = os.environ.get("SUPERADMIN_PASSWORD") or auth.generate_compliant_password()
        db.add(models.SuperAdminUser(username=username, hashed_password=auth.get_password_hash(password)))
        db.commit()
        if not os.environ.get("SUPERADMIN_PASSWORD"):
            # Written to its own owner-only file rather than the general system log —
            # journal entries are often readable more widely (any local monitoring tool,
            # sometimes shipped to a centralized log service), while this file only
            # readable by whoever owns the server process.
            bootstrap_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap_superadmin_password.txt")
            with open(bootstrap_file, "w") as f:
                # codeql[py/clear-text-storage-sensitive-data]: this IS the one-time
                # credential handoff mechanism for a fresh install — there's no other way
                # to give an operator the auto-generated password. Mitigated, not
                # eliminated: chmod 600 below restricts it to this server's owner, and it
                # self-deletes on the first successful superadmin login (see
                # _super_admin_login_impl) — CodeQL can't trace that deletion back to
                # this write since it happens in an unrelated request much later.
                f.write(f"username: {username}\npassword: {password}\n")
            os.chmod(bootstrap_file, 0o600)
            print(f"[bootstrap] Created super-admin account '{username}' — "
                  f"password written to {bootstrap_file} (save it now, then delete that file).")
    finally:
        db.close()


_bootstrap_super_admin()

# Cosine+KNN matcher location (env-driven): laptop tunnel default, or localhost on the instance.
MATCHER_URL = os.environ.get("MATCHER_URL", "http://localhost:8800/match")
MATCHER_API_KEY = os.environ.get("MATCHER_API_KEY", "change-me")

app = FastAPI(title="Smart Attendance Backend")

# Only needed when the frontend is hosted on a different origin than this API (e.g. a
# Vercel-hosted static frontend calling this server directly) — same-origin deployments
# (frontend served from this app's own /static, the default) never hit CORS at all, so
# this is a no-op unless ALLOWED_FRONTEND_ORIGIN is actually set. Comma-separated so a
# production and a preview-deployment domain can both be allowed at once.
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_FRONTEND_ORIGIN", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(requests.exceptions.RequestException)
async def model_service_unreachable_handler(request: Request, exc: requests.exceptions.RequestException):
    """Catches every unhandled network error talking to a school's model service (timeout,
    connection refused, DNS failure, etc.) across every route that calls model_client —
    without this, a school's instance being stopped/unreachable surfaces as a raw,
    unhelpful 500 instead of a clear, expected error."""
    return JSONResponse(
        status_code=503,
        content={"detail": "This school's model service is currently unreachable. Please try again shortly."},
    )


@app.exception_handler(model_client.InvalidImageError)
async def invalid_image_handler(request: Request, exc: model_client.InvalidImageError):
    """A genuinely undecodable upload — NOT a model-service connectivity problem, so it
    must not fall into the 503 handler above (model_client.InvalidImageError is not a
    RequestException, so it won't)."""
    return JSONResponse(status_code=400, content={"detail": str(exc) or "Invalid image file"})


# --- Recognize queue -------------------------------------------------------------
# /teacher/recognize used to read the whole photo into memory and process it inline,
# with no limit on how many could happen at once — a load test this session showed that
# 160 concurrent uploads drives memory to the ceiling and crashes the server. Fix:
# stream each upload straight to disk (never build one big in-memory copy), enqueue a
# small job description (a file path, not photo bytes), and let a small fixed pool of
# workers process the queue — so memory use stays bounded no matter how many requests
# arrive at once. The route returns immediately with a job_id; the teacher's app polls
# /teacher/recognize_status/{job_id} for the result instead of one connection staying
# open for the whole wait.
PENDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_uploads")
RECOGNIZE_QUEUE_MAX = 48       # queue slots — beyond this, new uploads are told to retry shortly
RECOGNIZE_WORKERS = 6          # how many recognize jobs can be actively processing at once
MAX_UPLOAD_BYTES = 6 * 1024 * 1024
JOB_RESULT_TTL_SECONDS = 5 * 60
STALE_UPLOAD_SECONDS = 15 * 60  # startup sweep: delete anything left over from a crash

_recognize_queue: Optional["asyncio.Queue"] = None
_recognize_jobs = {}  # job_id -> dict — same in-memory-dict pattern as auth._failed_logins


def _process_recognize_job(job: dict):
    """The exact logic /teacher/recognize used to run inline — now run per-job, off the
    shared event loop (via run_in_threadpool), only once a worker slot is free. Reads
    the photo from disk (not from memory) and always cleans up the file when done."""
    db = None
    try:
        db, model_url, model_key = auth.resolve_tenant(job["school_id"])
        section_id = job["section_id"]
        eng_name = model_client.engine_name(model_url=model_url, model_key=model_key)
        detected = model_client.detect_embed_from_path(job["file_path"], model_url=model_url, model_key=model_key)

        students = db.query(models.Student).filter(
            models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
        ).all()
        id_to_info = {s.id: {"name": s.name, "roll_no": s.roll_no} for s in students}
        section_ids = list(id_to_info.keys())

        refs, ref_ids = [], []
        rows = db.query(models.StudentEmbedding).filter(
            models.StudentEmbedding.student_id.in_(section_ids),
            models.StudentEmbedding.embedding_model == eng_name,
        ).all()
        for r in rows:
            refs.append(np.frombuffer(r.vector, dtype=np.float32).tolist())
            ref_ids.append(r.student_id)

        if not refs:
            for s in students:
                if s.embedding_vector and s.embedding_model == eng_name:
                    refs.append(np.frombuffer(s.embedding_vector, dtype=np.float32).tolist())
                    ref_ids.append(s.id)

        sec_gate, student_gates = thresholds.get_thresholds(section_id, eng_name, db)

        queries = [d["embedding"] for d in detected]
        blank = {"status": "unknown", "student_id": None, "closest_id": None, "score": 0.0}
        matches = [dict(blank) for _ in detected]
        if queries and refs:
            try:
                payload = {"queries": queries, "gallery": refs, "gallery_ids": ref_ids, "k": 4}
                if sec_gate is not None:
                    payload["sim_gate"] = sec_gate
                if student_gates:
                    payload["student_gates"] = student_gates
                resp = requests.post(MATCHER_URL, json=payload,
                                      headers={"x-api-key": MATCHER_API_KEY}, timeout=20)
                resp.raise_for_status()
                by_idx = {m["query_index"]: m for m in resp.json()["matches"]}
                matches = [by_idx.get(i, dict(blank)) for i in range(len(detected))]
            except Exception as e:
                print(f"Matcher instance error: {e}")  # instance down -> all unknown (kill-switch)

        results = []
        for d, m in zip(detected, matches):
            if m["status"] == "recognized":
                info = id_to_info.get(m["student_id"], {})
                results.append({
                    "status": "recognized", "student_id": m["student_id"],
                    "name": info.get("name"), "roll_no": info.get("roll_no", ""),
                    "score": round(m["score"], 3), "bbox": d["bbox"], "crop": d.get("crop"),
                })
            else:
                closest = id_to_info.get(m["closest_id"], {})
                results.append({
                    "status": "unknown", "closest_match": closest.get("name", "None"),
                    "score": round(m["score"], 3), "bbox": d["bbox"], "crop": d.get("crop"),
                })

        job["status"] = "done"
        job["result"] = {
            "total_detected": len(detected), "results": results, "engine": eng_name,
            "gallery_embeddings": len(ref_ids), "enrolled_students": len(set(ref_ids)),
        }
    except HTTPException as e:
        job["status"] = "error"
        job["error"] = e.detail
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Recognition failed: {e}"
    finally:
        if db is not None:
            db.close()
        try:
            os.remove(job["file_path"])
        except OSError:
            pass
        job["finished_at"] = time.monotonic()


async def _recognize_worker():
    while True:
        job = await _recognize_queue.get()
        try:
            await run_in_threadpool(_process_recognize_job, job)
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"Unexpected worker error: {e}"
            job["finished_at"] = time.monotonic()
        finally:
            _recognize_queue.task_done()


def _prune_recognize_jobs():
    cutoff = time.monotonic() - JOB_RESULT_TTL_SECONDS
    expired = [jid for jid, j in _recognize_jobs.items() if j.get("finished_at") and j["finished_at"] < cutoff]
    for jid in expired:
        _recognize_jobs.pop(jid, None)


@app.on_event("startup")
async def _start_recognize_queue():
    global _recognize_queue
    os.makedirs(PENDING_DIR, exist_ok=True)
    # Sweep anything orphaned by a previous crash/restart — a job whose in-memory queue
    # entry no longer exists but whose file is still sitting on disk.
    cutoff = time.time() - STALE_UPLOAD_SECONDS
    for fname in os.listdir(PENDING_DIR):
        fpath = os.path.join(PENDING_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
        except OSError:
            pass
    _recognize_queue = asyncio.Queue(maxsize=RECOGNIZE_QUEUE_MAX)
    for _ in range(RECOGNIZE_WORKERS):
        asyncio.create_task(_recognize_worker())


# --- Shared helpers ---------------------------------------------------------
LOW_ATTENDANCE_THRESHOLD = 75  # percent — below this, a student is flagged "low attendance"
LOW_ATTENDANCE_ALERT_COOLDOWN_DAYS = 7  # don't re-notify the same parent more than once a week
LOW_ATTENDANCE_ALERT_PREFIX = "Low attendance alert: "  # also doubles as the cooldown marker below


def attendance_percentage(student: models.Student, db: Session):
    """Returns (percentage, present_count, graded_total, unique_days) for a student.

    'leave' days are excluded from the percentage (counted as neither present nor absent).
    """
    records = db.query(models.AttendanceRecord).filter(
        models.AttendanceRecord.student_id == student.id
    ).all()
    present = sum(1 for r in records if r.status == "present")
    absent = sum(1 for r in records if r.status == "absent")
    total = present + absent           # graded days only (leave excluded)
    pct = round((present / total) * 100, 1) if total else 0.0
    unique_days = len(set(r.date.strftime("%Y-%m-%d") for r in records))
    return pct, present, total, unique_days


def bulk_attendance_stats(student_ids, db: Session, subject: str = "All"):
    """Same stats as attendance_percentage, for MANY students in a single query.

    Returns {student_id: {"percentage", "present", "total", "unique_days", "records"}}.
    Avoids the N+1 pattern of calling attendance_percentage per student (each call is a
    separate DB round trip — expensive when the DB is reached over a network tunnel).
    """
    if not student_ids:
        return {}
    query = db.query(models.AttendanceRecord).filter(
        models.AttendanceRecord.student_id.in_(student_ids)
    )
    if subject != "All":
        query = query.filter(models.AttendanceRecord.subject == subject)
    by_student = {sid: [] for sid in student_ids}
    for r in query.all():
        by_student.setdefault(r.student_id, []).append(r)

    out = {}
    for sid, records in by_student.items():
        present = sum(1 for r in records if r.status == "present")
        absent = sum(1 for r in records if r.status == "absent")
        leave = sum(1 for r in records if r.status == "leave")
        total = present + absent
        pct = round((present / total) * 100, 1) if total else 0.0
        unique_days = len(set(r.date.strftime("%Y-%m-%d") for r in records))
        out[sid] = {"percentage": pct, "present": present, "absent": absent, "leave": leave,
                     "total": total, "unique_days": unique_days, "records": records}
    return out


def bulk_parent_usernames(student_ids, db: Session):
    """{student_id: parent_username} for MANY students in a single query."""
    if not student_ids:
        return {}
    rows = db.query(models.User.student_id, models.User.username).filter(
        models.User.student_id.in_(student_ids)
    ).all()
    return {sid: uname for sid, uname in rows}


def bulk_leave_status_for_subject(student_ids, subject: str, db: Session, on_date: datetime.datetime):
    """{student_id: 'approved'|'pending'} for students with a leave request covering
    on_date whose approval for THIS SPECIFIC SUBJECT is approved or still undecided — a
    Hindi teacher's approval only ever affects the Hindi LeaveApproval row, never the
    English one for the same days. Students with no leave request at all for this
    subject/date (or one that was rejected) are absent from the returned dict, meaning
    "handle as a normal absence" for the caller. ONE query instead of one lookup per
    student.

    `subject` is matched case-insensitively (same normalization as teacher_can_act_on) —
    submit_attendance's `subject` is arbitrary free-form input, so a submission of
    "hindi" must still find an approval stored against the canonical "Hindi"."""
    if not student_ids:
        return {}
    normalized_subject = (subject or "").strip().lower()
    rows = db.query(models.LeaveApproval.status, models.LeaveRequest.student_id).join(
        models.LeaveRequest, models.LeaveApproval.leave_request_id == models.LeaveRequest.id
    ).filter(
        models.LeaveRequest.student_id.in_(student_ids),
        func.lower(models.LeaveApproval.subject) == normalized_subject,
        models.LeaveApproval.status.in_(["approved", "pending"]),
        models.LeaveRequest.start_date <= on_date,
        models.LeaveRequest.end_date >= on_date,
    ).all()
    result = {}
    for status, student_id in rows:
        if result.get(student_id) != "approved":
            result[student_id] = status
    return result


# --- Health -----------------------------------------------------------------
@app.get("/health")
def read_root():
    return {"status": "ok", "message": "Smart Attendance API is running"}


def _find_candidates_by_username(username: str, central_db: Session):
    """Every (user, school_id, tenant_session) whose username matches — the default
    tenant, plus every onboarded school's own database. A username can genuinely exist
    in MORE than one tenant at once (e.g. this deployment really does have a "teacher2"
    in both the default tenant and a later-onboarded school) — login must try each
    candidate's actual password rather than stopping at the first name match, or
    whichever account happens to be checked first permanently shadows every other
    school's same-named account, locking its real owner out of their own login no
    matter what password an admin resets it to. school_id is None for the default
    tenant; every returned tenant_session is NOT closed here — caller must close
    whichever ones it doesn't end up using."""
    candidates = []
    default_user = central_db.query(models.User).filter(models.User.username == username).first()
    if default_user:
        candidates.append((default_user, None, None))
    schools = central_db.query(models.School).filter(models.School.status == "active").all()
    for school in schools:
        tsession = None
        try:
            supabase_url = provisioning.decrypt_secret(school.supabase_db_url_encrypted)
            tsession = database.get_tenant_sessionmaker(supabase_url)()
            candidate = tsession.query(models.User).filter(models.User.username == username).first()
        except Exception:
            # One school's database being unreachable (deleted/paused Supabase project,
            # network blip) must never break login for every other school's users —
            # but still close the session if it was opened before the failure, or a
            # flaky/paused tenant DB leaks a connection on every single login attempt.
            if tsession:
                tsession.close()
            continue
        if candidate:
            candidates.append((candidate, school.id, tsession))
        else:
            tsession.close()
    return candidates


# Verified against on every "username doesn't exist anywhere" login attempt, so that
# path costs the same bcrypt time as a real wrong-password attempt instead of returning
# near-instantly — without this, response timing alone would leak whether a given
# username exists anywhere in the system. This value is never a real account's password.
_DUMMY_PASSWORD_HASH = auth.get_password_hash(secrets.token_urlsafe(32))


# --- Login concurrency throttling -------------------------------------------------
# bcrypt password verification is deliberately CPU-slow, and this server has only 2
# real CPU cores — the blanket uvicorn --limit-concurrency gate used to mean the 4th+
# simultaneous login got an instant "server busy" even though a CPU core would free up
# a fraction of a second later. This lets a login WAIT for its turn instead of being
# rejected outright: only LOGIN_CONCURRENCY_LIMIT logins are ever actually hashing at
# once (matching real CPU count), and the moment one finishes, the next waiting login
# proceeds immediately and automatically — no client-side retry needed. A login only
# gives up with a clean 503 if it's still waiting after LOGIN_WAIT_TIMEOUT_SECONDS,
# comfortably inside the Cloudflare tunnel's own ~100s edge timeout.
LOGIN_CONCURRENCY_LIMIT = 2
LOGIN_WAIT_TIMEOUT_SECONDS = 25
_login_semaphore = asyncio.Semaphore(LOGIN_CONCURRENCY_LIMIT)


async def _throttled_login(fn, *args):
    try:
        await asyncio.wait_for(_login_semaphore.acquire(), timeout=LOGIN_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Server is busy — please try again shortly")
    try:
        return await run_in_threadpool(fn, *args)
    finally:
        _login_semaphore.release()


# --- Auth -------------------------------------------------------------------
@app.post("/token")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(database.get_db),
):
    return await _throttled_login(_login_impl, form_data, db)


def _login_impl(
    form_data: OAuth2PasswordRequestForm,
    db: Session,
):
    candidates = _find_candidates_by_username(form_data.username, db)

    matched = None
    first_tried_key = None  # the first not-already-locked candidate's lockout key
    try:
        for user, school_id, tenant_session in candidates:
            # Lockout is scoped per-account (school_id + username), not just the raw
            # username — two different schools can genuinely both have a same-named
            # account (this deployment really does, e.g. a "teacher2" in two different
            # tenants), and locking out one must never lock out the other.
            lockout_key = f"{school_id if school_id is not None else 'default'}:{form_data.username}"
            try:
                auth.check_login_lockout(lockout_key)
            except HTTPException:
                # THIS specific account is locked — skip trying its password, but keep
                # checking the other same-named candidates; a lockout on one account
                # must never block a login into a completely different one.
                continue

            if first_tried_key is None:
                first_tried_key = lockout_key
            if auth.verify_password(form_data.password, user.hashed_password):
                auth.clear_login_failures(lockout_key)
                matched = (user, school_id, tenant_session)
                break

        if matched is None:
            if not candidates:
                # No real account anywhere has this username. Route it through the SAME
                # lockout bookkeeping a real account uses (a dedicated namespaced key, so
                # it can never collide with or affect a real account's own lockout state)
                # — otherwise a nonexistent username could never reach the fast-429
                # branch below, making its response timing distinguishably different
                # from a real, repeatedly-wrong-password account after 5 attempts. That
                # difference is a username-existence oracle: 5 wrong attempts followed by
                # a 6th reveals, via response speed/status alone, whether the account is
                # real — useful reconnaissance before a credential-stuffing attempt.
                dummy_key = f"nonexistent:{form_data.username}"
                try:
                    auth.check_login_lockout(dummy_key)
                except HTTPException:
                    raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in a few minutes.")
                # Still pay the same bcrypt cost a real wrong-password attempt would, so
                # this path isn't distinguishably faster on its own either (see
                # _DUMMY_PASSWORD_HASH above).
                auth.verify_password(form_data.password, _DUMMY_PASSWORD_HASH)
                auth.record_login_failure(dummy_key)
            elif first_tried_key is not None:
                # Charge exactly ONE account for this failed attempt — the first
                # not-already-locked candidate — instead of fanning the failure out to
                # every same-named account across every tenant. Trying each candidate's
                # password is still necessary to correctly resolve who owns a collided
                # username, but a wrong password against School A's "teacher2" must not
                # also nudge School B's unrelated "teacher2" toward lockout.
                auth.record_login_failure(first_tried_key)
            else:
                # Every candidate with this username is already locked out.
                raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in a few minutes.")
            raise HTTPException(status_code=401, detail="Incorrect username or password")

        user, school_id, tenant_session = matched
        needs_capture = False
        if user.role == "parent" and user.student is not None:
            needs_capture = bool(user.student.parent_must_capture)
        student_id = user.student_id

        token = auth.create_access_token({
            "sub": user.username, "role": user.role, "school_id": school_id,
            "tv": user.token_version or 0,
        })
        return {
            "access_token": token,
            "token_type": "bearer",
            "role": user.role,
            "student_id": student_id,
            "needs_capture": needs_capture,
        }
    finally:
        # Close every tenant session opened while scanning for candidates — the
        # (default tenant's own db is closed by its own dependency, not here).
        for _, _, tenant_session in candidates:
            if tenant_session:
                tenant_session.close()


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/me/change_password")
def change_password(
    body: ChangePasswordRequest,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Self-service password change — works for admin, teacher, and parent accounts.
    Lets anyone move off the random password an admin generated for them."""
    if not auth.verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    policy_error = auth.validate_password_policy(body.new_password)
    if policy_error:
        raise HTTPException(status_code=400, detail=policy_error)
    current_user.hashed_password = auth.get_password_hash(body.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    return {"status": "ok"}


# --- Admin: registration ----------------------------------------------------
class RegisterStudentRequest(BaseModel):
    name: str
    roll_no: str
    section_id: int
    parent_email: str = None


@app.post("/admin/register_student")
def register_student(
    req: RegisterStudentRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Admin registers student INFO ONLY (no photo) and the system mints parent credentials."""
    if db.query(models.Student).filter(models.Student.roll_no == req.roll_no).first():
        raise HTTPException(status_code=400, detail=f"Roll No {req.roll_no} already exists")

    student = models.Student(
        name=req.name,
        roll_no=req.roll_no,
        section_id=req.section_id,
        parent_must_capture=True,
    )
    db.add(student)
    try:
        db.commit()
    except IntegrityError:
        # The SELECT check above already covers this in the common case — this only
        # fires for a genuine concurrent double-submit that raced past it.
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Roll No {req.roll_no} already exists")
    db.refresh(student)

    # Auto-create the parent login. Username derived from roll_no, random password.
    parent_username = f"parent_{req.roll_no}"
    parent_password = auth.generate_compliant_password()
    parent_user = models.User(
        username=parent_username,
        hashed_password=auth.get_password_hash(parent_password),
        email=req.parent_email,
        role="parent",
        student_id=student.id,
    )
    db.add(parent_user)
    db.commit()

    # Plaintext credentials returned ONCE for the admin to hand to the parent.
    return {
        "message": f"Registered {req.name} (Roll {req.roll_no})",
        "student_id": student.id,
        "parent_credentials": {"username": parent_username, "password": parent_password},
    }


class RegisterTeacherRequest(BaseModel):
    username: str
    email: Optional[str] = None


@app.post("/admin/register_teacher")
def register_teacher(
    req: RegisterTeacherRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Admin creates a new teacher login. A random password is generated and returned once."""
    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(status_code=400, detail=f"Username '{username}' already exists")

    password = auth.generate_compliant_password()
    teacher = models.User(
        username=username,
        hashed_password=auth.get_password_hash(password),
        email=req.email,
        role="teacher",
    )
    db.add(teacher)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Username '{username}' already exists")

    return {
        "message": f"Teacher account created: {username}",
        "teacher_credentials": {"username": username, "password": password},
    }


@app.get("/admin/teachers")
def list_teachers(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """All teacher accounts, for the admin's Manage Teachers view."""
    teachers = db.query(models.User).filter(models.User.role == "teacher").order_by(models.User.username).all()
    return {
        "teachers": [
            {"id": t.id, "username": t.username, "email": t.email}
            for t in teachers
        ]
    }


@app.post("/admin/teachers/{teacher_id}/reset_password")
def reset_teacher_password(
    teacher_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Generates a new random password for an existing teacher account."""
    teacher = db.query(models.User).filter(
        models.User.id == teacher_id, models.User.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    password = auth.generate_compliant_password()
    teacher.hashed_password = auth.get_password_hash(password)
    teacher.token_version = (teacher.token_version or 0) + 1
    db.commit()
    return {"username": teacher.username, "password": password}


@app.delete("/admin/teachers/{teacher_id}")
def delete_teacher(
    teacher_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    teacher = db.query(models.User).filter(
        models.User.id == teacher_id, models.User.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    name = teacher.username

    # Both of these are foreign keys into users.id with no cascade — deleting a teacher
    # who has an assignment, or who has ever decided a leave approval, would otherwise
    # fail with an unhandled foreign-key-violation 500. Assignments for this teacher are
    # removed outright (they're meaningless once the teacher is gone); past leave
    # decisions are kept for history, just with decided_by cleared instead of deleted.
    db.query(models.TeacherAssignment).filter(models.TeacherAssignment.teacher_id == teacher_id).delete()
    db.query(models.LeaveApproval).filter(models.LeaveApproval.decided_by == teacher_id).update({"decided_by": None})

    db.delete(teacher)
    db.commit()
    return {"message": f"Deleted teacher {name}"}


@app.get("/admin/embedding_status")
def embedding_status(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    mc: tuple = Depends(auth.get_model_config),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Lists students missing an image — drives the pre-generation halt notification.

    Pass section_id to scope the check to one class section; omit for all students.
    """
    eng_name = model_client.engine_name(model_url=mc[0], model_key=mc[1])
    query = db.query(models.Student).filter(models.Student.deleted_at.is_(None))
    if section_id is not None:
        query = query.filter(models.Student.section_id == section_id)
    students = query.all()
    missing = [
        {"roll_no": s.roll_no, "name": s.name}
        for s in students if not s3_photos.has_photo(s, db)
    ]
    # "pending" = has a photo but no embedding from the CURRENT engine (covers engine switches).
    pending_embeddings = [
        {"roll_no": s.roll_no, "name": s.name}
        for s in students
        if s3_photos.has_photo(s, db) and (not s.embedding_vector or s.embedding_model != eng_name)
    ]
    return {
        "total_students": len(students),
        "missing_image": missing,
        "pending_embeddings": pending_embeddings,
        "engine": eng_name,
        "ready": len(missing) == 0,
    }


@app.post("/admin/generate_embeddings")
def generate_embeddings(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    mc: tuple = Depends(auth.get_model_config),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Batch-generate embeddings. Pass section_id to do one section, omit for all students.

    Halts if any student in the scope lacks an image.
    """
    query = db.query(models.Student).filter(models.Student.deleted_at.is_(None))
    if section_id is not None:
        query = query.filter(models.Student.section_id == section_id)
    students = query.all()

    missing = [s.roll_no for s in students if not s3_photos.has_photo(s, db)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Some students have info but no image in DB. Generation halted.",
                "missing_roll_nos": missing,
            },
        )

    eng_name = model_client.engine_name(model_url=mc[0], model_key=mc[1])
    generated, skipped = 0, []
    for s in students:
        try:
            vec = model_client.embed_largest(s3_photos.get_photo_bytes(s, db), model_url=mc[0], model_key=mc[1])
        except model_client.InvalidImageError:
            skipped.append(s.roll_no)  # stored photo is corrupt/undecodable — skip like any other unusable one
            continue
        if vec is None:
            skipped.append(s.roll_no)  # photo present but engine found no usable face
            continue
        s.embedding_vector = np.asarray(vec, dtype="float32").tobytes()
        s.embedding_model = eng_name
        generated += 1
    db.commit()

    # Auto-recompute match cutoffs for every affected section, for EVERY engine that has
    # a gallery there (primary + fallback). Per section + per student, never global.
    affected = {section_id} if section_id is not None else {s.section_id for s in students}
    threshold_summary = []
    for sec in affected:
        if sec is not None:
            threshold_summary.extend(thresholds.compute_all_engines(sec, db))

    scope = "all students" if section_id is None else "the selected section"
    return {"message": f"Embeddings generated for {scope}",
            "count": generated, "engine": eng_name, "skipped_no_face": skipped,
            "thresholds": threshold_summary}


# --- Admin: this school's own S3 bucket for photo storage --------------------
class S3SettingsRequest(BaseModel):
    access_key: str
    secret_key: str
    bucket_name: str
    region: str = "us-east-1"


@app.get("/admin/s3_settings")
def get_s3_settings(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Never returns the secret key back — only enough to show the admin what's already
    configured (so they can tell it's set up without re-entering it every time)."""
    cfg = db.query(models.S3Config).first()
    if not cfg or not cfg.bucket_name:
        return {"configured": False, "bucket_name": None, "region": None}
    return {"configured": True, "bucket_name": cfg.bucket_name, "region": cfg.region}


@app.post("/admin/s3_settings")
def save_s3_settings(
    req: S3SettingsRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    if not req.access_key.strip() or not req.secret_key.strip() or not req.bucket_name.strip():
        raise HTTPException(status_code=400, detail="Access key, secret key, and bucket name are all required")

    cfg = db.query(models.S3Config).first()
    if not cfg:
        cfg = models.S3Config()
        db.add(cfg)
    cfg.access_key_encrypted = provisioning.encrypt_secret(req.access_key.strip())
    cfg.secret_key_encrypted = provisioning.encrypt_secret(req.secret_key.strip())
    cfg.bucket_name = req.bucket_name.strip()
    cfg.region = req.region.strip() or "us-east-1"
    db.commit()
    return {"message": "S3 settings saved"}


# --- Recognition confidence tuning (per school) -------------------------------
DEFAULT_AUTO_CHECK_THRESHOLD = 0.90


class RecognitionSettingsRequest(BaseModel):
    auto_check_threshold: float
    # Minutes offset from UTC (e.g. 330 for IST). Optional — omitting it leaves
    # whatever's already configured (or UTC/0 default) untouched.
    timezone_offset_minutes: Optional[int] = None


@app.get("/recognition_settings")
def get_recognition_settings(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin", "teacher")),
):
    """Readable by both admin and teacher — the teacher's attendance-scanning screen
    needs this value too, to know what counts as a confident-enough auto-check."""
    cfg = db.query(models.RecognitionSettings).first()
    threshold = cfg.auto_check_threshold if cfg and cfg.auto_check_threshold is not None else DEFAULT_AUTO_CHECK_THRESHOLD
    return {
        "auto_check_threshold": threshold,
        "is_default": cfg is None or cfg.auto_check_threshold is None,
        "timezone_offset_minutes": cfg.timezone_offset_minutes if cfg and cfg.timezone_offset_minutes is not None else 0,
    }


@app.post("/admin/recognition_settings")
def save_recognition_settings(
    req: RecognitionSettingsRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    if not (0.5 <= req.auto_check_threshold <= 0.99):
        raise HTTPException(status_code=400, detail="Threshold must be between 0.50 and 0.99")
    if req.timezone_offset_minutes is not None and not (-720 <= req.timezone_offset_minutes <= 840):
        raise HTTPException(status_code=400, detail="Timezone offset must be a real UTC offset in minutes")
    cfg = db.query(models.RecognitionSettings).first()
    if not cfg:
        cfg = models.RecognitionSettings()
        db.add(cfg)
    cfg.auto_check_threshold = req.auto_check_threshold
    if req.timezone_offset_minutes is not None:
        cfg.timezone_offset_minutes = req.timezone_offset_minutes
    db.commit()
    return {
        "message": "Saved",
        "auto_check_threshold": cfg.auto_check_threshold,
        "timezone_offset_minutes": cfg.timezone_offset_minutes or 0,
    }


def _school_today_range(db: Session):
    """(today_start, today_end, local_today_start) for THIS school's configured local
    time (RecognitionSettings.timezone_offset_minutes, default 0/UTC).

    today_start/today_end are UTC instants — use these against fields that store a real
    timestamp (e.g. AttendanceRecord.date, set from the server clock at creation time).

    local_today_start is the school's local calendar date expressed as a naive midnight
    with NO offset applied — use this against fields that store a literal date the user
    picked (e.g. LeaveRequest.start_date/end_date, SchoolEvent.event_date, all parsed via
    `datetime.fromisoformat("YYYY-MM-DD")` with no timezone conversion). Comparing one of
    those against the UTC-shifted today_start instead is off by the timezone offset —
    for a school in IST (UTC+5:30) it happens to still exclude yesterday correctly, but
    for a school WEST of UTC it wrongly excludes today's own date/event."""
    cfg = db.query(models.RecognitionSettings).first()
    offset = datetime.timedelta(minutes=(cfg.timezone_offset_minutes or 0) if cfg else 0)
    local_now = datetime.datetime.utcnow() + offset
    local_today_start = datetime.datetime.combine(local_now.date(), datetime.time.min)
    today_start = local_today_start - offset
    today_end = today_start + datetime.timedelta(days=1)
    return today_start, today_end, local_today_start


# --- School events / announcements (per school) -------------------------------
# Replaces the old static "Upcoming Event" placeholder on the parent dashboard —
# admin creates real events, parents see the upcoming ones.
class SchoolEventRequest(BaseModel):
    title: str
    description: Optional[str] = None
    event_date: str  # "YYYY-MM-DD"


@app.get("/admin/events")
def list_events(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Every event, newest first — the admin management view (includes past events,
    unlike the parent-facing /parent/events which only shows upcoming ones)."""
    events = db.query(models.SchoolEvent).order_by(models.SchoolEvent.event_date.desc()).all()
    return {"events": [
        {
            "id": e.id,
            "title": e.title,
            "description": e.description,
            "event_date": e.event_date.strftime("%Y-%m-%d"),
        } for e in events
    ]}


@app.post("/admin/events")
def create_event(
    req: SchoolEventRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    try:
        event_date = datetime.datetime.fromisoformat(req.event_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid event date")
    event = models.SchoolEvent(
        title=title,
        description=(req.description or "").strip() or None,
        event_date=event_date,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {"id": event.id, "title": event.title, "description": event.description,
            "event_date": event.event_date.strftime("%Y-%m-%d")}


@app.delete("/admin/events/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    event = db.query(models.SchoolEvent).filter(models.SchoolEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    title = event.title
    db.delete(event)
    db.commit()
    return {"message": f"Deleted {title}"}


@app.get("/parent/events")
def list_upcoming_events(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("parent")),
):
    """Upcoming events only (today or later), soonest first, capped at 5 — a dashboard
    preview, not a full history."""
    _, _, local_today_start = _school_today_range(db)
    events = db.query(models.SchoolEvent).filter(
        models.SchoolEvent.event_date >= local_today_start
    ).order_by(models.SchoolEvent.event_date.asc()).limit(5).all()
    return {"events": [
        {
            "id": e.id,
            "title": e.title,
            "description": e.description,
            "event_date": e.event_date.strftime("%Y-%m-%d"),
        } for e in events
    ]}


@app.post("/admin/recompute_thresholds")
def recompute_thresholds(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Recompute match cutoffs (section baseline + per student) for EVERY engine that has
    a gallery in scope (primary + fallback). Runs automatically on embedding generation
    and parent capture; this is the manual re-run button."""
    if section_id is not None:
        sections = [section_id]
    else:
        sections = [s.id for s in db.query(models.Section).all()]
    results = []
    for sec in sections:
        results.extend(thresholds.compute_all_engines(sec, db))
    return {"results": results}


@app.get("/admin/thresholds")
def list_thresholds(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Inspect the computed cutoffs — section baseline + per-student, with quality flags."""
    q = db.query(models.RecognitionThreshold)
    if section_id is not None:
        q = q.filter(models.RecognitionThreshold.section_id == section_id)
    rows = q.all()
    id_to_name = {s.id: s.name for s in db.query(models.Student).all()}
    return {"thresholds": [
        {"section_id": r.section_id,
         "student": id_to_name.get(r.student_id) if r.student_id else None,
         "scope": r.scope, "engine": r.embedding_model,
         "threshold": round(r.threshold, 4), "quality": r.quality,
         "n_genuine": r.n_genuine, "n_impostor": r.n_impostor}
        for r in sorted(rows, key=lambda x: (x.section_id, x.scope != "section", x.threshold))
    ]}


# --- Admin: manage students (view / edit / delete) --------------------------
@app.get("/admin/classes")
def list_classes(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin", "teacher")),
):
    """Classes with their sections — used to build the cascading filter. Readable by
    teachers too (not just admin) since they need it to populate their own section
    dropdowns (take attendance, analytics, leave review)."""
    classes = db.query(models.ClassGroup).order_by(models.ClassGroup.name).all()
    return {
        "classes": [
            {
                "id": c.id,
                "name": c.name,
                "sections": [
                    {"id": s.id, "name": s.name}
                    for s in sorted(c.sections, key=lambda x: x.name)
                ],
            }
            for c in classes
        ]
    }


class CreateClassRequest(BaseModel):
    name: str


@app.post("/admin/classes")
def create_class(
    req: CreateClassRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Class name is required")
    if db.query(models.ClassGroup).filter(models.ClassGroup.name == name).first():
        raise HTTPException(status_code=400, detail=f"Class '{name}' already exists")
    cls = models.ClassGroup(name=name)
    db.add(cls)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Class '{name}' already exists")
    db.refresh(cls)
    return {"id": cls.id, "name": cls.name}


class CreateSectionRequest(BaseModel):
    class_id: int
    name: str


@app.post("/admin/sections")
def create_section(
    req: CreateSectionRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    cls = db.query(models.ClassGroup).filter(models.ClassGroup.id == req.class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Section name is required")
    if db.query(models.Section).filter(
        models.Section.class_id == req.class_id, models.Section.name == name
    ).first():
        raise HTTPException(status_code=400, detail=f"Section '{name}' already exists in this class")
    sec = models.Section(name=name, class_id=req.class_id)
    db.add(sec)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Section '{name}' already exists in this class")
    db.refresh(sec)
    return {"id": sec.id, "name": sec.name, "class_id": sec.class_id}


class CreateSubjectRequest(BaseModel):
    class_id: int
    name: str


@app.get("/admin/subjects")
def list_subjects(
    class_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin", "teacher")),
):
    """Subjects for one class — shared by every section within it. Readable by teachers
    too, since they need this to populate the subject dropdown when taking attendance."""
    subjects = db.query(models.Subject).filter(
        models.Subject.class_id == class_id
    ).order_by(models.Subject.name).all()
    return {"subjects": [{"id": s.id, "name": s.name} for s in subjects]}


@app.post("/admin/subjects")
def create_subject(
    req: CreateSubjectRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    cls = db.query(models.ClassGroup).filter(models.ClassGroup.id == req.class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Subject name is required")
    if db.query(models.Subject).filter(
        models.Subject.class_id == req.class_id, models.Subject.name == name
    ).first():
        raise HTTPException(status_code=400, detail=f"Subject '{name}' already exists in this class")
    subject = models.Subject(name=name, class_id=req.class_id)
    db.add(subject)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Subject '{name}' already exists in this class")
    db.refresh(subject)
    return {"id": subject.id, "name": subject.name, "class_id": subject.class_id}


@app.delete("/admin/subjects/{subject_id}")
def delete_subject(
    subject_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    subject = db.query(models.Subject).filter(models.Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")
    name = subject.name
    db.delete(subject)
    db.commit()
    return {"message": f"Deleted {name}"}


# --- Admin: teacher assignments (which teacher teaches which subject, in which section) ---
class CreateTeacherAssignmentRequest(BaseModel):
    teacher_id: int
    section_id: int
    subject: str


@app.get("/admin/teacher_assignments")
def list_teacher_assignments(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    query = db.query(models.TeacherAssignment)
    if section_id is not None:
        query = query.filter(models.TeacherAssignment.section_id == section_id)
    assignments = query.all()
    return {"assignments": [
        {
            "id": a.id,
            "teacher_id": a.teacher_id,
            "teacher_username": a.teacher.username if a.teacher else None,
            "section_id": a.section_id,
            "subject": a.subject,
        } for a in assignments
    ]}


@app.post("/admin/teacher_assignments")
def create_teacher_assignment(
    req: CreateTeacherAssignmentRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Assigns a teacher to teach one subject in one section. Once ANY teacher is
    assigned to a (section, subject) pair, only assigned teacher(s) may take attendance
    or approve leave requests for it — a pair with no assignment at all stays open to
    any teacher, unchanged from before this feature existed."""
    teacher = db.query(models.User).filter(
        models.User.id == req.teacher_id, models.User.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    section = db.query(models.Section).filter(models.Section.id == req.section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    subject = req.subject.strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")
    if db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == req.teacher_id,
        models.TeacherAssignment.section_id == req.section_id,
        func.lower(models.TeacherAssignment.subject) == subject.lower(),
    ).first():
        raise HTTPException(status_code=400, detail=f"{teacher.username} is already assigned to {subject} for this section")
    assignment = models.TeacherAssignment(teacher_id=req.teacher_id, section_id=req.section_id, subject=subject)
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return {"id": assignment.id, "teacher_id": assignment.teacher_id,
            "teacher_username": teacher.username, "section_id": assignment.section_id,
            "subject": assignment.subject}


@app.delete("/admin/teacher_assignments/{assignment_id}")
def delete_teacher_assignment(
    assignment_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    assignment = db.query(models.TeacherAssignment).filter(models.TeacherAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(assignment)
    db.commit()
    return {"message": "Assignment removed"}


def teacher_can_act_on(db: Session, teacher_id: int, section_id: int, subject: str) -> bool:
    """True if this teacher may take attendance / recognize photos / approve a leave
    request for this (section, subject) pair. Rules, in order:

    1. If THIS teacher is specifically assigned to this pair, always True.
    2. If this teacher has ANY assignment elsewhere, they're boxed into exactly their
       own assigned pairs — they don't get to freely act on some other, unrelated,
       still-unassigned subject just because nobody else claimed it either (e.g.
       Pallavi assigned to Hindi/5C must NOT be able to take attendance for Math/5C,
       even if Math/5C has no assignment).
    3. If this teacher has NO assignment anywhere, but this SCHOOL has assignments for
       OTHER teachers, they're denied too — a fresh teacher account created partway
       through a school actively using this feature should default to no access, not
       unrestricted access to every other teacher's sections until an admin gets around
       to assigning them.
    4. Only if NEITHER this teacher NOR anyone else at the school has ever been
       assigned anything does the pair stay fully open — this is what keeps a school
       that hasn't adopted the feature at all working exactly as before.

    Subject comparisons are case/whitespace-insensitive: /teacher/recognize and
    /teacher/submit_attendance accept `subject` as arbitrary free-form input (not
    validated against the canonical Subject list), so an exact-string match could be
    bypassed just by submitting "Hindi" as "hindi" — an unassigned teacher would see
    zero rows for the mismatched casing and get waved through as if the pair had no
    assignment at all."""
    normalized_subject = subject.strip().lower()

    if db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == teacher_id,
        models.TeacherAssignment.section_id == section_id,
        func.lower(models.TeacherAssignment.subject) == normalized_subject,
    ).first():
        return True

    teacher_has_any_assignment = db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == teacher_id
    ).first() is not None
    if teacher_has_any_assignment:
        return False

    # A brand-new teacher account (zero assignments of their own) only stays "fully
    # open" if this school has never used the assignment feature at all — the moment
    # ANY teacher anywhere in the school has an assignment, the feature is in active
    # use, and a fresh account with none yet should default to no access rather than
    # unrestricted access to every other teacher's sections during the normal window
    # before an admin gets around to assigning them.
    if db.query(models.TeacherAssignment.id).first() is not None:
        return False

    pair_has_any_assignment = db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.section_id == section_id,
        func.lower(models.TeacherAssignment.subject) == normalized_subject,
    ).first() is not None
    return not pair_has_any_assignment


def teacher_can_access_section(db: Session, teacher_id: int, section_id: int) -> bool:
    """Subject-agnostic version of teacher_can_act_on, for routes that read/sync a
    section's full roster or biometric data rather than acting on one specific
    subject (e.g. downloading the embeddings gallery for offline recognition — matching
    a face to a name has to happen before a subject is even chosen). Same rules: a
    teacher with an assignment ANYWHERE in this section may access it; a teacher with
    assignments only in OTHER sections may not; a teacher with none at all stays open
    ONLY if this school has never used the assignment feature for anyone (see
    teacher_can_act_on for the full reasoning)."""
    if db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == teacher_id,
        models.TeacherAssignment.section_id == section_id,
    ).first():
        return True

    teacher_has_any_assignment = db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == teacher_id
    ).first() is not None
    if teacher_has_any_assignment:
        return False
    # Same reasoning as teacher_can_act_on: a fresh account only stays "fully open" if
    # this school has never used the assignment feature at all.
    return db.query(models.TeacherAssignment.id).first() is None


@app.get("/admin/students")
def list_students(
    section_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Student listing for the admin database view, optionally filtered by section."""
    query = db.query(models.Student).filter(models.Student.deleted_at.is_(None))
    if section_id is not None:
        query = query.filter(models.Student.section_id == section_id)
    students = query.order_by(models.Student.roll_no).all()

    ids = [s.id for s in students]
    stats = bulk_attendance_stats(ids, db)          # 1 query total, not 1-per-student
    parents = bulk_parent_usernames(ids, db)         # 1 query total, not 1-per-student

    rows = []
    for s in students:
        st = stats.get(s.id, {"percentage": 0.0, "present": 0, "total": 0, "unique_days": 0})
        rows.append({
            "id": s.id,
            "name": s.name,
            "roll_no": s.roll_no,
            "section_id": s.section_id,
            "has_image": s3_photos.has_photo(s, db),
            "has_embedding": s.embedding_vector is not None,
            "parent_username": parents.get(s.id),
            "percentage": st["percentage"],
            "present": st["present"],
            "total": st["total"],
            "unique_days": st["unique_days"],
        })
    return {"students": rows}


class UpdateStudentRequest(BaseModel):
    name: str
    roll_no: str
    section_id: int


@app.put("/admin/students/{student_id}")
def update_student(
    student_id: int,
    req: UpdateStudentRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    student = db.query(models.Student).filter(
        models.Student.id == student_id, models.Student.deleted_at.is_(None)
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Roll no must stay unique across other students.
    clash = db.query(models.Student).filter(
        models.Student.roll_no == req.roll_no,
        models.Student.id != student_id,
    ).first()
    if clash:
        raise HTTPException(status_code=400, detail=f"Roll No {req.roll_no} already in use")

    student.name = req.name
    student.roll_no = req.roll_no
    student.section_id = req.section_id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Roll No {req.roll_no} already in use")
    return {"message": f"Updated {student.name}", "id": student.id}


@app.post("/admin/students/{student_id}/reset_parent")
def reset_parent_credentials(
    student_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Regenerate the parent's login (forgot-password). The student record — photo,
    embedding, attendance — is left untouched."""
    student = db.query(models.Student).filter(
        models.Student.id == student_id, models.Student.deleted_at.is_(None)
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    new_password = auth.generate_compliant_password()
    username = f"parent_{student.roll_no}"
    parent = db.query(models.User).filter(models.User.student_id == student_id).first()
    if parent is None:
        # No parent login existed — create a fresh one linked to this student.
        parent = models.User(
            username=username,
            hashed_password=auth.get_password_hash(new_password),
            role="parent",
            student_id=student.id,
        )
        db.add(parent)
    else:
        parent.username = username  # keep username in sync with current roll no
        parent.hashed_password = auth.get_password_hash(new_password)
        parent.token_version = (parent.token_version or 0) + 1
    db.commit()

    return {
        "message": f"New login generated for {student.name}",
        "parent_credentials": {"username": username, "password": new_password},
    }


@app.delete("/admin/students/{student_id}")
def delete_student(
    student_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Soft delete: hides the student from active rosters/logins, but keeps the row,
    their attendance history, and embeddings intact — reversible via the restore
    endpoint, and never breaks a year-long report that references a student who left
    mid-year. Only the parent login (a credential, not historical data) is actually
    removed, same as before; it's regenerable via 'Reset Login' after a restore."""
    student = db.query(models.Student).filter(
        models.Student.id == student_id, models.Student.deleted_at.is_(None)
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    db.query(models.ParentChild).filter(
        models.ParentChild.student_id == student_id
    ).delete()
    own_parent = db.query(models.User).filter(models.User.student_id == student_id).first()
    if own_parent:
        # This parent's login is keyed off the legacy User.student_id field, which
        # points at the student just deleted — but the SAME login may also cover other,
        # still-active siblings via ParentChild (see link_child). Only remove the whole
        # login if there are truly no other children left on it; otherwise re-point it to
        # a remaining sibling so that family doesn't lose access to their other kids.
        sibling_link = db.query(models.ParentChild).filter(
            models.ParentChild.parent_id == own_parent.id
        ).join(models.Student, models.Student.id == models.ParentChild.student_id).filter(
            models.Student.deleted_at.is_(None)
        ).first()
        if sibling_link:
            own_parent.student_id = sibling_link.student_id
            db.query(models.ParentChild).filter(models.ParentChild.id == sibling_link.id).delete()
        else:
            db.query(models.ParentChild).filter(
                models.ParentChild.parent_id == own_parent.id
            ).delete()
            db.delete(own_parent)
    student.deleted_at = datetime.datetime.utcnow()
    db.commit()
    return {"message": f"Deleted {student.name}"}


@app.post("/admin/students/{student_id}/restore")
def restore_student(
    student_id: int,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Undoes a soft delete. The parent login isn't recreated automatically — use
    'Reset Login' afterward to generate a fresh one for the family."""
    student = db.query(models.Student).filter(
        models.Student.id == student_id, models.Student.deleted_at.isnot(None)
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Deleted student not found")
    student.deleted_at = None
    db.commit()
    return {"message": f"Restored {student.name}"}


@app.get("/admin/students/deleted")
def list_deleted_students(
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Recently-deleted students, most recent first — backs the 'Deleted Students /
    Restore' admin view."""
    students = db.query(models.Student).filter(
        models.Student.deleted_at.isnot(None)
    ).order_by(models.Student.deleted_at.desc()).all()
    return {"students": [
        {"id": s.id, "name": s.name, "roll_no": s.roll_no, "section_id": s.section_id,
         "deleted_at": s.deleted_at.isoformat()}
        for s in students
    ]}


MAX_BURST_FRAMES = 15  # frontend sends 9; generous headroom without being unbounded


async def _read_capped(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Reads an upload in chunks, rejecting early instead of materializing an
    arbitrarily large file into memory first and checking size after the fact — the
    exact memory-exhaustion pattern that crashed the server via /teacher/recognize
    before that route was rewritten to stream to disk. These parent endpoints don't
    need disk-queueing (each parent uploads once, not continuously like a teacher
    taking daily attendance), but still must never hold an unbounded amount of a
    single upload in memory."""
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1 << 20)  # 1MB at a time
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Photo is too large (max 6MB)")
        chunks.append(chunk)
    return b"".join(chunks)


# Recognized by their leading magic bytes — no image library needed for this cheap check
# (Pillow/opencv are deliberately NOT installed on the cloud instance). This isn't a full
# format validation, just a real check that we're not storing/forwarding to the model
# service something that clearly isn't image data at all (e.g. a polyglot file crafted to
# smuggle a second payload past a check that only relies on face-detection succeeding).
_IMAGE_MAGIC_BYTES = (
    b"\xff\xd8\xff",           # JPEG
    b"\x89PNG\r\n\x1a\n",      # PNG
    b"RIFF",                    # WEBP (starts RIFF....WEBP — checked further below)
)


def _looks_like_image(contents: bytes) -> bool:
    if contents[:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return True
    return any(contents.startswith(sig) for sig in _IMAGE_MAGIC_BYTES if sig != b"RIFF")


# --- Parent -----------------------------------------------------------------
@app.get("/parent/status")
def parent_status(
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Lightweight, always-fresh check of whether this parent still needs to capture —
    used when resuming a saved session, since a login token alone can't reflect a state
    change (e.g. the parent saw the capture screen last time but never submitted it)."""
    student = current_user.student
    if student is None:
        raise HTTPException(status_code=400, detail="No student linked to this parent")
    return {"needs_capture": bool(student.parent_must_capture)}


@app.post("/parent/upload_photo")
async def parent_upload_photo(
    file: UploadFile = File(...),
    db: Session = Depends(auth.get_tenant_db),
    mc: tuple = Depends(auth.get_model_config),
    school_id: Optional[int] = Depends(auth.get_school_id),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Parent (first login) uploads a live camera photo, saved to their student record."""
    if current_user.student is None:
        raise HTTPException(status_code=400, detail="No student linked to this parent")

    contents = await _read_capped(file)
    if not _looks_like_image(contents):
        raise HTTPException(status_code=400, detail="File does not look like a valid image (JPEG/PNG/WEBP)")
    if model_client.embed_largest(contents, model_url=mc[0], model_key=mc[1]) is None:
        raise HTTPException(status_code=400, detail="No face detected — please retake the photo")

    student = current_user.student
    if s3_photos.configured(db):
        student.photo_s3_key = s3_photos.upload_photo(student.id, contents, school_id=school_id, db=db)
    else:
        student.image_data = contents  # S3 not set up — fall back to the old DB blob
    student.parent_must_capture = False
    db.commit()
    return {"message": "Photo saved successfully", "student_id": student.id}


@app.post("/parent/upload_photo_burst")
async def parent_upload_photo_burst(
    files: List[UploadFile] = File(...),
    db: Session = Depends(auth.get_tenant_db),
    mc: tuple = Depends(auth.get_model_config),
    school_id: Optional[int] = Depends(auth.get_school_id),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Parent captures a short burst (several frames from a ~3s live capture) instead of
    one static photo. Each frame with a detectable face becomes its own reference
    embedding, giving the KNN classifier several real views of the student instead of one.
    """
    if current_user.student is None:
        raise HTTPException(status_code=400, detail="No student linked to this parent")
    if not files:
        raise HTTPException(status_code=400, detail="No frames received")
    if len(files) > MAX_BURST_FRAMES:
        raise HTTPException(status_code=400, detail=f"Too many frames (max {MAX_BURST_FRAMES})")

    student = current_user.student
    eng_name = model_client.engine_name(model_url=mc[0], model_key=mc[1])

    # A fresh capture replaces any previous frame-based embeddings for this student.
    db.query(models.StudentEmbedding).filter(
        models.StudentEmbedding.student_id == student.id
    ).delete()

    first_vec, first_key, first_contents = None, None, None
    saved = 0
    for i, f in enumerate(files):
        contents = await _read_capped(f)
        if not _looks_like_image(contents):
            continue  # skip a non-image frame rather than failing the whole burst
        # Enrichment: embed the frame AS-IS plus a few mild, realistic lighting variants
        # (brighter/darker/contrast) of the same real frame — one parent capture ends up
        # giving the matcher reference points across more lighting conditions, with no
        # extra effort from the parent. See engines.generate_lighting_variants().
        try:
            variants = model_client.embed_largest_variants(contents, model_url=mc[0], model_key=mc[1])
        except model_client.InvalidImageError:
            continue  # magic bytes looked valid but the frame is actually corrupt — skip it too
        if not variants:
            continue  # skip frames with no detectable face (blink, motion blur, etc.)

        if first_vec is None:
            original = next((v for v in variants if v["variant"] == "original"), variants[0])
            first_vec = original["embedding"]
            if s3_photos.configured(db):
                first_key = s3_photos.upload_photo_frame(student.id, i, contents, school_id=school_id, db=db)
            else:
                first_contents = contents
        elif s3_photos.configured(db):
            s3_photos.upload_photo_frame(student.id, i, contents, school_id=school_id, db=db)

        for v in variants:
            db.add(models.StudentEmbedding(
                student_id=student.id,
                vector=np.asarray(v["embedding"], dtype="float32").tobytes(),
                embedding_model=eng_name,
                source=f"frame_{i}_{v['variant']}",
            ))
        saved += 1

    if saved == 0:
        raise HTTPException(status_code=400, detail="No face detected in any frame — please retake")

    # Representative single photo/embedding, kept for older code paths that expect one
    # (admin embedding_status, generate_embeddings, has_image checks, etc.).
    if s3_photos.configured(db):
        student.photo_s3_key = first_key
    else:
        student.image_data = first_contents
    student.embedding_vector = np.asarray(first_vec, dtype="float32").tobytes()
    student.embedding_model = eng_name
    student.parent_must_capture = False
    db.commit()

    # This student's gallery changed -> refit their section's cutoffs (all engines), then
    # check whether THIS student came back flagged "confusable" (a classmate's photo scores
    # close enough to be a real accuracy risk) — if so, nudge the parent to retake now,
    # while they still have the camera open, instead of only surfacing it to admin later.
    quality_warning = None
    if student.section_id is not None:
        try:
            thresholds.compute_all_engines(student.section_id, db)
            my_threshold = db.query(models.RecognitionThreshold).filter(
                models.RecognitionThreshold.section_id == student.section_id,
                models.RecognitionThreshold.student_id == student.id,
                models.RecognitionThreshold.embedding_model == eng_name,
                models.RecognitionThreshold.scope == "student",
            ).first()
            if my_threshold and my_threshold.quality == "confusable":
                quality_warning = (
                    "These photos look similar to another student's in the same class, which "
                    "can make attendance less reliable. For best accuracy, consider retaking "
                    "with clearer lighting and the face closer to the camera."
                )
        except Exception as e:
            print(f"Threshold recompute after burst failed: {e}")

    return {
        "message": f"Captured {saved} of {len(files)} frames successfully",
        "student_id": student.id,
        "frames_saved": saved,
        "quality_warning": quality_warning,
    }


def resolve_parent_student(current_user, student_id, db):
    """Returns the Student a parent request is about. If student_id is given (multi-child
    accounts), verifies it's actually linked to this parent before returning it — either
    via the legacy single User.student_id field or an extra ParentChild link. With no
    student_id, falls back to the legacy field (unchanged behavior for single-child
    accounts, the overwhelming majority). A soft-deleted student is treated as gone —
    the parent dashboard shouldn't show a removed child."""
    if student_id is not None:
        if current_user.student_id == student_id:
            student = current_user.student
        else:
            link = db.query(models.ParentChild).filter(
                models.ParentChild.parent_id == current_user.id,
                models.ParentChild.student_id == student_id,
            ).first()
            if not link:
                raise HTTPException(status_code=403, detail="This student is not linked to your account")
            student = db.query(models.Student).filter(models.Student.id == student_id).first()
    else:
        student = current_user.student
    return student if student and student.deleted_at is None else None


def resolve_parent_student_ids(current_user, student_id, db):
    """Returns the student IDs a parent-scoped query should cover. With a specific
    student_id, returns just that one child (after verifying the link). With none,
    returns ALL of this parent's linked children — used for account-wide views like the
    notification badge, so nothing is missed just because a different child's tab is open."""
    if student_id is not None:
        s = resolve_parent_student(current_user, student_id, db)
        return [s.id] if s else []
    ids = set()
    if current_user.student_id:
        ids.add(current_user.student_id)
    links = db.query(models.ParentChild).filter(models.ParentChild.parent_id == current_user.id).all()
    ids.update(l.student_id for l in links)
    if not ids:
        return []
    active = db.query(models.Student.id).filter(
        models.Student.id.in_(ids), models.Student.deleted_at.is_(None)
    ).all()
    return [row[0] for row in active]


@app.get("/parent/children")
def get_parent_children(
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Every child linked to this parent account (legacy single link + any additional
    ParentChild rows). A single-child parent just gets a one-item list."""
    ids = set()
    if current_user.student_id:
        ids.add(current_user.student_id)
    links = db.query(models.ParentChild).filter(models.ParentChild.parent_id == current_user.id).all()
    ids.update(l.student_id for l in links)
    students = db.query(models.Student).filter(
        models.Student.id.in_(ids), models.Student.deleted_at.is_(None)
    ).order_by(models.Student.name).all()
    return {"children": [{"id": s.id, "name": s.name, "roll_no": s.roll_no} for s in students]}


class LinkChildRequest(BaseModel):
    parent_username: str
    student_id: int


@app.post("/admin/link_child")
def link_child(
    req: LinkChildRequest,
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("admin")),
):
    """Links an additional existing student to an existing parent account — for families
    with more than one child at the school, without needing a second parent login."""
    parent = db.query(models.User).filter(
        models.User.username == req.parent_username, models.User.role == "parent"
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent account not found")
    student = db.query(models.Student).filter(
        models.Student.id == req.student_id, models.Student.deleted_at.is_(None)
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if parent.student_id == student.id:
        raise HTTPException(status_code=400, detail=f"{student.name} is already this parent's primary child")
    existing = db.query(models.ParentChild).filter(
        models.ParentChild.parent_id == parent.id, models.ParentChild.student_id == student.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"{student.name} is already linked to this parent")
    db.add(models.ParentChild(parent_id=parent.id, student_id=student.id))
    db.commit()

    # The student being linked usually already has their own solo parent login (created
    # automatically when their account was first set up). Once merged into `parent`'s
    # combined account, that old login is redundant — leaving it active would let the
    # family log in two different ways for the same child. Only auto-remove it if it's a
    # plain single-child account with no other kids on it; if it has its own extra
    # ParentChild links, it's a distinct family situation an admin should handle by hand.
    deactivated_username = None
    old_account = db.query(models.User).filter(
        models.User.student_id == student.id, models.User.role == "parent", models.User.id != parent.id
    ).first()
    if old_account:
        other_links = db.query(models.ParentChild).filter(
            models.ParentChild.parent_id == old_account.id
        ).count()
        if other_links == 0:
            deactivated_username = old_account.username
            db.delete(old_account)
            db.commit()

    message = f"Linked {student.name} (Roll {student.roll_no}) to parent '{parent.username}'"
    if deactivated_username:
        message += f". Deactivated the old separate login '{deactivated_username}' for the same student."
    return {"message": message, "deactivated_username": deactivated_username}


@app.get("/parent/attendance")
def parent_attendance(
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    student = resolve_parent_student(current_user, student_id, db)
    if student is None:
        raise HTTPException(status_code=400, detail="No student linked to this parent")

    pct, present, total, unique_days = attendance_percentage(student, db)

    # Full history (used for subject-wise month/year stats and today's classes).
    all_records = db.query(models.AttendanceRecord).filter(
        models.AttendanceRecord.student_id == student.id
    ).order_by(models.AttendanceRecord.date.desc()).all()

    now = datetime.datetime.utcnow()
    today_start, today_end, _ = _school_today_range(db)

    # --- Today's classes: one entry per subject recorded today, using the school's own
    # local calendar day (same boundary submit_attendance uses) rather than the raw UTC
    # date — otherwise a class taken between the school's local midnight and the
    # following UTC midnight can vanish from "today" once UTC has already rolled over. ---
    today_classes = [
        {"subject": r.subject, "status": r.status}
        for r in all_records if today_start <= r.date < today_end
    ]

    # --- Subject-wise stats for this month and this year (leave counted separately) ---
    def _pct(recs):
        p = sum(1 for r in recs if r.status == "present")
        a = sum(1 for r in recs if r.status == "absent")
        lv = sum(1 for r in recs if r.status == "leave")
        t = p + a                       # graded days only (leave not counted in %)
        return (round(p / t * 100, 1) if t else 0.0, p, t, lv)

    subjects = sorted({r.subject for r in all_records})
    by_subject = []
    for subj in subjects:
        month_recs = [r for r in all_records
                      if r.subject == subj and r.date.year == now.year and r.date.month == now.month]
        year_recs = [r for r in all_records
                     if r.subject == subj and r.date.year == now.year]
        m_pct, m_p, m_t, m_lv = _pct(month_recs)
        y_pct, y_p, y_t, y_lv = _pct(year_recs)
        by_subject.append({
            "subject": subj,
            "month_percentage": m_pct, "month_present": m_p, "month_total": m_t, "month_leave": m_lv,
            "year_percentage": y_pct, "year_present": y_p, "year_total": y_t, "year_leave": y_lv,
        })

    return {
        "student": {"name": student.name, "roll_no": student.roll_no},
        "percentage": pct,
        "present": present,
        "total": total,
        "unique_days": unique_days,
        "today": today_classes,
        "by_subject": by_subject,
        "records": [
            {"date": r.date.isoformat(), "status": r.status, "subject": r.subject}
            for r in all_records
        ],
    }


@app.get("/parent/notifications")
def parent_notifications(
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Absence (and other) alerts, newest first. With no student_id, covers ALL of this
    parent's linked children combined (so the bell badge never misses a sibling's alert)."""
    student_ids = resolve_parent_student_ids(current_user, student_id, db)
    if not student_ids:
        raise HTTPException(status_code=400, detail="No student linked to this parent")

    notes = db.query(models.Notification).filter(
        models.Notification.student_id.in_(student_ids)
    ).order_by(models.Notification.created_at.desc()).limit(50).all()

    return {
        "unread_count": sum(1 for n in notes if not n.is_read),
        "notifications": [
            {
                "id": n.id,
                "message": n.message,
                "created_at": n.created_at.isoformat(),
                "is_read": n.is_read,
            }
            for n in notes
        ],
    }


@app.post("/parent/notifications/read")
def mark_notifications_read(
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    """Marks notifications as read — all children's by default, or just one if specified."""
    student_ids = resolve_parent_student_ids(current_user, student_id, db)
    if not student_ids:
        raise HTTPException(status_code=400, detail="No student linked to this parent")

    db.query(models.Notification).filter(
        models.Notification.student_id.in_(student_ids),
        models.Notification.is_read == False,  # noqa: E712
    ).update({models.Notification.is_read: True}, synchronize_session=False)
    db.commit()
    return {"message": "Notifications marked as read"}


# --- Teacher ----------------------------------------------------------------
@app.get("/teacher/roster")
def get_teacher_roster(
    section_id: int,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Lightweight student list for a section — backs the attendance review checklist
    (manual correction of recognition results, and bulk mark-all-present)."""
    if not teacher_can_access_section(db, current_user.id, section_id):
        raise HTTPException(status_code=403, detail="You are not assigned to this section")
    students = db.query(models.Student).filter(
        models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
    ).order_by(models.Student.roll_no).all()
    return {"students": [{"id": s.id, "name": s.name, "roll_no": s.roll_no} for s in students]}


@app.get("/teacher/my_subjects")
def get_teacher_subjects(
    section_id: int,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Subjects THIS teacher may act on for this section — filtered through the exact
    same check (teacher_can_act_on) that /teacher/recognize, /teacher/submit_attendance,
    and leave approval use, so the dropdown can never offer a subject the backend would
    then reject. A teacher who's never been assigned to anything sees every subject in
    the section (unchanged legacy behavior)."""
    all_subjects = _subjects_for_section(section_id, db)
    allowed = [s for s in all_subjects if teacher_can_act_on(db, current_user.id, section_id, s)]
    return {"subjects": allowed}


@app.get("/teacher/sync_section/{section_id}")
def sync_section(
    section_id: int,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Downloads the embeddings payload for on-device TFLite inference (mobile). This
    returns raw biometric face-embedding vectors, not just names/roll numbers — the
    most sensitive data class in the app — so it's gated the same way recognize/
    submit_attendance are, just without a subject (matching a face to a name has to
    happen before a subject is even chosen)."""
    if not teacher_can_access_section(db, current_user.id, section_id):
        raise HTTPException(status_code=403, detail="You are not assigned to this section")

    students = db.query(models.Student).filter(
        models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
    ).all()

    payload = []
    for s in students:
        if s.embedding_vector:
            vec = np.frombuffer(s.embedding_vector, dtype=np.float32).tolist()
            payload.append({
                "student_id": s.id,
                "roll_no": s.roll_no,
                "name": s.name,
                "embedding": vec,
            })
    return {"section_id": section_id, "students": payload}


@app.post("/teacher/recognize")
async def recognize_attendance(
    section_id: int = Form(...),
    subject: str = Form("General"),
    file: UploadFile = File(...),
    school_id: Optional[int] = Depends(auth.get_school_id),
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Queues a group photo for recognition against this section's enrolled embeddings,
    instead of processing it inline — this never holds more than a bounded number of
    photos in memory at once, no matter how many teachers upload at the same time.
    Streams the upload straight to disk (never builds one big in-memory copy), then
    returns immediately with a job_id. Poll GET /teacher/recognize_status/{job_id} for
    the actual result — matching (cosine / euclidean per engine, greedy unique
    assignment) happens in the background worker once a processing slot is free."""
    if not teacher_can_act_on(db, current_user.id, section_id, subject):
        raise HTTPException(status_code=403, detail=f"You are not assigned to teach {subject} for this section")

    job_id = str(uuid.uuid4())
    file_path = os.path.join(PENDING_DIR, f"{job_id}.jpg")

    size = 0
    try:
        with open(file_path, "wb") as out:
            while True:
                chunk = await file.read(1 << 20)  # 1MB at a time — never one big bytes object
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Photo is too large (max 6MB)")
                out.write(chunk)
    except HTTPException:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=400, detail="Could not read the uploaded photo")

    job = {
        "job_id": job_id,
        "file_path": file_path,
        "section_id": section_id,
        "school_id": school_id,
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": time.monotonic(),
        "finished_at": None,
    }
    try:
        _recognize_queue.put_nowait(job)
    except asyncio.QueueFull:
        os.remove(file_path)
        raise HTTPException(status_code=503,
                             detail="Server is busy processing other photos — please try again shortly")

    _recognize_jobs[job_id] = job
    return {"job_id": job_id, "status": "queued"}


@app.get("/teacher/recognize_status/{job_id}")
def recognize_status(
    job_id: str,
    school_id: Optional[int] = Depends(auth.get_school_id),
    _: models.User = Depends(auth.require_role("teacher")),
):
    """Poll this after /teacher/recognize until status is 'done' (or it raises on
    'error'). A 404 means the job is gone (expired after 5 minutes, or the server
    restarted while it was queued) — the app should treat that as 'please retake and
    resubmit the photo', not hang waiting.

    _recognize_jobs is one process-wide dict shared by every school on this instance
    (job_ids are unguessable UUIDs, but that alone isn't an authorization check) — a
    teacher must only ever be able to read a job that was submitted by their OWN school,
    otherwise a leaked/logged job_id from School A could hand School B's recognized
    student names and face-crop photos to a teacher who has nothing to do with School A."""
    _prune_recognize_jobs()
    job = _recognize_jobs.get(job_id)
    if job is None or job["school_id"] != school_id:
        raise HTTPException(status_code=404,
                             detail="Job not found or expired — please retake and resubmit the photo")
    if job["status"] == "queued":
        return {"status": "queued"}
    if job["status"] == "error":
        raise HTTPException(status_code=502, detail=job["error"] or "Recognition failed")
    return {"status": "done", **job["result"]}


class SubmitAttendanceRequest(BaseModel):
    section_id: int
    subject: str = "General"
    present_student_ids: list[int]


@app.post("/teacher/submit_attendance")
def submit_attendance(
    req: SubmitAttendanceRequest,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Records attendance for the section and returns the absent list."""
    if not teacher_can_act_on(db, current_user.id, req.section_id, req.subject):
        raise HTTPException(status_code=403, detail=f"You are not assigned to teach {req.subject} for this section")

    all_students = db.query(models.Student).filter(
        models.Student.section_id == req.section_id, models.Student.deleted_at.is_(None)
    ).all()

    records = []
    absent = []
    on_leave = []
    pending_leave = []
    today_start, today_end, local_today_start = _school_today_range(db)

    all_student_ids = [s.id for s in all_students]

    # Existing records for THIS subject today, so a re-submit updates instead of duplicating.
    # Subject is matched case-insensitively — it's arbitrary free-form input (not
    # validated against the canonical Subject list), so "Math" and "math" on the same
    # day must resolve to the same record instead of creating a duplicate.
    existing = {
        r.student_id: r for r in db.query(models.AttendanceRecord).filter(
            models.AttendanceRecord.student_id.in_(all_student_ids),
            func.lower(models.AttendanceRecord.subject) == (req.subject or "").strip().lower(),
            models.AttendanceRecord.date >= today_start,
            models.AttendanceRecord.date < today_end,
        ).all()
    }

    # Which students have a leave request covering today whose approval for THIS
    # SUBJECT is approved or still pending — ONE query for the whole section instead of
    # one lookup per absent student. local_today_start (not today_start) because
    # LeaveRequest.start_date/end_date store a literal date, not a UTC timestamp.
    leave_status = bulk_leave_status_for_subject(all_student_ids, req.subject, db, local_today_start)

    for s in all_students:
        if s.id in req.present_student_ids:
            status = "present"                  # attended this class — overrides any leave
        else:
            # Missed this class: excused as "leave" if this subject's teacher already
            # approved it, "pending" if a decision is still outstanding, else "absent".
            state = leave_status.get(s.id)
            status = "leave" if state == "approved" else ("pending" if state == "pending" else "absent")

        # One record per student + subject + day: update if it already exists, else create.
        rec = existing.get(s.id)
        if rec:
            rec.status = status
        else:
            records.append(models.AttendanceRecord(student_id=s.id, status=status, subject=req.subject))

        if status == "absent":
            absent.append({"student_id": s.id, "name": s.name, "roll_no": s.roll_no})
        elif status == "leave":
            on_leave.append({"student_id": s.id, "name": s.name, "roll_no": s.roll_no})
        elif status == "pending":
            pending_leave.append({"student_id": s.id, "name": s.name, "roll_no": s.roll_no})

    db.add_all(records)
    db.flush()  # so the just-recorded absence(s) above are visible to the percentage check below

    # In-app notifications to the parent side for absent students (no email/Firebase needed).
    # Not sent for "pending" — that's not a confirmed absence yet, just an undecided leave.
    if absent:
        date_str = local_today_start.strftime("%d %b %Y")
        notes = [
            models.Notification(
                student_id=a["student_id"],
                message=f"{a['name']} (Roll {a['roll_no']}) was marked ABSENT on {date_str}.",
            )
            for a in absent
        ]
        db.add_all(notes)

        # Low-attendance alert: a student who's been under the threshold for weeks
        # shouldn't get a fresh notification every single time they're marked absent
        # in any of their classes that day — cap it to once per
        # LOW_ATTENDANCE_ALERT_COOLDOWN_DAYS per student, using the alert's own text
        # prefix as the marker (no schema change needed for this).
        students_by_id = {s.id: s for s in all_students}
        cooldown_start = today_start - datetime.timedelta(days=LOW_ATTENDANCE_ALERT_COOLDOWN_DAYS)
        for a in absent:
            student = students_by_id[a["student_id"]]
            pct, present, total, _ = attendance_percentage(student, db)
            if total == 0 or pct >= LOW_ATTENDANCE_THRESHOLD:
                continue
            already_alerted = db.query(models.Notification).filter(
                models.Notification.student_id == a["student_id"],
                models.Notification.message.like(f"{LOW_ATTENDANCE_ALERT_PREFIX}%"),
                models.Notification.created_at >= cooldown_start,
            ).first()
            if already_alerted:
                continue
            db.add(models.Notification(
                student_id=a["student_id"],
                message=(
                    f"{LOW_ATTENDANCE_ALERT_PREFIX}{a['name']}'s (Roll {a['roll_no']}) attendance is "
                    f"currently {pct}%, below the school's {LOW_ATTENDANCE_THRESHOLD}% requirement. "
                    f"Please ensure regular attendance."
                ),
            ))

    db.commit()

    return {
        "message": "Attendance saved successfully",
        "total_present": len(req.present_student_ids),
        "total_absent": len(absent),
        "absent_count": len(absent),
        "absent_students": absent,
        "leave_count": len(on_leave),
        "leave_students": on_leave,
        "pending_count": len(pending_leave),
        "pending_students": pending_leave,
    }


# --- Teacher Analytics ------------------------------------------------------
@app.get("/teacher/analytics")
def get_teacher_analytics(
    section_id: int,
    subject: str = "All",
    db: Session = Depends(auth.get_tenant_db),
    _: models.User = Depends(auth.require_role("teacher")),
):
    return teacher_analytics(section_id, db, _, subject)


def teacher_analytics(section_id: int, db: Session, current_user, subject: str = "All"):
    allowed = (teacher_can_access_section(db, current_user.id, section_id) if subject == "All"
               else teacher_can_act_on(db, current_user.id, section_id, subject))
    if not allowed:
        raise HTTPException(status_code=403, detail="You are not assigned to this section/subject")

    students = db.query(models.Student).filter(
        models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
    ).all()
    student_ids = [s.id for s in students]

    # ONE query for every student's records (used for both per-student stats and the
    # daily calendar below) instead of one query per student.
    bulk = bulk_attendance_stats(student_ids, db, subject=subject)

    stats = []
    for s in students:
        st = bulk.get(s.id, {"percentage": 0.0, "present": 0, "absent": 0, "leave": 0,
                              "total": 0, "unique_days": 0})
        pct = st["percentage"]
        stats.append({
            "student_id": s.id,
            "name": s.name,
            "roll_no": s.roll_no,
            "percentage": pct,
            "present": st["present"],
            "absent": st["absent"],
            "leave": st["leave"],
            "total": st["total"],
            "unique_days": st["unique_days"],
            "low_attendance": pct < LOW_ATTENDANCE_THRESHOLD and st["total"] > 0,
        })
    stats.sort(key=lambda x: x["percentage"])

    # Daily aggregates for the section calendar, reusing the SAME records already fetched.
    from collections import defaultdict
    daily = defaultdict(lambda: {"total": 0, "present": 0})
    for st in bulk.values():
        for r in st["records"]:
            if r.status == "leave":
                continue                      # leave days excluded from daily grading
            date_str = r.date.strftime("%Y-%m-%d")
            daily[date_str]["total"] += 1
            if r.status == "present":
                daily[date_str]["present"] += 1
            
    daily_stats = []
    for date_str, counts in sorted(daily.items()):
        total = counts["total"]
        present = counts["present"]
        pct = round((present / total) * 100, 1) if total > 0 else 0
        daily_stats.append({
            "date": date_str,
            "total": total,
            "present": present,
            "percentage": pct
        })

    return {
        "students": stats,
        "low_attendance": [s for s in stats if s["low_attendance"]],
        "daily_stats": daily_stats
    }

import io
import csv
from fastapi.responses import StreamingResponse


def _csv_safe(value):
    """Neutralizes CSV/Excel formula injection: a name/roll_no/subject starting with
    =, +, -, or @ would otherwise execute as a formula if the exported file is opened
    in Excel/Sheets. These fields are admin-entered, so this only matters against a
    malicious or compromised admin account, but it's a one-line defensive fix."""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@"):
        return "'" + text
    return text


@app.get("/teacher/analytics/export")
def export_analytics(
    section_id: int,
    subject: str = "All",
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    stats = teacher_analytics(section_id, db, current_user, subject)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["Roll No", "Name", "Total Classes", "Classes Present", "Unique Days", "Attendance %", "Low Attendance (<75%)"])
    
    for s in stats["students"]:
        writer.writerow([
            _csv_safe(s["roll_no"]),
            _csv_safe(s["name"]),
            s["total"],
            s["present"],
            s["unique_days"],
            s["percentage"],
            "YES" if s["low_attendance"] else "NO"
        ])
        
    output.seek(0)
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=attendance_report_section_{section_id}.csv"
    return response


# --- Detailed attendance reports (date-range, per-record — not just the % summary
# above) — exportable as CSV or PDF. Available to admin and teacher alike, same
# section-scoping convention already used everywhere else in this app.
def _attendance_report_rows(db, section_id, start, end, subject, student_id=None):
    q = db.query(models.Student).filter(
        models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
    )
    if student_id is not None:
        q = q.filter(models.Student.id == student_id)
    students = q.all()
    id_to_info = {s.id: (s.roll_no, s.name) for s in students}
    student_ids = list(id_to_info.keys())

    q = db.query(models.AttendanceRecord).filter(
        models.AttendanceRecord.student_id.in_(student_ids),
        models.AttendanceRecord.date >= start,
        models.AttendanceRecord.date <= end,
    )
    if subject and subject != "All":
        q = q.filter(models.AttendanceRecord.subject == subject)
    records = q.order_by(models.AttendanceRecord.date, models.AttendanceRecord.student_id).all()

    rows = []
    for r in records:
        roll_no, name = id_to_info.get(r.student_id, ("?", "?"))
        rows.append({"date": r.date.strftime("%Y-%m-%d"), "roll_no": roll_no,
                     "name": name, "subject": r.subject, "status": r.status})
    return rows


def _parse_report_range(start_date: str, end_date: str):
    try:
        start = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        end = (datetime.datetime.strptime(end_date, "%Y-%m-%d")
               + datetime.timedelta(days=1) - datetime.timedelta(seconds=1))
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be in YYYY-MM-DD format")
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must not be before start_date")
    return start, end


def _check_report_access(db: Session, current_user: models.User, section_id: int, subject: str):
    """Admins can view any section's report in their own school — this restriction
    only applies to teachers, matching the same rule recognize/submit_attendance use."""
    if current_user.role != "teacher":
        return
    allowed = (teacher_can_access_section(db, current_user.id, section_id) if subject == "All"
               else teacher_can_act_on(db, current_user.id, section_id, subject))
    if not allowed:
        raise HTTPException(status_code=403, detail="You are not assigned to this section/subject")


@app.get("/reports/attendance.csv")
def attendance_report_csv(
    section_id: int,
    start_date: str,
    end_date: str,
    subject: str = "All",
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("admin", "teacher")),
):
    _check_report_access(db, current_user, section_id, subject)
    start, end = _parse_report_range(start_date, end_date)
    rows = _attendance_report_rows(db, section_id, start, end, subject)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Roll No", "Name", "Subject", "Status"])
    for r in rows:
        writer.writerow([r["date"], _csv_safe(r["roll_no"]), _csv_safe(r["name"]), _csv_safe(r["subject"]), r["status"]])
    output.seek(0)
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=attendance_{section_id}_{start_date}_to_{end_date}.csv")
    return response


def _pdf_safe(text: str) -> str:
    """FPDF's core Helvetica font is Latin-1 only — a name/subject containing e.g.
    Devanagari script or an emoji would otherwise raise partway through rendering,
    turning a routine report download into a 500. Un-encodable characters are replaced
    with '?' instead: the report still generates, just with a placeholder for anything
    that font genuinely can't render (embedding a full Unicode font is the complete
    fix, but needs bundling a real font file — this is the safe, dependency-free one)."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _build_attendance_pdf_bytes(rows, subtitle_line):
    from fpdf import FPDF
    counts = {"present": 0, "absent": 0, "leave": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Attendance Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, _pdf_safe(subtitle_line), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Total records: {len(rows)}  |  Present: {counts['present']}  |  "
                   f"Absent: {counts['absent']}  |  Leave: {counts['leave']}",
              new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    col_widths = [24, 36, 55, 34, 24]  # roll numbers can run 15+ chars (e.g. SATENDRA21CS2028)
    headers = ["Date", "Roll No", "Name", "Subject", "Status"]
    pdf.set_font("Helvetica", "B", 10)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 8, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for r in rows:
        pdf.cell(col_widths[0], 7, _pdf_safe(r["date"]), border=1)
        pdf.cell(col_widths[1], 7, _pdf_safe(r["roll_no"][:17]), border=1)
        pdf.cell(col_widths[2], 7, _pdf_safe(r["name"][:28]), border=1)
        pdf.cell(col_widths[3], 7, _pdf_safe(r["subject"][:16]), border=1)
        pdf.cell(col_widths[4], 7, _pdf_safe(r["status"]), border=1)
        pdf.ln()

    return bytes(pdf.output())


@app.get("/reports/attendance.pdf")
def attendance_report_pdf(
    section_id: int,
    start_date: str,
    end_date: str,
    subject: str = "All",
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("admin", "teacher")),
):
    _check_report_access(db, current_user, section_id, subject)
    start, end = _parse_report_range(start_date, end_date)
    rows = _attendance_report_rows(db, section_id, start, end, subject)
    pdf_bytes = _build_attendance_pdf_bytes(
        rows, f"Section {section_id}  |  {start_date} to {end_date}  |  Subject: {subject}")
    response = StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=attendance_{section_id}_{start_date}_to_{end_date}.pdf")
    return response


@app.get("/parent/attendance_report.csv")
def parent_attendance_report_csv(
    start_date: str,
    end_date: str,
    subject: str = "All",
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    student = resolve_parent_student(current_user, student_id, db)
    if not student:
        raise HTTPException(status_code=400, detail="No student linked to this parent")
    start, end = _parse_report_range(start_date, end_date)
    rows = _attendance_report_rows(db, student.section_id, start, end, subject, student_id=student.id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Subject", "Status"])
    for r in rows:
        writer.writerow([r["date"], _csv_safe(r["subject"]), r["status"]])
    output.seek(0)
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=attendance_{student.roll_no}_{start_date}_to_{end_date}.csv")
    return response


@app.get("/parent/attendance_report.pdf")
def parent_attendance_report_pdf(
    start_date: str,
    end_date: str,
    subject: str = "All",
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    student = resolve_parent_student(current_user, student_id, db)
    if not student:
        raise HTTPException(status_code=400, detail="No student linked to this parent")
    start, end = _parse_report_range(start_date, end_date)
    rows = _attendance_report_rows(db, student.section_id, start, end, subject, student_id=student.id)
    pdf_bytes = _build_attendance_pdf_bytes(
        rows, f"{student.name} (Roll {student.roll_no})  |  {start_date} to {end_date}  |  Subject: {subject}")
    response = StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=attendance_{student.roll_no}_{start_date}_to_{end_date}.pdf")
    return response


# --- Leave Management --------------------------------------------------------------
# Each leave request fans out into one LeaveApproval per subject taught in the
# student's section — a Hindi teacher approving a leave has no effect on the English
# teacher's own decision for the same days; see models.LeaveApproval.
def _subjects_for_section(section_id: int, db: Session) -> List[str]:
    section = db.query(models.Section).filter(models.Section.id == section_id).first()
    if not section:
        return ["General"]
    subjects = db.query(models.Subject.name).filter(models.Subject.class_id == section.class_id).all()
    names = [s.name for s in subjects]
    return names or ["General"]  # schools that haven't set up Subjects yet keep working


def _leave_overall_status(approvals: List["models.LeaveApproval"]) -> str:
    """Single-value summary for list views / legacy display: 'approved' only once every
    subject has approved it, 'rejected' only once every subject has rejected it,
    'pending' for anything in between (including a mix of approved+rejected)."""
    if not approvals:
        return "pending"
    statuses = {a.status for a in approvals}
    if statuses == {"approved"}:
        return "approved"
    if statuses == {"rejected"}:
        return "rejected"
    return "pending"


class LeaveRequestCreate(BaseModel):
    start_date: str
    end_date: str
    reason: str
    student_id: Optional[int] = None  # which child — defaults to the primary/legacy one

@app.post("/parent/leave")
def create_leave_request(
    req: LeaveRequestCreate,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    student = resolve_parent_student(current_user, req.student_id, db)
    if not student:
        raise HTTPException(status_code=400, detail="No student linked to this parent")

    try:
        start_date = datetime.datetime.fromisoformat(req.start_date)
        end_date = datetime.datetime.fromisoformat(req.end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start or end date")

    # Guards against an accidental double-submit (double-click, slow connection retry,
    # browser back-button resubmit) silently creating two identical leave requests —
    # this exact combination already happened in testing before this check existed.
    if db.query(models.LeaveRequest).filter(
        models.LeaveRequest.student_id == student.id,
        models.LeaveRequest.start_date == start_date,
        models.LeaveRequest.end_date == end_date,
        models.LeaveRequest.reason == req.reason,
    ).first():
        raise HTTPException(
            status_code=400,
            detail="A leave request for these exact dates and reason has already been submitted",
        )

    leave = models.LeaveRequest(
        student_id=student.id,
        start_date=start_date,
        end_date=end_date,
        reason=req.reason,
        status="pending",
    )
    db.add(leave)
    try:
        db.flush()  # need leave.id before creating its per-subject approvals
    except IntegrityError:
        # The SELECT check above already covers this in the common case — this only
        # fires for a genuine concurrent double-submit that raced past it, caught here
        # by the DB-level unique constraint instead of surfacing a raw 500.
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="A leave request for these exact dates and reason has already been submitted",
        )

    for subject in _subjects_for_section(student.section_id, db):
        db.add(models.LeaveApproval(leave_request_id=leave.id, subject=subject, status="pending"))

    db.commit()
    return {"message": "Leave request submitted successfully"}

@app.get("/parent/leave")
def get_parent_leaves(
    student_id: Optional[int] = None,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("parent")),
):
    student = resolve_parent_student(current_user, student_id, db)
    if not student:
        raise HTTPException(status_code=400, detail="No student linked to this parent")
    leaves = db.query(models.LeaveRequest).filter(models.LeaveRequest.student_id == student.id).order_by(models.LeaveRequest.start_date.desc()).all()
    return [
        {
            "id": l.id,
            "start_date": l.start_date.strftime("%Y-%m-%d"),
            "end_date": l.end_date.strftime("%Y-%m-%d"),
            "reason": l.reason,
            "status": _leave_overall_status(l.approvals),
            "approvals": [{"subject": a.subject, "status": a.status} for a in l.approvals],
        } for l in leaves
    ]

@app.get("/teacher/leave/pending_count")
def get_pending_leave_count(
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Count of per-subject leave approvals awaiting a decision that THIS teacher may
    act on — either they're assigned to that (section, subject) pair, or nobody is
    (open to any teacher, unchanged legacy behavior for schools without assignments)."""
    rows = db.query(models.LeaveApproval, models.Student.section_id).join(
        models.LeaveRequest, models.LeaveApproval.leave_request_id == models.LeaveRequest.id
    ).join(
        models.Student, models.LeaveRequest.student_id == models.Student.id
    ).filter(models.LeaveApproval.status == "pending").all()
    count = sum(1 for approval, section_id in rows
                if teacher_can_act_on(db, current_user.id, section_id, approval.subject))
    return {"pending_count": count}


@app.get("/teacher/leave/pending")
def get_all_pending_leaves(
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Every pending per-subject leave approval this teacher may act on, newest first —
    feeds the teacher notification drawer so it doesn't need a section picked first."""
    rows = db.query(models.LeaveApproval, models.LeaveRequest, models.Student).join(
        models.LeaveRequest, models.LeaveApproval.leave_request_id == models.LeaveRequest.id
    ).join(
        models.Student, models.LeaveRequest.student_id == models.Student.id
    ).filter(models.LeaveApproval.status == "pending").order_by(models.LeaveRequest.start_date.desc()).all()
    visible = [(a, l, s) for a, l, s in rows if teacher_can_act_on(db, current_user.id, s.section_id, a.subject)]
    return {"leaves": [
        {
            "id": l.id,
            "subject": a.subject,
            "student_name": s.name,
            "roll_no": s.roll_no,
            "start_date": l.start_date.strftime("%Y-%m-%d"),
            "end_date": l.end_date.strftime("%Y-%m-%d"),
            "reason": l.reason,
        } for a, l, s in visible
    ]}


@app.get("/teacher/leave")
def get_section_leaves(
    section_id: int,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Every per-subject leave approval for the section that THIS teacher may act on —
    one row per (leave request, subject), so a Hindi-only teacher never even sees the
    English approval row for the same leave request. Pending ones first, then most
    recent. Rows for a subject this teacher isn't assigned to (when someone else IS
    assigned to it) are left out entirely, not just hidden from action."""
    students = db.query(models.Student).filter(
        models.Student.section_id == section_id, models.Student.deleted_at.is_(None)
    ).all()
    student_ids = [s.id for s in students]
    if not student_ids:
        return []
    students_by_id = {s.id: s for s in students}
    rows = db.query(models.LeaveApproval, models.LeaveRequest).join(
        models.LeaveRequest, models.LeaveApproval.leave_request_id == models.LeaveRequest.id
    ).filter(models.LeaveRequest.student_id.in_(student_ids)).all()
    visible = [(a, l) for a, l in rows if teacher_can_act_on(db, current_user.id, section_id, a.subject)]
    visible.sort(key=lambda pair: (pair[0].status != "pending", -pair[1].start_date.toordinal()))
    return [
        {
            "id": l.id,
            "subject": a.subject,
            "student_name": students_by_id[l.student_id].name,
            "roll_no": students_by_id[l.student_id].roll_no,
            "start_date": l.start_date.strftime("%Y-%m-%d"),
            "end_date": l.end_date.strftime("%Y-%m-%d"),
            "reason": l.reason,
            "status": a.status,
        } for a, l in visible
    ]

class LeaveStatusUpdate(BaseModel):
    subject: str
    status: str

@app.post("/teacher/leave/{leave_id}/status")
def update_leave_status(
    leave_id: int,
    req: LeaveStatusUpdate,
    db: Session = Depends(auth.get_tenant_db),
    current_user: models.User = Depends(auth.require_role("teacher")),
):
    """Decides ONE subject's approval for this leave request — not the whole request.
    A Hindi teacher approving does not touch the English (or any other subject's)
    approval row for the same leave request; each subject's teacher decides on their
    own, independently."""
    leave = db.query(models.LeaveRequest).filter(models.LeaveRequest.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if req.status not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    approval = db.query(models.LeaveApproval).filter(
        models.LeaveApproval.leave_request_id == leave_id,
        models.LeaveApproval.subject == req.subject,
    ).first()
    if not approval:
        raise HTTPException(status_code=404, detail=f"No {req.subject} approval record on this leave request")

    if not teacher_can_act_on(db, current_user.id, leave.student.section_id, req.subject):
        raise HTTPException(status_code=403, detail=f"You are not assigned to teach {req.subject} for this student's section")

    approval.status = req.status
    approval.decided_by = current_user.id
    approval.decided_at = datetime.datetime.utcnow()

    # Retroactively reflect the decision on already-recorded days in the leave window,
    # for THIS SUBJECT ONLY — a missed Hindi class becomes excused "leave" because the
    # Hindi teacher approved it; that has no effect on English attendance for the same days.
    day_start = datetime.datetime.combine(leave.start_date.date(), datetime.time.min)
    day_end = datetime.datetime.combine(leave.end_date.date(), datetime.time.max)
    affected = db.query(models.AttendanceRecord).filter(
        models.AttendanceRecord.student_id == leave.student_id,
        models.AttendanceRecord.subject == req.subject,
        models.AttendanceRecord.date >= day_start,
        models.AttendanceRecord.date <= day_end,
    ).all()

    # Per-class: a MISSED class in the leave window is excused as "leave"; classes the
    # student actually attended stay "present". (Approve: absent/pending -> leave;
    # reject: pending -> absent, and undo a previous approval back to absent too.)
    converted = 0
    for rec in affected:
        if req.status == "approved" and rec.status in ("absent", "pending"):
            rec.status = "leave"          # missed class becomes excused leave
            converted += 1
        elif req.status == "rejected" and rec.status in ("leave", "pending"):
            rec.status = "absent"         # undo if the leave is rejected
            converted += 1

    # Notify the parent in-app instead of email — scoped to this one subject, since
    # this decision doesn't mean the whole leave request is resolved.
    db.add(models.Notification(
        student_id=leave.student_id,
        message=(
            f"{req.subject} leave request for {leave.student.name} "
            f"({leave.start_date.strftime('%d %b')} – {leave.end_date.strftime('%d %b')}) "
            f"has been {req.status}."
        ),
    ))
    db.commit()

    return {"message": f"{req.subject} leave {req.status}", "days_updated": converted}


# --- Super Admin & School Onboarding -----------------------------------------
# Entirely separate from tenant auth above: a super admin manages onboarding only, never
# touches student/attendance data, and isn't a row in any school's `users` table.

def require_super_admin(
    token: str = Depends(auth.oauth2_scheme),
    db: Session = Depends(database.get_db),
) -> models.SuperAdminUser:
    payload = auth.decode_token(token)
    if not payload or not payload.get("super_admin"):
        raise HTTPException(status_code=403, detail="Super admin access required")
    sa = db.query(models.SuperAdminUser).filter(models.SuperAdminUser.username == payload.get("sub")).first()
    if not sa:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    return sa


@app.post("/superadmin/token")
async def super_admin_login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(database.get_db),
):
    return await _throttled_login(_super_admin_login_impl, form_data, db)


def _super_admin_login_impl(
    form_data: OAuth2PasswordRequestForm,
    db: Session,
):
    # Namespaced ("superadmin:" prefix) so this lockout bucket never overlaps with a
    # tenant username that happens to match — this is the single most powerful account
    # in the system, so it gets the same brute-force protection as everyone else.
    lockout_key = "superadmin:" + form_data.username
    auth.check_login_lockout(lockout_key)
    sa = db.query(models.SuperAdminUser).filter(models.SuperAdminUser.username == form_data.username).first()
    if not sa or not auth.verify_password(form_data.password, sa.hashed_password):
        auth.record_login_failure(lockout_key)
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    auth.clear_login_failures(lockout_key)
    # A successful login means the bootstrap file (if any) has served its purpose —
    # either this login used it (so it's now been retrieved) or the password was
    # already changed since (so it's stale either way). Removing it bounds how long the
    # one-time plaintext password sits on disk to "until first login", not indefinitely.
    bootstrap_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap_superadmin_password.txt")
    if os.path.exists(bootstrap_file):
        try:
            os.remove(bootstrap_file)
        except OSError:
            pass
    token = auth.create_access_token(
        {"sub": sa.username, "super_admin": True},
        expires_delta=datetime.timedelta(minutes=60 * 24),
    )
    return {"access_token": token, "token_type": "bearer"}


class CreateOnboardingLinkRequest(BaseModel):
    school_name: str
    contact_email: Optional[str] = None


@app.post("/superadmin/onboarding_links")
def create_onboarding_link(
    req: CreateOnboardingLinkRequest,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    name = req.school_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="School name is required")
    private_pem, public_key = provisioning.generate_deploy_keypair()
    token = secrets.token_urlsafe(24)
    school = models.School(
        name=name,
        contact_email=req.contact_email,
        onboarding_token=token,
        status="invited",
        deploy_public_key=public_key,
        deploy_private_key_encrypted=provisioning.encrypt_secret(private_pem),
    )
    db.add(school)
    db.commit()
    db.refresh(school)
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    onboarding_url = f"{base}/onboard.html?token={token}" if base else f"/onboard.html?token={token}"
    return {"id": school.id, "token": token, "onboarding_url": onboarding_url}


@app.get("/superadmin/onboarding_requests")
def list_onboarding_requests(
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    schools = db.query(models.School).order_by(models.School.created_at.desc()).all()
    return {"schools": [
        {
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "contact_email": s.contact_email,
            "elastic_ip": s.elastic_ip,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "rejection_reason": s.rejection_reason,
            "provisioning_error": s.provisioning_error,
            "service_stopped": s.service_stopped,
            "admin_username": s.admin_username,
        } for s in schools
    ]}


@app.post("/superadmin/onboarding_requests/{school_id}/accept")
def accept_onboarding_request(
    school_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    # Atomic status transition (UPDATE ... WHERE status='submitted', not read-then-write)
    # — closes a race where two near-simultaneous accept calls (double-click, retried
    # request) could both pass a plain status check and spawn two competing
    # provision_school runs for the same school, each generating its own model API key
    # and potentially desyncing the stored key from whichever SSH setup finishes last.
    updated = db.query(models.School).filter(
        models.School.id == school_id, models.School.status == "submitted"
    ).update({"status": "provisioning"})
    db.commit()
    if not updated:
        db.refresh(school)
        raise HTTPException(status_code=400, detail=f"Cannot accept a request in status '{school.status}'")
    background_tasks.add_task(provisioning.provision_school, school.id)
    return {"message": "Provisioning started — this takes a few minutes"}


@app.post("/superadmin/onboarding_requests/{school_id}/retry")
def retry_onboarding_request(
    school_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    """Re-runs provisioning for a failed request using the same IP/Supabase URL already
    on file — for when the actual fix was on the school's server (e.g. the key finally
    got added correctly), not the submitted details, so there's nothing to resubmit."""
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    # Same atomic-transition reasoning as accept_onboarding_request above.
    updated = db.query(models.School).filter(
        models.School.id == school_id, models.School.status == "failed"
    ).update({"status": "provisioning", "provisioning_error": None})
    db.commit()
    if not updated:
        db.refresh(school)
        raise HTTPException(status_code=400, detail=f"Can only retry a request in status 'failed' (this one is '{school.status}')")
    background_tasks.add_task(provisioning.provision_school, school.id)
    return {"message": "Retrying — this takes a few minutes"}


@app.get("/superadmin/onboarding_requests/{school_id}/public_key")
def get_school_public_key(
    school_id: int,
    _: models.SuperAdminUser = Depends(require_super_admin),
    db: Session = Depends(database.get_db),
):
    """Lets you re-check exactly which key a given request expects, without needing the
    school's own onboarding link/token handy — safe to expose since it's the public half."""
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    return {"public_key": school.deploy_public_key}


class RejectOnboardingRequest(BaseModel):
    reason: str


@app.post("/superadmin/onboarding_requests/{school_id}/reject")
def reject_onboarding_request(
    school_id: int,
    req: RejectOnboardingRequest,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    school.status = "rejected"
    school.rejection_reason = req.reason
    db.commit()
    return {"message": "Request rejected"}


@app.delete("/superadmin/onboarding_requests/{school_id}")
def delete_onboarding_request(
    school_id: int,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    """Removes this school from the onboarding list. Only deletes OUR record of it —
    their actual Supabase database and EC2 server (if any) are untouched, since those
    belong to the school, not us. For an 'active' school this does mean their users can
    no longer log in afterward (their school_id won't resolve), so the frontend warns
    clearly before calling this for anything other than an abandoned/failed attempt."""
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(school)
    db.commit()
    return {"message": "Deleted"}


@app.post("/superadmin/onboarding_requests/{school_id}/reset_admin_password")
def reset_school_admin_password(
    school_id: int,
    db: Session = Depends(database.get_db),
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    """Generates a fresh password for this school's admin login — for when they're
    locked out and need help, without anyone SSHing into a server to fix it by hand."""
    school = db.query(models.School).filter(models.School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Not found")
    if school.status != "active" or not school.admin_username:
        raise HTTPException(status_code=400, detail="This school has no admin account yet")

    try:
        supabase_url = provisioning.decrypt_secret(school.supabase_db_url_encrypted)
        TenantSession = database.get_tenant_sessionmaker(supabase_url)
        tsession = TenantSession()
    except Exception:
        raise HTTPException(status_code=503, detail="This school's database is currently unreachable")

    try:
        admin_user = tsession.query(models.User).filter(models.User.username == school.admin_username).first()
        if not admin_user:
            raise HTTPException(status_code=404, detail="Admin account not found in the school's database")
        new_password = auth.generate_compliant_password()
        admin_user.hashed_password = auth.get_password_hash(new_password)
        admin_user.token_version = (admin_user.token_version or 0) + 1
        tsession.commit()
    finally:
        tsession.close()

    return {"admin_username": school.admin_username, "new_password": new_password}


@app.post("/superadmin/onboarding_requests/{school_id}/stop_service")
def stop_school_service(
    school_id: int,
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    try:
        provisioning.set_service_running(school_id, running=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not stop the service: {e}")
    return {"message": "Service stopped"}


@app.post("/superadmin/onboarding_requests/{school_id}/start_service")
def start_school_service(
    school_id: int,
    _: models.SuperAdminUser = Depends(require_super_admin),
):
    try:
        provisioning.set_service_running(school_id, running=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not start the service: {e}")
    return {"message": "Service started"}


# --- Public onboarding page endpoints ------------------------------------------------
# Unauthenticated: the token itself IS the credential (long, random, sent only to the
# invited school). Never logged, never guessable, never enumerable (404 either way).

@app.get("/public/onboarding/{token}")
def get_onboarding_status(token: str, db: Session = Depends(database.get_db)):
    school = db.query(models.School).filter(models.School.onboarding_token == token).first()
    if not school:
        raise HTTPException(status_code=404, detail="Invalid onboarding link")
    resp = {"status": school.status, "school_name": school.name, "public_key": school.deploy_public_key}
    if school.status == "rejected":
        resp["reason"] = school.rejection_reason
    if school.status == "failed":
        resp["error"] = ("Setup failed — this is usually because the public key above wasn't "
                          "added to the instance, or the wrong IP/connection string was entered. "
                          "Double-check both, then resubmit below.")
    if school.status == "active":
        resp["admin_username"] = school.admin_username
        if school.admin_password_plaintext:
            # Shown once: clear immediately after this response is built, same convention
            # as every other generated credential in this app.
            resp["admin_password"] = school.admin_password_plaintext
            school.admin_password_plaintext = None
            db.commit()
        login_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        resp["login_url"] = f"{login_base}/" if login_base else "/"
    return resp


@app.get("/public/onboarding/{token}/public_key.pub")
def download_onboarding_public_key(token: str, db: Session = Depends(database.get_db)):
    """Serves the school's public key as a downloadable file — for AWS's 'Import Key
    Pair' flow when launching a new instance, so the school never has to open a
    terminal at all (see Option A on the onboarding page)."""
    school = db.query(models.School).filter(models.School.onboarding_token == token).first()
    if not school:
        raise HTTPException(status_code=404, detail="Invalid onboarding link")
    # Filename includes the school id so re-downloading for a different onboarding
    # attempt never silently collides with (or gets confused for) an earlier download
    # sitting in the same Downloads folder — each attempt's key file is visibly distinct.
    return Response(
        content=school.deploy_public_key + "\n",
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=smart-attendance-key-{school.id}.pub"},
    )


class SubmitOnboardingRequest(BaseModel):
    supabase_db_url: str
    elastic_ip: str
    pubkey_confirmed: bool


@app.post("/public/onboarding/{token}/submit")
def submit_onboarding(token: str, req: SubmitOnboardingRequest, db: Session = Depends(database.get_db)):
    school = db.query(models.School).filter(models.School.onboarding_token == token).first()
    if not school:
        raise HTTPException(status_code=404, detail="Invalid onboarding link")
    if school.status not in ("invited", "rejected", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"This link is no longer accepting submissions (status: {school.status})",
        )
    if not req.pubkey_confirmed:
        raise HTTPException(status_code=400, detail="Please confirm you've added the public key to your instance first")
    if not req.supabase_db_url.strip() or not req.elastic_ip.strip():
        raise HTTPException(status_code=400, detail="Supabase DB URL and Elastic IP are required")
    try:
        ip_obj = ipaddress.ip_address(req.elastic_ip.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Elastic IP must be a valid IP address")
    # Must be a real, public address the central server can reach over the internet —
    # never one that resolves to this server's own internal network or cloud metadata
    # endpoint (e.g. 169.254.169.254), which would otherwise let a malicious/careless
    # submission make this server send real photo data to an internal-only target (SSRF).
    if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
            or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
        raise HTTPException(
            status_code=400,
            detail="Elastic IP must be a public IP address, not a private/internal/reserved one",
        )

    school.supabase_db_url_encrypted = provisioning.encrypt_secret(req.supabase_db_url.strip())
    school.elastic_ip = req.elastic_ip.strip()
    school.status = "submitted"
    school.rejection_reason = None
    school.provisioning_error = None
    db.commit()
    return {"message": "Submitted — awaiting approval"}


# Static web frontend (mounted last so API routes take precedence)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
