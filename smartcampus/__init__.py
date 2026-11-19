"""Smart Campus - College Management and Student Service System (HCIT)."""
import os
import secrets

from flask import Flask, g, render_template, request, session

from . import db as dbm
from .security import ROLES, check_csrf, csrf_token, load_user

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAV = {
    "admin": [("Dashboard", "auth.dashboard"), ("Users", "admin.users"), ("Students", "admin.students"),
              ("Programs", "admin.programs"), ("Courses", "admin.courses"), ("Attendance", "attendance.index"),
              ("Exams", "exams.index"), ("Fees", "fees.index"), ("Announcements", "comms.announcements"),
              ("Reports", "reports.index"), ("Activity log", "admin.audit_view"),
              ("Settings", "admin.settings"), ("Backups", "admin.backups"), ("E-mail outbox", "admin.outbox")],
    "teacher": [("Dashboard", "auth.dashboard"), ("My courses", "admin.courses"), ("Attendance", "attendance.index"),
                ("Assignments", "assignments.index"), ("Exams & marks", "exams.index"),
                ("Announcements", "comms.announcements"), ("Reports", "reports.index")],
    "student": [("Dashboard", "auth.dashboard"), ("My courses", "admin.courses"), ("Attendance", "attendance.index"),
                ("Assignments", "assignments.index"), ("Exams", "exams.index"), ("Results", "exams.results"),
                ("Fees", "fees.index"), ("Announcements", "comms.announcements"),
                ("Notifications", "comms.notifications")],
    "finance": [("Dashboard", "auth.dashboard"), ("Student fees", "fees.index"), ("New fee", "fees.new_fee"),
                ("Reports", "reports.index"), ("Announcements", "comms.announcements")],
    "examofficer": [("Dashboard", "auth.dashboard"), ("Examinations", "exams.index"),
                    ("Results", "exams.results"), ("Reports", "reports.index"),
                    ("Announcements", "comms.announcements")],
}


def create_app(config=None):
    app = Flask(__name__, instance_path=os.path.join(BASE, "instance"), instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    key_file = os.path.join(app.instance_path, "secret_key")
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if not os.path.exists(key_file):
            with open(key_file, "w") as f:
                f.write(secrets.token_hex(32))
            os.chmod(key_file, 0o600)
        secret = open(key_file).read().strip()

    app.config.update(
        SECRET_KEY=secret,
        DATABASE=os.path.join(app.instance_path, "smartcampus.db"),
        UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
        BACKUP_FOLDER=os.path.join(app.instance_path, "backups"),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SC_HTTPS") == "1",
        SMTP_HOST=os.environ.get("SMTP_HOST"),
        SMTP_PORT=os.environ.get("SMTP_PORT", 587),
        SMTP_USER=os.environ.get("SMTP_USER"),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD"),
        MAIL_FROM=os.environ.get("MAIL_FROM", "no-reply@smartcampus.local"),
    )
    if config:
        app.config.update(config)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    dbm.init_db(app.config["DATABASE"])

    app.teardown_appcontext(dbm.close_db)

    @app.before_request
    def _before():
        load_user()
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            check_csrf()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        user = g.get("user")
        unread = 0
        nav = []
        if user:
            unread = dbm.get_db().execute(
                "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)).fetchone()["c"]
            nav = NAV.get(user["role"], [])
        return dict(csrf_token=csrf_token, current_user=user, nav=nav, unread=unread,
                    role_names=ROLES, college=dbm.get_setting("college_name"))

    @app.template_filter("money")
    def _money(v):
        return "{:,.2f}".format(float(v or 0))

    @app.template_filter("num")
    def _num(v):
        if v is None:
            return "-"
        v = float(v)
        return str(int(v)) if v == int(v) else ("%.2f" % v).rstrip("0").rstrip(".")

    @app.template_filter("dt")
    def _dt(v):
        return (v or "")[:16]

    @app.template_filter("dtlocal")
    def _dtlocal(v):
        return (v or "")[:16].replace(" ", "T")

    from . import admin, assignments, attendance, auth, comms, exams, fees, reports
    for m in (auth, admin, attendance, assignments, exams, fees, comms, reports):
        app.register_blueprint(m.bp)

    def _error(code, title, msg):
        return render_template("error.html", code=code, title=title, msg=msg), code

    @app.errorhandler(400)
    def _e400(e):
        return _error(400, "Bad request", getattr(e, "description", "The request could not be processed."))

    @app.errorhandler(403)
    def _e403(e):
        return _error(403, "Access denied", "You do not have permission to access this page or action.")

    @app.errorhandler(404)
    def _e404(e):
        return _error(404, "Page not found", "The requested page or record does not exist.")

    @app.errorhandler(413)
    def _e413(e):
        return _error(413, "File too large", "The uploaded data exceeds the allowed size.")

    @app.errorhandler(500)
    def _e500(e):
        return _error(500, "Server error",
                      "Something went wrong while processing your request. Your data was not changed. Please try again.")

    return app
