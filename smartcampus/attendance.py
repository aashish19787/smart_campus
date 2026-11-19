"""Attendance recording, calculation, warnings and history."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import db as dbm
from .db import audit, get_db, get_float, valid_date
from .security import course_access, enrolled_students, login_required, roles_required

bp = Blueprint("attendance", __name__)
STATUSES = ("Present", "Absent", "Late")


def attendance_stats(db, student_id, course_id=None, date_from=None, date_to=None):
    """Percentage follows the configured policy: Present=1, Late=late_weight, Absent=0."""
    sql = ("SELECT r.status, COUNT(*) c FROM att_records r JOIN att_sessions s ON s.id=r.session_id "
           "WHERE r.student_id=?")
    args = [student_id]
    if course_id:
        sql += " AND s.course_id=?"
        args.append(course_id)
    if date_from:
        sql += " AND s.date>=?"
        args.append(date_from)
    if date_to:
        sql += " AND s.date<=?"
        args.append(date_to)
    counts = {r["status"]: r["c"] for r in db.execute(sql + " GROUP BY r.status", args)}
    present, absent, late = counts.get("Present", 0), counts.get("Absent", 0), counts.get("Late", 0)
    total = present + absent + late
    w = get_float("late_weight", db)
    pct = round((present + late * w) / total * 100, 2) if total else None
    return {"present": present, "absent": absent, "late": late, "total": total, "pct": pct}


def warn_low_attendance(db, course_id, student_ids):
    thr = get_float("attendance_threshold", db)
    course = db.execute("SELECT code,name FROM courses WHERE id=?", (course_id,)).fetchone()
    warned = 0
    for sid in student_ids:
        s = attendance_stats(db, sid, course_id)
        if s["pct"] is not None and s["pct"] < thr:
            msg = "Low attendance warning: %s %s is at %.1f%% (minimum %s%%)." % (
                course["code"], course["name"], s["pct"], dbm.get_setting("attendance_threshold", db))
            dup = db.execute("SELECT 1 FROM notifications WHERE user_id=? AND message=? AND created_at>=?",
                             (sid, msg, dbm.today() + " 00:00:00")).fetchone()
            if not dup:
                dbm.notify([sid], msg, url_for("attendance.index"), email=True, db=db)
                warned += 1
    return warned


@bp.route("/attendance")
@login_required
def index():
    db = get_db()
    role = g.user["role"]
    if role == "student":
        thr = get_float("attendance_threshold")
        rows = []
        for c in db.execute("SELECT c.* FROM enrollments e JOIN courses c ON c.id=e.course_id "
                            "WHERE e.student_id=? ORDER BY c.code", (g.user["id"],)):
            s = attendance_stats(db, g.user["id"], c["id"])
            rows.append((c, s, s["pct"] is not None and s["pct"] < thr))
        overall = attendance_stats(db, g.user["id"])
        return render_template("attendance_student.html", rows=rows, overall=overall, thr=thr)
    if role == "teacher":
        courses = db.execute("SELECT * FROM courses WHERE teacher_id=? ORDER BY code", (g.user["id"],)).fetchall()
    elif role == "admin":
        courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    else:
        abort(403)
    return render_template("attendance_courses.html", courses=courses)


@bp.route("/attendance/take/<int:course_id>", methods=["GET", "POST"])
@roles_required("teacher")
def take(course_id):
    db = get_db()
    course = course_access(course_id, write=True)
    students = enrolled_students(course_id)
    date = (request.values.get("date") or dbm.today()).strip()
    if not valid_date(date):
        flash("Invalid date.", "error")
        date = dbm.today()
    sess = db.execute("SELECT * FROM att_sessions WHERE course_id=? AND date=?", (course_id, date)).fetchone()
    existing = {}
    if sess:
        existing = {r["student_id"]: r["status"] for r in
                    db.execute("SELECT student_id,status FROM att_records WHERE session_id=?", (sess["id"],))}
    if request.method == "POST" and request.form.get("save"):
        form = {s["id"]: request.form.get("status_%d" % s["id"]) for s in students}
        reason = request.form.get("reason", "").strip()
        if date > dbm.today():
            flash("Attendance cannot be recorded for a future date.", "error")
        elif not students:
            flash("No students are enrolled in this course.", "error")
        elif any(v not in STATUSES for v in form.values()):
            flash("Incomplete record: mark every student as Present, Absent or Late.", "error")
        elif sess:
            changed = {k: v for k, v in form.items() if existing.get(k) != v}
            if not changed:
                flash("No changes to save.", "error")
            elif not reason:
                flash("A reason is required to correct an existing attendance record.", "error")
            else:
                for sid, st in changed.items():
                    db.execute("INSERT INTO att_records(session_id,student_id,status) VALUES(?,?,?) "
                               "ON CONFLICT(session_id,student_id) DO UPDATE SET status=excluded.status",
                               (sess["id"], sid, st))
                audit("attendance_correct", "course", "%s %s: %d change(s). Reason: %s" % (
                    course["code"], date, len(changed), reason), db=db)
                warned = warn_low_attendance(db, course_id, list(changed))
                db.commit()
                flash("Attendance corrected (%d change(s)); %d warning(s) sent." % (len(changed), warned), "success")
                return redirect(url_for("attendance.take", course_id=course_id, date=date))
        else:
            cur = db.execute("INSERT OR IGNORE INTO att_sessions(course_id,date,taken_by,created_at) VALUES(?,?,?,?)",
                             (course_id, date, g.user["id"], dbm.now()))
            if cur.rowcount == 0:
                flash("Attendance for this date already exists (duplicate sessions are not allowed).", "error")
            else:
                sid_ = cur.lastrowid
                db.executemany("INSERT INTO att_records(session_id,student_id,status) VALUES(?,?,?)",
                               [(sid_, k, v) for k, v in form.items()])
                audit("attendance_record", "course", "%s %s: %d students" % (course["code"], date, len(form)), db=db)
                warned = warn_low_attendance(db, course_id, list(form))
                db.commit()
                flash("Attendance saved. %d low-attendance warning(s) sent." % warned, "success")
                return redirect(url_for("attendance.take", course_id=course_id, date=date))
        existing = {k: v for k, v in form.items() if v in STATUSES} or existing
    return render_template("attendance_take.html", course=course, students=students, date=date, sess=sess,
                           existing=existing, statuses=STATUSES, today=dbm.today())


@bp.route("/attendance/course/<int:course_id>")
@roles_required("teacher", "admin")
def course_summary(course_id):
    db = get_db()
    course = course_access(course_id)
    thr = get_float("attendance_threshold")
    rows = []
    for s in enrolled_students(course_id):
        st = attendance_stats(db, s["id"], course_id)
        rows.append((s, st, st["pct"] is not None and st["pct"] < thr))
    sessions = db.execute("SELECT s.*, (SELECT COUNT(*) FROM att_records r WHERE r.session_id=s.id AND r.status!='Absent') p, "
                          "(SELECT COUNT(*) FROM att_records r WHERE r.session_id=s.id) t FROM att_sessions s "
                          "WHERE course_id=? ORDER BY date DESC LIMIT 60", (course_id,)).fetchall()
    return render_template("attendance_summary.html", course=course, rows=rows, thr=thr, sessions=sessions)


@bp.route("/attendance/history/<int:course_id>")
@login_required
def history(course_id):
    db = get_db()
    role = g.user["role"]
    if role == "student":
        sid = g.user["id"]
        if not db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?", (course_id, sid)).fetchone():
            abort(403)
        course = db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
    else:
        course = course_access(course_id)
        sid = request.args.get("student", type=int)
        if not sid:
            abort(404)
    student = db.execute("SELECT u.name, s.student_no FROM users u JOIN students s ON s.user_id=u.id WHERE u.id=?",
                         (sid,)).fetchone()
    if not student:
        abort(404)
    recs = db.execute("SELECT s.date, r.status FROM att_records r JOIN att_sessions s ON s.id=r.session_id "
                      "WHERE s.course_id=? AND r.student_id=? ORDER BY s.date DESC", (course_id, sid)).fetchall()
    return render_template("attendance_history.html", course=course, student=student, recs=recs,
                           stats=attendance_stats(db, sid, course_id))
