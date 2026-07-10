"""Runs every 5 minutes: checks the app is actually up and that last night's backup
succeeded, and emails an alert on failure. Dedupes so an ongoing outage doesn't spam an
email every 5 minutes — one alert per incident, then a reminder every hour while it's
still down, plus a "recovered" email once it's back.
"""
import datetime
import json
import os
import smtplib
import subprocess
from email.mime.text import MIMEText

STATE_FILE = "/home/ec2-user/app/.health_alert_state.json"
REMINDER_INTERVAL_MINUTES = 60

ALERT_FROM = os.environ["ALERT_EMAIL_FROM"]
ALERT_APP_PASSWORD = os.environ["ALERT_EMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_EMAIL_TO"]


def send_email(subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM
    msg["To"] = ALERT_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(ALERT_FROM, ALERT_APP_PASSWORD)
        server.send_message(msg)


def _service_active(name: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", name], stdout=subprocess.PIPE, text=True)
    return result.stdout.strip() == "active"


def _http_healthy() -> bool:
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8000/health"],
        stdout=subprocess.PIPE, text=True, timeout=15,
    )
    return result.stdout.strip() == "200"


def _backup_ran_recently() -> bool:
    """The nightly backup timer runs once a day — flag it only if NO backup folder has
    been touched in the last 36 hours (gives slack for the randomized delay + retries)."""
    backup_dir = "/home/ec2-user/backups"
    if not os.path.isdir(backup_dir):
        return False
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=36)
    for root, _, files in os.walk(backup_dir):
        for f in files:
            mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(os.path.join(root, f)))
            if mtime > cutoff:
                return True
    return False


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"down_since": None, "last_alert": None, "problems": []}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    problems = []
    if not _service_active("webapp.service"):
        problems.append("webapp.service is not active")
    if not _service_active("matcher.service"):
        problems.append("matcher.service is not active")
    if not _service_active("cftunnel.service"):
        problems.append("cftunnel.service is not active")
    if not _http_healthy():
        problems.append("/health endpoint is not responding with 200")
    if not _backup_ran_recently():
        problems.append("No database backup has run in the last 36 hours")

    state = _load_state()
    now = datetime.datetime.utcnow()

    if problems:
        if state["down_since"] is None:
            state["down_since"] = now.isoformat()
            send_email(
                "[Smart Attendance] ALERT - problem detected",
                "The following issue(s) were detected:\n\n" + "\n".join(f"- {p}" for p in problems),
            )
            state["last_alert"] = now.isoformat()
        else:
            last_alert = datetime.datetime.fromisoformat(state["last_alert"])
            if (now - last_alert).total_seconds() >= REMINDER_INTERVAL_MINUTES * 60:
                down_since = datetime.datetime.fromisoformat(state["down_since"])
                send_email(
                    "[Smart Attendance] STILL DOWN - reminder",
                    f"Still ongoing since {down_since.isoformat()} UTC:\n\n" +
                    "\n".join(f"- {p}" for p in problems),
                )
                state["last_alert"] = now.isoformat()
        state["problems"] = problems
    else:
        if state["down_since"] is not None:
            down_since = datetime.datetime.fromisoformat(state["down_since"])
            send_email(
                "[Smart Attendance] RECOVERED",
                f"Everything is healthy again. Was down since {down_since.isoformat()} UTC.",
            )
        state = {"down_since": None, "last_alert": None, "problems": []}

    _save_state(state)


if __name__ == "__main__":
    main()
