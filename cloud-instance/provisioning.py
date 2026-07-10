"""School onboarding: secret encryption, per-school SSH deploy keypairs, and the
provisioning pipeline that turns a submitted onboarding request into a live tenant.

This module is entirely additive — nothing in main.py's existing routes, auth, or
database access depends on it. It's only reached via the new /superadmin and
/public/onboarding routes.
"""
import io
import os
import secrets
import time
import traceback

import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import auth
import database
import models

DEPLOY_SSH_USER = os.environ.get("SCHOOL_SSH_USER", "ec2-user")
MODEL_SERVICE_PORT = 9100
BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_service_bundle")
REMOTE_APP_DIR = "/home/ec2-user/model_service"


# --- Secret encryption (Fernet, key held only in this server's env — never in the DB) ---
def _fernet() -> Fernet:
    key = os.environ.get("CONTROL_PLANE_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "CONTROL_PLANE_ENCRYPTION_KEY is not set — generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and put it in the server's env file. Refusing to handle secrets without it."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# --- Per-school deploy keypair --------------------------------------------------------
def generate_deploy_keypair():
    """Returns (private_pem: str, public_openssh: str). We keep the private half
    (encrypted); the school only ever sees the public half, which they add to their own
    instance's authorized_keys — we never receive or hold their master key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_openssh = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8") + " smart-attendance-deploy"
    return private_pem, public_openssh


# --- Remote setup script (runs on the school's EC2 instance) --------------------------
def _setup_script(model_key: str) -> str:
    return f"""set -e
sudo mkdir -p {REMOTE_APP_DIR}
sudo chown {DEPLOY_SSH_USER}:{DEPLOY_SSH_USER} {REMOTE_APP_DIR}
cd {REMOTE_APP_DIR}
if ! command -v python3 &>/dev/null; then
    echo "python3 not found on this instance" >&2
    exit 1
fi
rm -rf venv
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cat > model-service.env <<EOF
LOCAL_MODEL_KEY={model_key}
RECOGNITION_ENGINE=sface
EOF

sudo tee /etc/systemd/system/model-service.service > /dev/null <<'UNIT'
[Unit]
Description=Smart Attendance Model Service
After=network.target

[Service]
Type=simple
User={DEPLOY_SSH_USER}
WorkingDirectory={REMOTE_APP_DIR}
EnvironmentFile={REMOTE_APP_DIR}/model-service.env
ExecStart={REMOTE_APP_DIR}/venv/bin/python -m uvicorn local_model_service:app --host 0.0.0.0 --port {MODEL_SERVICE_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable model-service.service
sudo systemctl restart model-service.service
sleep 3
systemctl is-active model-service.service
"""


def _upload_bundle(sftp, local_dir, remote_dir):
    """Recursively mirrors local_dir onto remote_dir over the given SFTP client,
    creating remote subdirectories as needed."""
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass  # already exists
    for entry in os.listdir(local_dir):
        local_path = os.path.join(local_dir, entry)
        remote_path = f"{remote_dir}/{entry}"
        if os.path.isdir(local_path):
            _upload_bundle(sftp, local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)


def _wait_for_health(elastic_ip: str, model_key: str, timeout_s: int = 120) -> bool:
    """/health now requires the API key too (it used to be open — this port is reachable
    from the whole internet, not just from us, so an unauthenticated /health was a free
    fingerprinting endpoint for anyone scanning for it)."""
    deadline = time.time() + timeout_s
    url = f"http://{elastic_ip}:{MODEL_SERVICE_PORT}/health"
    while time.time() < deadline:
        try:
            r = requests.get(url, headers={"x-api-key": model_key}, timeout=5)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


def provision_school(school_id: int):
    """The full pipeline: bootstrap the school's Supabase schema, deploy the model
    service to their EC2 instance over SSH, create their first admin account. Meant to
    run as a background task — takes several minutes (pip installing opencv/onnxruntime/
    insightface on their instance is the slow part). Never raises; failure is recorded
    on the School row instead, since nothing is awaiting this call directly."""
    db = database.SessionLocal()
    try:
        school = db.query(models.School).filter(models.School.id == school_id).first()
        if not school:
            return
        school.status = "provisioning"
        db.commit()

        supabase_url = decrypt_secret(school.supabase_db_url_encrypted)
        private_pem = decrypt_secret(school.deploy_private_key_encrypted)

        # 1. Bootstrap schema on the school's own Supabase DB — same tables, fresh DB.
        tenant_engine = create_engine(supabase_url, pool_pre_ping=True)
        models.Base.metadata.create_all(bind=tenant_engine)

        # 2. SSH in with OUR generated key (school only ever saw the public half) and
        #    deploy the model service.
        import paramiko
        pkey = paramiko.RSAKey.from_private_key(io.StringIO(private_pem))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=school.elastic_ip, username=DEPLOY_SSH_USER, pkey=pkey, timeout=30)

        try:
            sftp = client.open_sftp()
            _upload_bundle(sftp, BUNDLE_DIR, REMOTE_APP_DIR)
            sftp.close()

            model_api_key = secrets.token_urlsafe(32)
            _, stdout, stderr = client.exec_command(_setup_script(model_api_key), timeout=900)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", "ignore")
            err = stderr.read().decode("utf-8", "ignore")
            if exit_code != 0:
                raise RuntimeError(f"Remote setup failed (exit {exit_code}):\n{out}\n{err}")
        finally:
            client.close()

        # 3. Confirm the service actually came up before declaring success.
        if not _wait_for_health(school.elastic_ip, model_api_key):
            raise RuntimeError("Model service did not respond healthy after setup")

        # 4. First admin account, created directly in the school's own tenant DB.
        # Username includes the school's id to guarantee it's globally unique — login
        # checks the default/original school first (see main.py's /token), so a plain
        # "admin" here would always be shadowed by that school's own "admin" account.
        admin_username = f"admin_{school.id}"
        TenantSession = sessionmaker(bind=tenant_engine)
        tsession = TenantSession()
        try:
            admin_password = auth.generate_compliant_password()
            tsession.add(models.User(
                username=admin_username,
                hashed_password=auth.get_password_hash(admin_password),
                role="admin",
            ))
            tsession.commit()
        finally:
            tsession.close()

        school.model_service_api_key_encrypted = encrypt_secret(model_api_key)
        school.admin_username = admin_username
        school.admin_password_plaintext = admin_password
        school.status = "active"
        import datetime
        school.approved_at = datetime.datetime.utcnow()
        school.provisioning_error = None
        db.commit()

    except Exception:
        db.rollback()
        school = db.query(models.School).filter(models.School.id == school_id).first()
        if school:
            school.status = "failed"
            school.provisioning_error = traceback.format_exc()[-2000:]
            db.commit()
    finally:
        db.close()


def set_service_running(school_id: int, running: bool):
    """Starts or stops a provisioned school's model service — used to suspend/resume a
    school (e.g. non-payment) without touching their data. Runs synchronously (a
    systemctl start/stop takes seconds, not minutes like provision_school) and raises on
    failure so the calling endpoint can report it immediately, instead of the
    record-the-error-on-the-row pattern provision_school uses for its background job."""
    import paramiko

    db = database.SessionLocal()
    try:
        school = db.query(models.School).filter(models.School.id == school_id).first()
        if not school:
            raise ValueError("School not found")
        if school.status != "active":
            raise ValueError(f"Can only start/stop a provisioned school (status is '{school.status}')")

        private_pem = decrypt_secret(school.deploy_private_key_encrypted)
        pkey = paramiko.RSAKey.from_private_key(io.StringIO(private_pem))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=school.elastic_ip, username=DEPLOY_SSH_USER, pkey=pkey, timeout=30)
        try:
            action = "start" if running else "stop"
            _, stdout, stderr = client.exec_command(f"sudo systemctl {action} model-service.service", timeout=30)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode("utf-8", "ignore")
                raise RuntimeError(f"systemctl {action} failed (exit {exit_code}): {err}")
        finally:
            client.close()

        school.service_stopped = not running
        db.commit()
    finally:
        db.close()
