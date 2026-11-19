"""Administration: users, students, programs, courses, settings, logs, backups."""
import sqlite3
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for)
from werkzeug.security import generate_password_hash

from . import backup as bk
from . import db as dbm
from .attendance import attendance_stats
from .db import audit, get_db, norm_dt, password_problem, to_float, valid_date, valid_email
from .security import ROLES, login_required, roles_required

bp = Blueprint("admin", __name__)


# ------------------------------------------------------------ users
@bp.route("/admin/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    db = get_db()
    if request.method == "POST":  # create staff account
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "")
        pw = request.form.get("password", "")
        err = None
        if not name or len(name) > 100:
            err = "Name is required."
        elif not valid_email(email):
            err = "A valid e-mail address is required."
        elif role not in ROLES or role == "student":
            err = "Choose a staff role (students are registered under Students)."
        else:
            err = password_problem(pw)
        if not err and db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            err = "A user with this e-mail already exists."
        if err:
            flash(err, "error")
        else:
            db.execute("INSERT INTO users(email,password_hash,name,role,phone,address,active,created_at) "
                       "VALUES(?,?,?,?,?,?,1,?)",
                       (email, generate_password_hash(pw), name, role,
                        request.form.get("phone", "").strip()[:30], "", dbm.now()))
            audit("user_create", "user", "%s (%s)" % (email, role), db=db)
            db.commit()
            flash("Account created for %s." % name, "success")
            return redirect(url_for("admin.users"))
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    sql, args = "SELECT * FROM users WHERE 1=1", []
    if q:
        sql += " AND (name LIKE ? OR email LIKE ?)"
        args += ["%" + q + "%"] * 2
    if role in ROLES:
        sql += " AND role=?"
        args.append(role)
    rows = db.execute(sql + " ORDER BY role, name LIMIT 500", args).fetchall()
    return render_template("admin_users.html", rows=rows, q=q, role=role)


@bp.route("/admin/users/<int:uid>", methods=["GET", "POST"])
@roles_required("admin")
def user_edit(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    if u["role"] == "student":
        return redirect(url_for("admin.student_detail", uid=uid))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", u["role"])
        active = 1 if request.form.get("active") else 0
        pw = request.form.get("password", "")
        err = None
        if not name:
            err = "Name is required."
        elif not valid_email(email):
            err = "A valid e-mail address is required."
        elif role not in ROLES or role == "student":
            err = "Invalid role."
        elif uid == g.user["id"] and (not active or role != "admin"):
            err = "You cannot deactivate or demote your own account."
        elif pw:
            err = password_problem(pw)
        if not err and db.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, uid)).fetchone():
            err = "Another user already has this e-mail."
        if err:
            flash(err, "error")
        else:
            db.execute("UPDATE users SET name=?, email=?, role=?, active=?, phone=? WHERE id=?",
                       (name, email, role, active, request.form.get("phone", "").strip()[:30], uid))
            if pw:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
            audit("user_update", "user", "%s role=%s active=%s%s" % (email, role, active, " pwreset" if pw else ""),
                  db=db)
            db.commit()
            flash("Account updated.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin_user_edit.html", u=u)


# ------------------------------------------------------------ programs
@bp.route("/admin/programs", methods=["GET", "POST"])
@roles_required("admin")
def programs():
    db = get_db()
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        pid = request.form.get("id")
        if not code or not name:
            flash("Program code and name are required.", "error")
        else:
            try:
                if pid:
                    db.execute("UPDATE programs SET code=?, name=? WHERE id=?", (code, name, int(pid)))
                    audit("program_update", "program", code, db=db)
                else:
                    db.execute("INSERT INTO programs(code,name) VALUES(?,?)", (code, name))
                    audit("program_create", "program", code, db=db)
                db.commit()
                flash("Program saved.", "success")
            except (sqlite3.IntegrityError, ValueError):
                db.rollback()
                flash("A program with this code or name already exists.", "error")
        return redirect(url_for("admin.programs"))
    rows = db.execute("SELECT p.*, (SELECT COUNT(*) FROM students s WHERE s.program_id=p.id) AS students, "
                      "(SELECT COUNT(*) FROM courses c WHERE c.program_id=p.id) AS courses "
                      "FROM programs p ORDER BY p.code").fetchall()
    return render_template("admin_programs.html", rows=rows)


# ------------------------------------------------------------ students
def next_student_no(db, year):
    prefix = "HCIT-%s-" % year
    r = db.execute("SELECT MAX(CAST(SUBSTR(student_no, ?) AS INTEGER)) m FROM students WHERE student_no LIKE ?",
                   (len(prefix) + 1, prefix + "%")).fetchone()
    return "%s%04d" % (prefix, (r["m"] or 0) + 1)


def register_student(db, name, email, pw, program_id, enroll_date, phone="", address=""):
    """Create a student user + profile. Returns the new user id. Raises ValueError on bad input."""
    email = email.strip().lower()
    if not name.strip():
        raise ValueError("Name is required.")
    if not valid_email(email):
        raise ValueError("A valid e-mail address is required.")
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise ValueError("A user with this e-mail already exists.")
    problem = password_problem(pw)
    if problem:
        raise ValueError(problem)
    if not valid_date(enroll_date):
        raise ValueError("Enrollment date must be a valid date (YYYY-MM-DD).")
    if not db.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
        raise ValueError("Select a valid academic program.")
    cur = db.execute("INSERT INTO users(email,password_hash,name,role,phone,address,active,created_at) "
                     "VALUES(?,?,?, 'student',?,?,1,?)",
                     (email, generate_password_hash(pw), name.strip(), phone.strip()[:30], address.strip()[:250],
                      dbm.now()))
    uid = cur.lastrowid
    no = next_student_no(db, enroll_date[:4])
    db.execute("INSERT INTO students(user_id,student_no,program_id,enrollment_date) VALUES(?,?,?,?)",
               (uid, no, program_id, enroll_date))
    audit("student_register", "student", "%s %s" % (no, email), db=db)
    return uid


@bp.route("/admin/students", methods=["GET", "POST"])
@roles_required("admin")
def students():
    db = get_db()
    progs = db.execute("SELECT * FROM programs ORDER BY code").fetchall()
    if request.method == "POST":
        try:
            pid = int(request.form.get("program_id", "0"))
        except ValueError:
            pid = 0
        try:
            uid = register_student(db, request.form.get("name", ""), request.form.get("email", ""),
                                   request.form.get("password", ""), pid,
                                   request.form.get("enrollment_date", dbm.today()),
                                   request.form.get("phone", ""), request.form.get("address", ""))
            db.commit()
            flash("Student registered.", "success")
            return redirect(url_for("admin.student_detail", uid=uid))
        except ValueError as e:
            db.rollback()
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Duplicate student record detected. Please retry.", "error")
    q = request.args.get("q", "").strip()
    prog = request.args.get("program", "")
    sql = ("SELECT u.id, u.name, u.email, u.active, s.student_no, s.enrollment_date, p.code AS program "
           "FROM students s JOIN users u ON u.id=s.user_id LEFT JOIN programs p ON p.id=s.program_id WHERE 1=1")
    args = []
    if q:
        sql += " AND (s.student_no LIKE ? OR u.name LIKE ? OR u.email LIKE ? OR p.code LIKE ? OR p.name LIKE ?)"
        args += ["%" + q + "%"] * 5
    if prog.isdigit():
        sql += " AND s.program_id=?"
        args.append(int(prog))
    rows = db.execute(sql + " ORDER BY s.student_no LIMIT 500", args).fetchall()
    return render_template("admin_students.html", rows=rows, progs=progs, q=q, prog=prog, today=dbm.today())


@bp.route("/admin/students/<int:uid>", methods=["GET", "POST"])
@roles_required("admin")
def student_detail(uid):
    db = get_db()
    st = db.execute("SELECT u.*, s.student_no, s.program_id, s.enrollment_date FROM users u "
                    "JOIN students s ON s.user_id=u.id WHERE u.id=?", (uid,)).fetchone()
    if not st:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "enroll":
            cid = request.form.get("course_id", "")
            if cid.isdigit() and db.execute("SELECT 1 FROM courses WHERE id=?", (int(cid),)).fetchone():
                db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)", (int(cid), uid))
                audit("enroll", "student", "%s -> course %s" % (st["student_no"], cid), db=db)
                db.commit()
                flash("Enrolled in course.", "success")
            else:
                flash("Select a valid course.", "error")
        else:
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            pid = request.form.get("program_id", "")
            ed = request.form.get("enrollment_date", "")
            err = None
            if not name:
                err = "Name is required."
            elif not valid_email(email):
                err = "A valid e-mail address is required."
            elif db.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, uid)).fetchone():
                err = "Another user already has this e-mail."
            elif not pid.isdigit() or not db.execute("SELECT 1 FROM programs WHERE id=?", (int(pid),)).fetchone():
                err = "Select a valid program."
            elif not valid_date(ed):
                err = "Enrollment date must be a valid date."
            if err:
                flash(err, "error")
            else:
                db.execute("UPDATE users SET name=?, email=?, phone=?, address=?, active=? WHERE id=?",
                           (name, email, request.form.get("phone", "").strip()[:30],
                            request.form.get("address", "").strip()[:250], 1 if request.form.get("active") else 0,
                            uid))
                db.execute("UPDATE students SET program_id=?, enrollment_date=? WHERE user_id=?",
                           (int(pid), ed, uid))
                audit("student_update", "student", st["student_no"], db=db)
                db.commit()
                flash("Student record updated.", "success")
        return redirect(url_for("admin.student_detail", uid=uid))
    progs = db.execute("SELECT * FROM programs ORDER BY code").fetchall()
    courses = db.execute("SELECT c.*, u.name AS teacher FROM enrollments e JOIN courses c ON c.id=e.course_id "
                         "LEFT JOIN users u ON u.id=c.teacher_id WHERE e.student_id=? ORDER BY c.code",
                         (uid,)).fetchall()
    other = db.execute("SELECT * FROM courses WHERE id NOT IN (SELECT course_id FROM enrollments WHERE student_id=?) "
                       "ORDER BY code", (uid,)).fetchall()
    att = [(c, attendance_stats(db, uid, c["id"])) for c in courses]
    results = db.execute(
        "SELECT c.code, e.exam_type, e.exam_date, m.marks, e.max_marks FROM marks m JOIN exams e ON e.id=m.exam_id "
        "JOIN courses c ON c.id=e.course_id WHERE m.student_id=? AND e.status='published' ORDER BY e.exam_date",
        (uid,)).fetchall()
    fees = db.execute(
        "SELECT f.*, COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.fee_id=f.id),0) AS paid "
        "FROM fee_records f WHERE f.student_id=? ORDER BY f.due_date", (uid,)).fetchall()
    return render_template("admin_student.html", st=st, progs=progs, courses=courses, other=other, att=att,
                           results=results, fees=fees, grade_for=dbm.grade_for)


# ------------------------------------------------------------ courses
@bp.route("/courses", methods=["GET", "POST"])
@login_required
def courses():
    db = get_db()
    role = g.user["role"]
    if request.method == "POST":
        if role != "admin":
            abort(403)
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        pid = request.form.get("program_id", "")
        tid = request.form.get("teacher_id", "")
        if not code or not name:
            flash("Course code and name are required.", "error")
        else:
            try:
                db.execute("INSERT INTO courses(code,name,program_id,teacher_id,semester) VALUES(?,?,?,?,?)",
                           (code, name, int(pid) if pid.isdigit() else None, int(tid) if tid.isdigit() else None,
                            request.form.get("semester", "").strip()[:30]))
                audit("course_create", "course", code, db=db)
                db.commit()
                flash("Course created.", "success")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("A course with this code already exists.", "error")
        return redirect(url_for("admin.courses"))
    base = ("SELECT c.*, u.name AS teacher, p.code AS program, "
            "(SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id) AS n FROM courses c "
            "LEFT JOIN users u ON u.id=c.teacher_id LEFT JOIN programs p ON p.id=c.program_id")
    if role == "teacher":
        rows = db.execute(base + " WHERE c.teacher_id=? ORDER BY c.code", (g.user["id"],)).fetchall()
    elif role == "student":
        rows = db.execute(base + " WHERE c.id IN (SELECT course_id FROM enrollments WHERE student_id=?) "
                          "ORDER BY c.code", (g.user["id"],)).fetchall()
    elif role == "admin":
        rows = db.execute(base + " ORDER BY c.code").fetchall()
    else:
        abort(403)
    progs = db.execute("SELECT * FROM programs ORDER BY code").fetchall() if role == "admin" else []
    teachers = db.execute("SELECT id,name FROM users WHERE role='teacher' AND active=1 ORDER BY name").fetchall() \
        if role == "admin" else []
    return render_template("courses.html", rows=rows, progs=progs, teachers=teachers)


@bp.route("/admin/courses/<int:cid>", methods=["GET", "POST"])
@roles_required("admin")
def course_detail(cid):
    db = get_db()
    c = db.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update":
            pid, tid = request.form.get("program_id", ""), request.form.get("teacher_id", "")
            name = request.form.get("name", "").strip()
            if not name:
                flash("Course name is required.", "error")
            else:
                db.execute("UPDATE courses SET name=?, program_id=?, teacher_id=?, semester=? WHERE id=?",
                           (name, int(pid) if pid.isdigit() else None, int(tid) if tid.isdigit() else None,
                            request.form.get("semester", "").strip()[:30], cid))
                audit("course_update", "course", c["code"], db=db)
                db.commit()
                flash("Course updated.", "success")
        elif action == "enroll":
            nos = [x.strip() for x in request.form.get("student_nos", "").replace(",", " ").split() if x.strip()]
            added, missing = 0, []
            for no in nos:
                r = db.execute("SELECT user_id FROM students WHERE student_no=? COLLATE NOCASE", (no,)).fetchone()
                if not r:
                    missing.append(no)
                    continue
                cur = db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)",
                                 (cid, r["user_id"]))
                added += cur.rowcount
            if request.form.get("whole_program") and c["program_id"]:
                cur = db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) "
                                 "SELECT ?, s.user_id FROM students s WHERE s.program_id=?", (cid, c["program_id"]))
                added += max(cur.rowcount, 0)
            audit("enroll", "course", "%s: %d added" % (c["code"], added), db=db)
            db.commit()
            flash("%d student(s) enrolled.%s" % (added, (" Unknown IDs: " + ", ".join(missing)) if missing else ""),
                  "success" if not missing else "error")
        elif action == "unenroll":
            sid = request.form.get("student_id", "")
            if sid.isdigit():
                db.execute("DELETE FROM enrollments WHERE course_id=? AND student_id=?", (cid, int(sid)))
                audit("unenroll", "course", "%s: user %s" % (c["code"], sid), db=db)
                db.commit()
                flash("Student removed from course.", "success")
        return redirect(url_for("admin.course_detail", cid=cid))
    progs = db.execute("SELECT * FROM programs ORDER BY code").fetchall()
    teachers = db.execute("SELECT id,name FROM users WHERE role='teacher' AND active=1 ORDER BY name").fetchall()
    roster = db.execute("SELECT u.id,u.name,s.student_no FROM enrollments e JOIN users u ON u.id=e.student_id "
                        "LEFT JOIN students s ON s.user_id=u.id WHERE e.course_id=? ORDER BY s.student_no",
                        (cid,)).fetchall()
    return render_template("admin_course.html", c=c, progs=progs, teachers=teachers, roster=roster)


# ------------------------------------------------------------ settings / logs / backups / outbox
@bp.route("/admin/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    db = get_db()
    if request.method == "POST":
        new, err = {}, None
        for k in dbm.SETTINGS_DEFAULTS:
            v = request.form.get(k, "").strip()
            if k == "attendance_threshold":
                f = to_float(v)
                if f is None or not 0 <= f <= 100:
                    err = "Attendance threshold must be between 0 and 100."
            elif k == "late_weight":
                f = to_float(v)
                if f is None or not 0 <= f <= 1:
                    err = "Late weight must be between 0 and 1."
            elif k in ("session_timeout_minutes", "max_upload_mb"):
                f = to_float(v)
                if f is None or f <= 0 or f > 1000:
                    err = "%s must be a positive number." % k
            elif k == "grading_scale":
                try:
                    dbm.parse_scale(v)
                except Exception:
                    err = "Grading scale format: 80:A,70:B,60:C,50:D,0:F (must include 0)."
            elif not v:
                err = "College name is required."
            new[k] = v
        if err:
            flash(err, "error")
        else:
            for k, v in new.items():
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (k, v))
            audit("settings_update", "settings", ", ".join("%s=%s" % kv for kv in new.items())[:500], db=db)
            db.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("admin.settings"))
    vals = {k: dbm.get_setting(k, db) for k in dbm.SETTINGS_DEFAULTS}
    return render_template("admin_settings.html", vals=vals, defs=dbm.SETTINGS_DEFAULTS)


@bp.route("/admin/audit")
@roles_required("admin")
def audit_view():
    db = get_db()
    action = request.args.get("action", "").strip()
    q = request.args.get("q", "").strip()
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if action:
        sql += " AND action=?"
        args.append(action)
    if q:
        sql += " AND (user_name LIKE ? OR detail LIKE ? OR entity LIKE ?)"
        args += ["%" + q + "%"] * 3
    rows = db.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall()
    actions = [r["action"] for r in db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return render_template("admin_audit.html", rows=rows, actions=actions, action=action, q=q)


@bp.route("/admin/backups", methods=["GET", "POST"])
@roles_required("admin")
def backups():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("action") == "restore":
                bk.restore_backup(current_app, request.form.get("name", ""), g.user["id"])
                flash("Database restored from backup. Please log in again if your session ended.", "success")
            else:
                name = bk.run_backup(current_app)
                audit("backup", "backup", name, db=db)
                db.commit()
                flash("Backup created and verified: " + name, "success")
        except Exception as exc:
            flash("Operation failed: %s" % exc, "error")
        return redirect(url_for("admin.backups"))
    items = bk.list_backups(current_app)
    for i in items:
        i["ok"] = bk.integrity_ok(current_app.config["BACKUP_FOLDER"] + "/" + i["name"])
    return render_template("admin_backups.html", items=items)


@bp.route("/admin/outbox")
@roles_required("admin")
def outbox():
    rows = get_db().execute("SELECT * FROM email_outbox ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("admin_outbox.html", rows=rows)
