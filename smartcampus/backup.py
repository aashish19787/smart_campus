"""Automated database backups, integrity checks and restore (NFR-49..54)."""
import glob
import os
import sqlite3
import threading
import time
from datetime import datetime

from .db import audit, connect


def backup_dir(app):
    os.makedirs(app.config["BACKUP_FOLDER"], exist_ok=True)
    return app.config["BACKUP_FOLDER"]


def integrity_ok(path):
    try:
        c = sqlite3.connect(path)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.close()
        return ok
    except sqlite3.DatabaseError:
        return False


def list_backups(app):
    files = sorted(glob.glob(os.path.join(backup_dir(app), "smartcampus-*.db")), reverse=True)
    return [{"name": os.path.basename(f), "size_kb": round(os.path.getsize(f) / 1024, 1),
             "time": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S")} for f in files]


def run_backup(app, keep=14):
    dest = os.path.join(backup_dir(app), "smartcampus-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    src = sqlite3.connect(app.config["DATABASE"])
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if not integrity_ok(dest):
        os.remove(dest)
        raise RuntimeError("Backup failed integrity verification.")
    for old in sorted(glob.glob(os.path.join(backup_dir(app), "smartcampus-*.db")), reverse=True)[keep:]:
        os.remove(old)
    return os.path.basename(dest)


def restore_backup(app, name, user_id=None):
    path = os.path.join(backup_dir(app), os.path.basename(name))
    if not name.startswith("smartcampus-") or not os.path.isfile(path):
        raise FileNotFoundError("Backup not found.")
    if not integrity_ok(path):
        raise RuntimeError("Backup file failed integrity check; restore aborted.")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(app.config["DATABASE"], timeout=30)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    conn = connect(app.config["DATABASE"])
    audit("restore", "backup", "Database restored from " + name, user_id=user_id, db=conn)
    conn.commit()
    conn.close()


def start_scheduler(app, interval_hours=24):
    """Background thread: back up every `interval_hours`, verify the latest backup hourly."""
    def loop():
        while True:
            try:
                b = list_backups(app)
                last = os.path.getmtime(os.path.join(backup_dir(app), b[0]["name"])) if b else 0
                if time.time() - last >= interval_hours * 3600:
                    run_backup(app)
                elif b and not integrity_ok(os.path.join(backup_dir(app), b[0]["name"])):
                    run_backup(app)  # latest copy damaged: take a fresh one
            except Exception as exc:  # keep the scheduler alive
                app.logger.error("Backup scheduler error: %s", exc)
            time.sleep(3600)
    t = threading.Thread(target=loop, name="backup-scheduler", daemon=True)
    t.start()
    return t
