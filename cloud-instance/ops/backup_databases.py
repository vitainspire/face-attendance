"""Daily backup: pg_dump the control-plane database plus every active school's own
tenant database, compressed and rotated locally on this server.

NOT off-site — if this EC2 instance itself is lost, these backups go with it. But it's
a real, automatic safety net against the far more common failure mode: accidental data
loss/corruption in any single database (a bad delete, a bad migration, etc.).
"""
import datetime
import gzip
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/home/ec2-user/app")
import database, models, provisioning  # noqa: E402

BACKUP_DIR = "/home/ec2-user/backups"
RETENTION_DAYS = 7


def _dump(db_url: str, dest_gz_path: str) -> bool:
    raw_path = dest_gz_path[:-3]  # strip trailing .gz for pg_dump's own output
    try:
        result = subprocess.run(
            ["pg_dump", db_url, "--no-owner", "--no-privileges", "-f", raw_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
        )
        if result.returncode != 0:
            print(f"FAILED: {dest_gz_path} - {result.stderr.decode('utf-8', 'ignore')[:500]}")
            if os.path.exists(raw_path):
                os.remove(raw_path)
            return False
        with open(raw_path, "rb") as f_in, gzip.open(dest_gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(raw_path)
        return True
    except Exception as e:
        print(f"FAILED: {dest_gz_path} - {e}")
        if os.path.exists(raw_path):
            os.remove(raw_path)
        return False


def _rotate(folder: str):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)
    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        if os.path.isfile(fpath) and datetime.datetime.utcfromtimestamp(os.path.getmtime(fpath)) < cutoff:
            os.remove(fpath)


def main():
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    any_failed = False

    default_folder = os.path.join(BACKUP_DIR, "_default")
    os.makedirs(default_folder, exist_ok=True)
    ok = _dump(os.environ["DATABASE_URL"], os.path.join(default_folder, f"{stamp}.sql.gz"))
    print(f"{'OK' if ok else 'FAILED'}: default database")
    any_failed = any_failed or not ok
    _rotate(default_folder)

    db = database.SessionLocal()
    schools = db.query(models.School).filter(models.School.status == "active").all()
    db.close()

    for school in schools:
        try:
            url = provisioning.decrypt_secret(school.supabase_db_url_encrypted)
        except Exception as e:
            print(f"FAILED: {school.name} - could not decrypt URL - {e}")
            any_failed = True
            continue
        folder = os.path.join(BACKUP_DIR, f"school_{school.id}_{school.name}")
        os.makedirs(folder, exist_ok=True)
        ok = _dump(url, os.path.join(folder, f"{stamp}.sql.gz"))
        print(f"{'OK' if ok else 'FAILED'}: {school.name}")
        any_failed = any_failed or not ok
        _rotate(folder)

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
