"""Database layer, settings, audit log, notifications and e-mail queue."""
import re
import smtplib
import sqlite3
from datetime import datetime
from email.message import EmailMessage

from flask import current_app, g, has_request_context, session

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student','finance','examofficer')),
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS students(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  student_no TEXT NOT NULL UNIQUE,
  program_id INTEGER REFERENCES programs(id),
  enrollment_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  program_id INTEGER REFERENCES programs(id),
  teacher_id INTEGER REFERENCES users(id),
  semester TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS enrollments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE(course_id, student_id)
);
CREATE TABLE IF NOT EXISTS att_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  taken_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  UNIQUE(course_id, date)
);
CREATE TABLE IF NOT EXISTS att_records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES att_sessions(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('Present','Absent','Late')),
  UNIQUE(session_id, student_id)
);
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  publish_date TEXT NOT NULL,
  deadline TEXT NOT NULL,
  max_marks REAL NOT NULL DEFAULT 100,
  attachment_name TEXT,
  attachment_path TEXT,
  created_by INTEGER REFERENCES users(id),
  notified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  file_path TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  is_late INTEGER NOT NULL DEFAULT 0,
  marks REAL,
  feedback TEXT NOT NULL DEFAULT '',
  released INTEGER NOT NULL DEFAULT 0,
  graded_by INTEGER REFERENCES users(id),
  graded_at TEXT,
  UNIQUE(assignment_id, student_id)
);
CREATE TABLE IF NOT EXISTS exams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  exam_type TEXT NOT NULL,
  exam_date TEXT NOT NULL,
  exam_time TEXT NOT NULL,
  max_marks REAL NOT NULL DEFAULT 100,
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK(status IN ('scheduled','entered','verified','approved','published')),
  created_by INTEGER REFERENCES users(id),
  published_at TEXT
);
CREATE TABLE IF NOT EXISTS marks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  marks REAL NOT NULL,
  entered_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL,
  UNIQUE(exam_id, student_id)
);
CREATE TABLE IF NOT EXISTS mark_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exam_id INTEGER NOT NULL,
  student_id INTEGER NOT NULL,
  old_marks REAL,
  new_marks REAL,
  reason TEXT NOT NULL DEFAULT '',
  changed_by INTEGER,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fee_records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  fee_type TEXT NOT NULL,
  amount REAL NOT NULL CHECK(amount > 0),
  due_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Unpaid' CHECK(status IN ('Paid','Partially Paid','Unpaid')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fee_id INTEGER NOT NULL REFERENCES fee_records(id) ON DELETE CASCADE,
  amount REAL NOT NULL CHECK(amount > 0),
  paid_on TEXT NOT NULL,
  method TEXT NOT NULL,
  reference TEXT NOT NULL DEFAULT '',
  recorded_by INTEGER REFERENCES users(id),
  receipt_no TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General',
  audience TEXT NOT NULL DEFAULT 'all' CHECK(audience IN ('all','students','teachers','staff')),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
  publish_date TEXT,
  attachment_name TEXT,
  attachment_path TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  link TEXT,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(user_id, is_read);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  user_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  entity TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action, created_at);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
"""

SETTINGS_DEFAULTS = {
    "attendance_threshold": ("75", "Minimum attendance percentage (warning below this)"),
    "late_weight": ("1.0", "Weight of a 'Late' record in attendance % (1 = counts as present, 0 = absent)"),
    "session_timeout_minutes": ("30", "Inactive session timeout (minutes)"),
    "grading_scale": ("80:A,70:B,60:C,50:D,0:F", "Grading scale  min%:grade, comma separated"),
    "max_upload_mb": ("10", "Maximum upload size (MB)"),
    "college_name": ("Himalayan College of Information Technology (HCIT)", "College name"),
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def norm_dt(s):
    """Normalise 'YYYY-MM-DD[ T]HH:MM[:SS]' to 'YYYY-MM-DD HH:MM:SS'; None if invalid."""
    s = (s or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def valid_date(s):
    try:
        datetime.strptime((s or "").strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    conn = connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for k, (v, _d) in SETTINGS_DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()


# ---------- settings ----------
def get_setting(key, db=None):
    db = db or get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else SETTINGS_DEFAULTS[key][0]


def get_float(key, db=None):
    try:
        return float(get_setting(key, db))
    except ValueError:
        return float(SETTINGS_DEFAULTS[key][0])


def parse_scale(text):
    out = []
    for part in text.split(","):
        pct, grade = part.strip().split(":")
        out.append((float(pct), grade.strip()))
    out.sort(reverse=True)
    if not out or out[-1][0] != 0:
        raise ValueError("Scale must include a 0 lower bound")
    return out


def grade_for(pct, db=None):
    try:
        scale = parse_scale(get_setting("grading_scale", db))
    except Exception:
        scale = parse_scale(SETTINGS_DEFAULTS["grading_scale"][0])
    for lo, g_ in scale:
        if pct + 1e-9 >= lo:
            return g_
    return scale[-1][1]


# ---------- audit / notify / e-mail ----------
def audit(action, entity="", detail="", user_id=None, user_name=None, db=None):
    db = db or get_db()
    if user_id is None and has_request_context():
        user_id = session.get("user_id")
    if user_name is None:
        user_name = ""
        if user_id:
            r = db.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
            user_name = r["name"] if r else ""
    db.execute(
        "INSERT INTO audit_log(user_id,user_name,action,entity,detail,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, user_name, action, entity, detail, now()),
    )


def send_email(to_email, subject, body, db=None):
    """Queue an e-mail; deliver through SMTP when configured, else keep in the outbox."""
    db = db or get_db()
    status = "logged"
    host = current_app.config.get("SMTP_HOST")
    if host:
        try:
            msg = EmailMessage()
            msg["From"] = current_app.config.get("MAIL_FROM", "no-reply@smartcampus.local")
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT", 587)), timeout=10) as s:
                if current_app.config.get("SMTP_TLS", True):
                    s.starttls()
                if current_app.config.get("SMTP_USER"):
                    s.login(current_app.config["SMTP_USER"], current_app.config.get("SMTP_PASSWORD", ""))
                s.send_message(msg)
            status = "sent"
        except Exception as exc:  # delivery must never break the request
            status = "failed: %s" % str(exc)[:120]
    db.execute(
        "INSERT INTO email_outbox(to_email,subject,body,status,created_at) VALUES(?,?,?,?,?)",
        (to_email, subject, body, status, now()),
    )


def notify(user_ids, message, link=None, email=False, db=None):
    db = db or get_db()
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return
    ts = now()
    db.executemany(
        "INSERT INTO notifications(user_id,message,link,created_at) VALUES(?,?,?,?)",
        [(u, message, link, ts) for u in ids],
    )
    if email and current_app.config.get("SMTP_HOST"):
        q = ",".join("?" * len(ids))
        for r in db.execute("SELECT email FROM users WHERE active=1 AND id IN (%s)" % q, ids):
            send_email(r["email"], "Smart Campus: " + message[:60], message, db)


# ---------- validation helpers ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(e):
    return bool(EMAIL_RE.match(e or "")) and len(e) <= 254


def password_problem(p):
    if len(p or "") < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", p) or not re.search(r"\d", p):
        return "Password must contain letters and digits."
    return None


def to_float(s, default=None):
    try:
        v = float(str(s).strip())
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except (TypeError, ValueError):
        return default
