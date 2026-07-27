import os
import threading
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# DB location is env-driven:
#   - laptop  : default SQLite (sql_app.db)
#   - instance: DATABASE_URL=postgresql://postgres:@localhost:5432/postgres  (tunnelled to laptop)
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./sql_app.db")

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # NOTE: pool_pre_ping=True would ping the DB on every single checkout — i.e. an
    # extra network round trip over the tunnel on EVERY request (get_current_user alone
    # runs on nearly every endpoint). Over a high-latency link that tax is significant,
    # so we skip it and just recycle idle connections periodically instead.
    # connect_timeout bounds how long a genuinely unreachable DB can hang a request —
    # without it, an outage on the other end blocks for the OS's own default (minutes).
    engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_recycle=1800, connect_args={"connect_timeout": 15})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """The control-plane/default-tenant database — used by /token (to search across
    schools), the /superadmin and /public/onboarding routes, and background jobs. Every
    other route uses auth.get_tenant_db instead, which resolves to whichever school the
    logged-in user actually belongs to."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Per-school engines, cached by Supabase connection string so repeated requests to the
# same tenant reuse a connection pool instead of opening a fresh one every time. Left
# unbounded deliberately — the number of onboarded schools is realistically in the
# dozens/hundreds, not large enough to matter, and evicting a still-active tenant's
# engine would just force it to reconnect on its next request for no real benefit.
_tenant_sessionmakers = {}
_tenant_sessionmakers_lock = threading.Lock()


def get_tenant_sessionmaker(supabase_url: str):
    # Double-checked locking: the lock is only needed for the (rare) first request for a
    # given tenant — without it, two concurrent first-requests could each construct and
    # cache their own separate engine/connection pool for the same URL.
    if supabase_url not in _tenant_sessionmakers:
        with _tenant_sessionmakers_lock:
            if supabase_url not in _tenant_sessionmakers:
                tenant_engine = create_engine(
                    supabase_url, pool_pre_ping=True, pool_recycle=1800,
                    connect_args={"connect_timeout": 15},
                )
                _tenant_sessionmakers[supabase_url] = sessionmaker(autocommit=False, autoflush=False, bind=tenant_engine)
    return _tenant_sessionmakers[supabase_url]
