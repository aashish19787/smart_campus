"""Login, logout, password recovery, profile and role dashboards."""
import hashlib
import secrets
import time
from datetime import datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm
from .attendance import attendance_stats
from .db import audit, get_db, get_float, now, password_problem, valid_email
from .security import login_required, safe_next

bp = Blueprint("auth", __name__)
MAX_FAILS = 5
LOCK_MIN = 15


def release_due_assignments(db):
    """Notify students about assignments whose publication date has arrived (FR-82)."""
    rows = db.execute("SELECT a.id, a.title, a.course_id, c.code FROM assignments a "
                      "JOIN courses c ON c.id=a.course_id WHERE a.notified=0 AND a.publish_date<=?",
                      (now(),)).fetchall()
    for a in rows:
        ids = [r["student_id"] for r in db.execute(
            "SELECT e.student_id FROM enrollments e JOIN users u ON u.id=e.student_id "
            "WHERE e.course_id=? AND u.active=1", (a["course_id"],))]
        dbm.notify(ids, "New assignment published in %s: %s" % (a["code"], a["title"]),
                   url_for("assignments.view", aid=a["id"]), email=True, db=db)
        db.execute("UPDATE assignments SET notified=1 WHERE id=?", (a["id"],))
    if rows:
        db.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("auth.dashboard"))
    db = get_db()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        since = (datetime.now() - timedelta(minutes=LOCK_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        fails = db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='login_failed' AND detail=? "
                           "AND created_at>=?", (email, since)).fetchone()["c"]
        if fails >= MAX_FAILS:
            flash("Too many failed attempts. Try again in %d minutes." % LOCK_MIN, "error")
            return render_template("login.html"), 429
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            audit("login_failed", "auth", email, user_name=email, db=db)
            db.commit()
            flash("Invalid email or password.", "error")
            return render_template("login.html"), 401
        if not user["active"]:
            audit("login_denied", "auth", "inactive account", user_id=user["id"], db=db)
            db.commit()
            flash("This account is deactivated. Please contact the administrator.", "error")
            return render_template("login.html"), 403
        nxt = safe_next(request.args.get("next"))
        session.clear()
        session["user_id"] = user["id"]
        session["last_active"] = time.time()
        audit("login", "auth", "Login successful", user_id=user["id"], db=db)
        db.commit()
        return redirect(nxt or url_for("auth.dashboard"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("logout", "auth", "Logout")
    get_db().commit()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        db = get_db()
        email = request.form.get("email", "").strip().lower()
        user = db.execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            h = hashlib.sha256(token.encode()).hexdigest()
            exp = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)",
                       (user["id"], h, exp))
            link = url_for("auth.reset", token=token, _external=True)
            dbm.send_email(user["email"], "Smart Campus password reset",
                           "Hello %s,\n\nUse this link within 1 hour to reset your password:\n%s\n\n"
                           "If you did not request this, ignore this message." % (user["name"], link), db)
            audit("password_reset_requested", "auth", email, user_id=user["id"], db=db)
            db.commit()
        flash("If that e-mail is registered, a password reset link has been sent.", "success")
        return redirect(url_for("auth.login"))
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = get_db()
    h = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute("SELECT * FROM password_resets WHERE token_hash=? AND used=0 AND expires_at>=?",
                     (h, now())).fetchone()
    if not row:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        problem = password_problem(pw) or (None if pw == pw2 else "Passwords do not match.")
        if problem:
            flash(problem, "error")
            return render_template("reset.html"), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), row["user_id"]))
        db.execute("UPDATE password_resets SET used=1 WHERE user_id=?", (row["user_id"],))
        audit("password_reset", "auth", "Password reset via e-mail", user_id=row["user_id"], db=db)
        db.commit()
        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("reset.html")


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    u = g.user
    if request.method == "POST":
        if request.form.get("action") == "password":
            cur = request.form.get("current", "")
            pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
            problem = None
            if not check_password_hash(u["password_hash"], cur):
                problem = "Current password is incorrect."
            else:
                problem = password_problem(pw) or (None if pw == pw2 else "Passwords do not match.")
            if problem:
                flash(problem, "error")
            else:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))
                audit("password_change", "user", "Password changed")
                db.commit()
                flash("Password changed.", "success")
                return redirect(url_for("auth.profile"))
        else:
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()[:30]
            address = request.form.get("address", "").strip()[:250]
            if u["role"] == "student":
                name = u["name"]  # students may update contact details only
            if not name or len(name) > 100:
                flash("Name is required (max 100 characters).", "error")
            else:
                db.execute("UPDATE users SET name=?, phone=?, address=? WHERE id=?", (name, phone, address, u["id"]))
                audit("profile_update", "user", "Profile updated")
                db.commit()
                flash("Profile updated.", "success")
                return redirect(url_for("auth.profile"))
    student = db.execute("SELECT s.*, p.name AS program FROM students s LEFT JOIN programs p ON p.id=s.program_id "
                         "WHERE s.user_id=?", (u["id"],)).fetchone()
    return render_template("profile.html", student=student)


@bp.route("/")
@login_required
def dashboard():
    db = get_db()
    u = g.user
    role = u["role"]
    release_due_assignments(db)
    ctx = {}
    if role == "admin":
        ctx["counts"] = db.execute(
            "SELECT role, COUNT(*) c FROM users WHERE active=1 GROUP BY role").fetchall()
        ctx["students"] = db.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
        ctx["courses"] = db.execute("SELECT COUNT(*) c FROM courses").fetchone()["c"]
        ctx["programs"] = db.execute("SELECT COUNT(*) c FROM programs").fetchone()["c"]
        ctx["recent"] = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 8").fetchall()
    elif role == "teacher":
        ctx["courses"] = db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id) AS n FROM courses c "
            "WHERE teacher_id=? ORDER BY c.code", (u["id"],)).fetchall()
        ctx["deadlines"] = db.execute(
            "SELECT a.*, c.code, (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id=a.id) AS subs "
            "FROM assignments a JOIN courses c ON c.id=a.course_id WHERE c.teacher_id=? AND a.deadline>=? "
            "ORDER BY a.deadline LIMIT 6", (u["id"], now())).fetchall()
        ctx["to_grade"] = db.execute(
            "SELECT COUNT(*) c FROM submissions s JOIN assignments a ON a.id=s.assignment_id "
            "JOIN courses c ON c.id=a.course_id WHERE c.teacher_id=? AND s.marks IS NULL", (u["id"],)).fetchone()["c"]
        ctx["marks_due"] = db.execute(
            "SELECT e.*, c.code FROM exams e JOIN courses c ON c.id=e.course_id "
            "WHERE c.teacher_id=? AND e.status IN ('scheduled','entered') AND e.exam_date<=? ORDER BY e.exam_date",
            (u["id"], dbm.today())).fetchall()
    elif role == "student":
        thr = get_float("attendance_threshold")
        courses = db.execute("SELECT c.* FROM enrollments e JOIN courses c ON c.id=e.course_id "
                             "WHERE e.student_id=? ORDER BY c.code", (u["id"],)).fetchall()
        att = []
        for c in courses:
            s = attendance_stats(db, u["id"], c["id"])
            att.append({"course": c, "stats": s, "low": s["pct"] is not None and s["pct"] < thr})
        ctx["att"] = att
        ctx["threshold"] = thr
        ctx["pending"] = db.execute(
            "SELECT a.*, c.code FROM assignments a JOIN courses c ON c.id=a.course_id "
            "JOIN enrollments e ON e.course_id=c.id AND e.student_id=? "
            "WHERE a.publish_date<=? AND a.deadline>=? AND NOT EXISTS "
            "(SELECT 1 FROM submissions s WHERE s.assignment_id=a.id AND s.student_id=?) ORDER BY a.deadline LIMIT 6",
            (u["id"], now(), now(), u["id"])).fetchall()
        ctx["balance"] = db.execute(
            "SELECT COALESCE(SUM(f.amount - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.fee_id=f.id),0)),0) b "
            "FROM fee_records f WHERE f.student_id=?", (u["id"],)).fetchone()["b"]
        ctx["upcoming_exams"] = db.execute(
            "SELECT e.*, c.code, c.name FROM exams e JOIN courses c ON c.id=e.course_id "
            "JOIN enrollments en ON en.course_id=c.id AND en.student_id=? WHERE e.exam_date>=? "
            "ORDER BY e.exam_date, e.exam_time LIMIT 5", (u["id"], dbm.today())).fetchall()
    elif role == "finance":
        ctx["billed"] = db.execute("SELECT COALESCE(SUM(amount),0) v FROM fee_records").fetchone()["v"]
        ctx["collected"] = db.execute("SELECT COALESCE(SUM(amount),0) v FROM payments").fetchone()["v"]
        ctx["overdue"] = db.execute("SELECT COUNT(*) c FROM fee_records WHERE status!='Paid' AND due_date<?",
                                    (dbm.today(),)).fetchone()["c"]
        ctx["recent"] = db.execute(
            "SELECT p.*, s.student_no, f.fee_type FROM payments p JOIN fee_records f ON f.id=p.fee_id "
            "LEFT JOIN students s ON s.user_id=f.student_id ORDER BY p.id DESC LIMIT 8").fetchall()
    elif role == "examofficer":
        ctx["pending"] = db.execute(
            "SELECT e.*, c.code, c.name FROM exams e JOIN courses c ON c.id=e.course_id "
            "WHERE e.status IN ('entered','verified','approved') ORDER BY e.exam_date").fetchall()
        ctx["upcoming"] = db.execute(
            "SELECT e.*, c.code, c.name FROM exams e JOIN courses c ON c.id=e.course_id "
            "WHERE e.exam_date>=? ORDER BY e.exam_date, e.exam_time LIMIT 6", (dbm.today(),)).fetchall()
    ctx["notes"] = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 5",
                              (u["id"],)).fetchall()
    return render_template("dashboard.html", **ctx)
