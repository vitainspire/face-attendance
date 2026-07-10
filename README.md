# Smart Attendance — Multi-Tenant Face Recognition Attendance System

A multi-tenant school attendance platform: one shared backend serves every onboarded
school, each school owns its own database and (optionally) its own S3 bucket, and each
school runs its own face-recognition model service on its own server.

## Features

- **Photo-based attendance** — a teacher uploads one group photo; faces are matched
  against each enrolled student's embeddings (cosine + KNN) and pre-checked on a
  manual-review checklist. Uploads are queued and streamed to disk (never held
  in memory), so a burst of concurrent uploads degrades gracefully instead of
  crashing the server.
- **Teacher subject/section assignments** — admin assigns a teacher to a specific
  class + section + subject. Once any assignment exists for a pair, only the
  assigned teacher(s) may take attendance, view that section's roster/reports, or
  decide leave requests for it. Sections/subjects with no assignment stay open to
  any teacher (so a school doesn't have to adopt this on day one).
- **Per-subject leave approval** — a single leave request fans out into one
  decision per subject taught in the student's section. A Hindi teacher approving
  a leave has no effect on the English teacher's own decision for the same days;
  attendance reflects "leave" only for the subject(s) actually approved.
- **Automatic low-attendance alerts** — when a student's overall attendance drops
  below 75%, their parent gets a notification automatically (throttled to at most
  once a week per student, so it doesn't spam every subsequent absence).
- **Events/announcements** — admin posts school-wide events; parents see upcoming
  ones on their dashboard.
- **Reports** — CSV/PDF attendance exports (per-section for teachers/admins,
  per-child for parents), with CSV formula-injection characters neutralized.
- **Auth & abuse protection** — per-account login lockout (scoped per school, so a
  same-named account in a different school is never affected), a login queue that
  throttles concurrent password checks to match real CPU capacity instead of
  instantly rejecting the 4th+ simultaneous login, and constant-time handling for
  unknown usernames to avoid a timing side-channel.
- **School onboarding** — a superadmin invites a school via a one-time link; the
  school submits their own EC2 IP + Supabase URL; the platform SSHes in and
  deploys their model-service automatically.

## Architecture

Two independently deployable pieces:

| Piece | Runs on | Contains |
|---|---|---|
| **`cloud-instance/`** | Your own central server (one instance, shared by every school) | Backend API, frontend, and the cosine/KNN matching service |
| **`model-service/`** | Each school's **own** server, deployed automatically during onboarding | Face detection + embedding generation only |

Each school's actual data (students, attendance, embeddings, logins) lives in **that
school's own Supabase Postgres database** — never on the central server. A small
control-plane table on the central server just tracks which school maps to which
(encrypted) database connection and model-service address.

```
Browser
  │  HTTPS
  ▼
cloud-instance (webapp.service : 8000)  ──┬── looks up the logged-in user's school
  │                                        └── calls that school's model-service over HTTP
  ├── matcher.service (127.0.0.1:8001)     — cosine + KNN, stateless, no DB access
  └── Supabase (per-school Postgres)       — one DB per school, resolved per request

Each school's own server
  └── model-service (local_model_service.py) — face detection + embedding only
```

## Folder structure

```
cloud-instance/
├── main.py                  FastAPI app — every HTTP endpoint, serves static/ too
├── models.py                SQLAlchemy ORM models (shared schema, one DB per school)
├── database.py               DB session/connection helpers (control-plane + per-tenant)
├── auth.py                   JWT auth, password hashing, role checks, login lockout
├── provisioning.py           School onboarding pipeline (SSH-deploys model-service to
│                             a new school's server, bootstraps their database)
├── s3_photos.py               Per-school S3 photo storage (falls back to DB blob if unset)
├── model_client.py            Talks to whichever school's model-service the request needs
├── thresholds.py              Auto-computed per-section/per-student match cutoffs
├── matcher_api.py             Cosine + KNN matching microservice (matcher.service)
├── requirements.txt
├── static/                    The actual website (vanilla HTML/CSS/JS, no build step)
│   ├── index.html             Admin + teacher + parent app (single page, role-routed)
│   ├── app.js
│   ├── styles.css
│   ├── superadmin.html        Operator console — approve/manage schools
│   ├── superadmin.js
│   ├── onboard.html           Public page a new school fills in during onboarding
│   └── onboard.js
├── scripts/                   Reusable admin/ops utilities, run manually when needed
│   ├── bulk_enroll_students.py  Bulk-add student face embeddings from a school's own
│   │                             S3 bucket or a local photo folder — for onboarding a
│   │                             whole class at once instead of one parent capture at
│   │                             a time. Fully configured via env vars (no hardcoded
│   │                             server/IP/path — safe to reuse for any school).
│   └── analyze_thresholds.py    Diagnostic: reports genuine-vs-impostor score
│                                 separation per section, to sanity-check recognition
│                                 accuracy for a given engine.
└── ops/                       Deployment tooling for the central server
    ├── systemd/                Unit files for all 5 services (see below)
    ├── backup_databases.py     Nightly pg_dump of every active school's DB
    ├── check_health.py         5-minute health check + email alert on failure
    └── cftunnel_wrapper.sh     Wraps cloudflared so a tunnel restart auto-updates
                                 PUBLIC_BASE_URL and restarts the app — no manual step

model-service/
├── local_model_service.py     FastAPI wrapper exposing /detect_embed, /embed_largest
├── engines.py                  Face detection + recognition engines (dlib, SFace, etc.)
├── requirements.txt            Heavier deps (opencv, onnxruntime, dlib) — never installed
│                                on the cloud instance
└── models_commercial/          ONNX model weights (detector + recognizer)
```

## Services on the central server (systemd)

| Service | Purpose | Port |
|---|---|---|
| `webapp.service` | FastAPI backend + static frontend | 8000 (public, via tunnel) |
| `matcher.service` | Cosine + KNN matching | 8001 (localhost only) |
| `cftunnel.service` | Cloudflare tunnel — public HTTPS URL | — |
| `db-backup.timer` → `db-backup.service` | Nightly backup of every school's DB | — |
| `health-check.timer` → `health-check.service` | Health check every 5 min, emails on failure | — |

## Required environment variables (`/etc/webapp.env` on the central server)

```
DATABASE_URL                 = postgresql://... (control-plane DB)
JWT_SECRET_KEY                = <random secret — auth breaks without a real one>
CONTROL_PLANE_ENCRYPTION_KEY  = <Fernet key — encrypts every school's DB URL/SSH key/S3 creds>
MATCHER_URL                   = http://localhost:8001/match
MATCHER_API_KEY                = <shared secret between webapp and matcher>
PUBLIC_BASE_URL                = https://your-current-public-url
                                  (kept in sync automatically by cftunnel_wrapper.sh)
ALERT_EMAIL_FROM / ALERT_EMAIL_APP_PASSWORD / ALERT_EMAIL_TO   (optional, for health-check alerts)
```

Each school's own Supabase URL, SSH deploy key, S3 credentials, and model-service API
key are generated per-school during onboarding and stored **encrypted** in the
control-plane database — never as plaintext env vars.

## Database setup

No manual migration step, no Alembic — `models.Base.metadata.create_all(...)` runs on
every app startup and on every new school's first provisioning, creating any tables
that don't exist yet. Point `DATABASE_URL` at an empty Postgres database (or SQLite
for local dev, the default if `DATABASE_URL` isn't set) and start the app; every table
is created automatically the first time it runs. The one-time `SUPERADMIN_USERNAME` /
`SUPERADMIN_PASSWORD` env vars (optional — a random password is generated and logged
if omitted) bootstrap the first superadmin account the same way.

## Deploying a change

```bash
KEY=/path/to/your.pem
HOST=ec2-user@<your-instance-ip>

scp -i $KEY cloud-instance/*.py                 $HOST:~/app/
scp -i $KEY -r cloud-instance/static/*          $HOST:~/app/static/
ssh -i $KEY $HOST "sudo systemctl restart webapp.service matcher.service"
```

A new school's `model-service/` is deployed automatically by `provisioning.py` — you
never scp it by hand.
