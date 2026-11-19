"""Authentication helpers, RBAC decorators, CSRF and safe file handling."""
import hmac
import os
import secrets
import time
import uuid
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session, url_for
from werkzeug.utils import secure_filename

from .db import get_db, get_float

ROLES = {
    "admin": "Administrator",
    "teacher": "Teacher",
    "student": "Student",
    "finance": "Finance Staff",
    "examofficer": "Examination Officer",
}
STAFF = ("admin", "teacher", "finance", "examofficer")
ALLOWED_EXT = {"pdf", "doc", "docx", "txt", "ppt", "pptx", "xls", "xlsx", "zip",
               "png", "jpg", "jpeg", "csv", "odt", "rtf"}


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def check_csrf():
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    if not hmac.compare_digest(sent, session.get("_csrf", "-")):
        abort(400, "Invalid or missing security token. Please reload the page and try again.")


def load_user():
    g.user = None
    if request.endpoint == "static":
        return
    uid = session.get("user_id")
    if not uid:
        return
    timeout = get_float("session_timeout_minutes") * 60
    if time.time() - session.get("last_active", 0) > timeout:
        session.clear()
        flash("Your session expired due to inactivity. Please log in again.", "error")
        return
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row["active"]:
        session.clear()
        return
    g.user = row
    session["last_active"] = time.time()


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if g.get("user") is None:
            flash("Please log in to continue.", "error")
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        return f(*a, **kw)
    return wrapper


def roles_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            if g.user["role"] not in roles:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def get_course_or_404(course_id):
    c = get_db().execute(
        "SELECT c.*, u.name AS teacher_name, p.code AS program_code FROM courses c "
        "LEFT JOIN users u ON u.id=c.teacher_id LEFT JOIN programs p ON p.id=c.program_id WHERE c.id=?",
        (course_id,)).fetchone()
    if not c:
        abort(404)
    return c


def course_access(course_id, write=False):
    """Teacher: only assigned courses. Admin: read-only. Others: forbidden."""
    c = get_course_or_404(course_id)
    role = g.user["role"]
    if role == "teacher" and c["teacher_id"] == g.user["id"]:
        return c
    if role == "admin" and not write:
        return c
    abort(403)


def is_enrolled(course_id, student_id):
    return get_db().execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                            (course_id, student_id)).fetchone() is not None


def enrolled_students(course_id):
    return get_db().execute(
        "SELECT u.id, u.name, u.email, s.student_no FROM enrollments e "
        "JOIN users u ON u.id=e.student_id LEFT JOIN students s ON s.user_id=u.id "
        "WHERE e.course_id=? AND u.active=1 ORDER BY s.student_no", (course_id,)).fetchall()


def upload_dir(kind):
    d = os.path.join(current_app.config["UPLOAD_FOLDER"], kind)
    os.makedirs(d, exist_ok=True)
    return d


def save_upload(file, kind, required=False):
    """Validate and store an uploaded file. Returns (original_name, stored_name) or (None, None).
    Raises ValueError with a user-facing message when invalid."""
    if file is None or not file.filename:
        if required:
            raise ValueError("Please choose a file to upload.")
        return None, None
    original = secure_filename(file.filename)
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("File type not allowed. Allowed: " + ", ".join(sorted(ALLOWED_EXT)) + ".")
    limit = int(get_float("max_upload_mb") * 1024 * 1024)
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        raise ValueError("The uploaded file is empty.")
    if size > limit:
        raise ValueError("File is too large (limit %s MB)." % get_float("max_upload_mb"))
    stored = uuid.uuid4().hex + "." + ext
    file.save(os.path.join(upload_dir(kind), stored))
    return original, stored


def remove_upload(kind, stored):
    if stored:
        try:
            os.remove(os.path.join(upload_dir(kind), stored))
        except OSError:
            pass
