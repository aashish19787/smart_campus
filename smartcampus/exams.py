"""Examination schedules, marks entry/verification, result calculation and publication."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, Response, url_for

from . import db as dbm
from .db import audit, get_db, grade_for, now, to_float, valid_date
from .pdfutil import table_pdf
from .security import course_access, enrolled_students, is_enrolled, login_required, roles_required

bp = Blueprint("exams", __name__)
EXAM_TYPES = ("Internal", "Quiz", "Midterm", "Practical", "Final")


def get_exam(eid):
    e = get_db().execute("SELECT e.*, c.code, c.name AS course_name, c.teacher_id FROM exams e "
                         "JOIN courses c ON c.id=e.course_id WHERE e.id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    return e


def valid_time(s):
    try:
        h, m = s.split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (ValueError, AttributeError):
        return False


def result_row(marks, max_marks, db=None):
    pct = round(marks / max_marks * 100, 2) if max_marks else 0.0
    return pct, grade_for(pct, db)


@bp.route("/exams")
@login_required
def index():
    db = get_db()
    role = g.user["role"]
    base = ("SELECT e.*, c.code, c.name AS course_name, "
            "(SELECT COUNT(*) FROM marks m WHERE m.exam_id=e.id) AS marked, "
            "(SELECT COUNT(*) FROM enrollments n WHERE n.course_id=e.course_id) AS enrolled "
            "FROM exams e JOIN courses c ON c.id=e.course_id ")
    if role in ("examofficer", "admin"):
        rows = db.execute(base + "ORDER BY e.exam_date DESC, e.exam_time").fetchall()
    elif role == "teacher":
        rows = db.execute(base + "WHERE c.teacher_id=? ORDER BY e.exam_date DESC", (g.user["id"],)).fetchall()
    elif role == "student":
        rows = db.execute(base + "WHERE e.course_id IN (SELECT course_id FROM enrollments WHERE student_id=?) "
                          "ORDER BY e.exam_date DESC", (g.user["id"],)).fetchall()
    else:
        abort(403)
    return render_template("exams.html", rows=rows, today=dbm.today())


def _exam_form(e=None):
    db = get_db()
    courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    if request.method == "POST":
        cid = request.form.get("course_id", "") if not e else str(e["course_id"])
        etype = request.form.get("exam_type", "")
        date = request.form.get("exam_date", "").strip()
        time_ = request.form.get("exam_time", "").strip()
        mx = to_float(request.form.get("max_marks"))
        err = None
        if not cid.isdigit() or not db.execute("SELECT 1 FROM courses WHERE id=?", (int(cid),)).fetchone():
            err = "Select a valid course."
        elif etype not in EXAM_TYPES:
            err = "Select a valid examination type."
        elif not valid_date(date):
            err = "Enter a valid examination date."
        elif not valid_time(time_):
            err = "Enter a valid examination time (HH:MM)."
        elif mx is None or mx <= 0 or mx > 1000:
            err = "Maximum marks must be a positive number."
        elif e and e["status"] != "scheduled" and mx != e["max_marks"]:
            err = "Maximum marks cannot change after marks have been entered."
        if err:
            flash(err, "error")
            return None
        if e:
            db.execute("UPDATE exams SET exam_type=?,exam_date=?,exam_time=?,max_marks=? WHERE id=?",
                       (etype, date, time_, mx, e["id"]))
            audit("exam_update", "exam", "%s %s %s" % (e["code"], etype, date), db=db)
            eid = e["id"]
        else:
            cur = db.execute("INSERT INTO exams(course_id,exam_type,exam_date,exam_time,max_marks,created_by) "
                             "VALUES(?,?,?,?,?,?)", (int(cid), etype, date, time_, mx, g.user["id"]))
            eid = cur.lastrowid
            audit("exam_create", "exam", "course %s %s %s" % (cid, etype, date), db=db)
        c = db.execute("SELECT code, teacher_id FROM courses WHERE id=?", (int(cid),)).fetchone()
        ids = [r["student_id"] for r in db.execute("SELECT student_id FROM enrollments WHERE course_id=?", (int(cid),))]
        if c["teacher_id"]:
            ids.append(c["teacher_id"])
        dbm.notify(ids, "Examination schedule %s: %s %s on %s at %s" % (
            "updated" if e else "published", c["code"], etype, date, time_), url_for("exams.index"), db=db)
        db.commit()
        flash("Examination saved.", "success")
        return eid
    return None


@bp.route("/exams/new", methods=["GET", "POST"])
@roles_required("examofficer")
def new():
    r = _exam_form()
    if r:
        return redirect(url_for("exams.index"))
    courses = get_db().execute("SELECT * FROM courses ORDER BY code").fetchall()
    return render_template("exam_form.html", e=None, courses=courses, types=EXAM_TYPES)


@bp.route("/exams/<int:eid>/edit", methods=["GET", "POST"])
@roles_required("examofficer")
def edit(eid):
    e = get_exam(eid)
    if e["status"] == "published":
        flash("Published examinations cannot be edited.", "error")
        return redirect(url_for("exams.index"))
    if _exam_form(e):
        return redirect(url_for("exams.index"))
    return render_template("exam_form.html", e=e, courses=[], types=EXAM_TYPES)


@bp.route("/exams/<int:eid>/delete", methods=["POST"])
@roles_required("examofficer")
def delete(eid):
    db = get_db()
    e = get_exam(eid)
    if e["status"] != "scheduled":
        flash("Only examinations without marks can be deleted.", "error")
    else:
        db.execute("DELETE FROM exams WHERE id=?", (eid,))
        audit("exam_delete", "exam", "%s %s %s" % (e["code"], e["exam_type"], e["exam_date"]), db=db)
        db.commit()
        flash("Examination deleted.", "success")
    return redirect(url_for("exams.index"))


@bp.route("/exams/<int:eid>/marks", methods=["GET", "POST"])
@roles_required("teacher", "examofficer", "admin")
def marks(eid):
    db = get_db()
    e = get_exam(eid)
    role = g.user["role"]
    if role == "teacher":
        course_access(e["course_id"], write=True)
    students = enrolled_students(e["course_id"])
    existing = {r["student_id"]: r["marks"] for r in db.execute("SELECT student_id, marks FROM marks WHERE exam_id=?",
                                                                (eid,))}
    can_edit = role == "teacher" and e["status"] != "published"
    if request.method == "POST":
        if not can_edit:
            abort(403)
        reason = request.form.get("reason", "").strip()
        updates, err = [], None
        for s in students:
            raw = request.form.get("marks_%d" % s["id"], "").strip()
            if raw == "":
                continue
            v = to_float(raw)
            if v is None or v < 0 or v > e["max_marks"]:
                err = "Marks for %s must be between 0 and %g." % (s["student_no"] or s["name"], e["max_marks"])
                break
            if s["id"] in existing and abs(existing[s["id"]] - v) < 1e-9:
                continue
            updates.append((s["id"], existing.get(s["id"]), v))
        if not err and any(old is not None for _, old, _ in updates) and not reason:
            err = "A reason is required when correcting existing marks."
        if err:
            flash(err, "error")
            existing = {**existing, **{k: to_float(request.form.get("marks_%d" % k), existing.get(k))
                                       for k in [s["id"] for s in students]
                                       if request.form.get("marks_%d" % k, "").strip() != ""}}
        elif not updates:
            flash("No changes to save.", "error")
        else:
            ts = now()
            for sid, old, new in updates:
                db.execute("INSERT INTO marks(exam_id,student_id,marks,entered_by,updated_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(exam_id,student_id) DO UPDATE SET marks=excluded.marks, "
                           "entered_by=excluded.entered_by, updated_at=excluded.updated_at",
                           (eid, sid, new, g.user["id"], ts))
                db.execute("INSERT INTO mark_audit(exam_id,student_id,old_marks,new_marks,reason,changed_by,changed_at) "
                           "VALUES(?,?,?,?,?,?,?)", (eid, sid, old, new, reason, g.user["id"], ts))
            if e["status"] in ("scheduled", "verified", "approved"):
                db.execute("UPDATE exams SET status='entered' WHERE id=?", (eid,))  # needs (re)verification
            audit("marks_entry", "exam", "%s %s: %d mark(s) saved%s" % (
                e["code"], e["exam_type"], len(updates), (". Reason: " + reason) if reason else ""), db=db)
            db.commit()
            flash("Marks saved (%d change(s)). Examination Officer review is required." % len(updates), "success")
            return redirect(url_for("exams.marks", eid=eid))
    history = db.execute("SELECT a.*, u.name AS student, s.student_no, t.name AS by FROM mark_audit a "
                         "JOIN users u ON u.id=a.student_id LEFT JOIN students s ON s.user_id=a.student_id "
                         "LEFT JOIN users t ON t.id=a.changed_by WHERE a.exam_id=? ORDER BY a.id DESC LIMIT 50",
                         (eid,)).fetchall()
    rows = []
    for s in students:
        m = existing.get(s["id"])
        pct, gr = result_row(m, e["max_marks"], db) if m is not None else (None, None)
        rows.append((s, m, pct, gr))
    missing = sum(1 for s in students if s["id"] not in existing)
    return render_template("exam_marks.html", e=e, rows=rows, can_edit=can_edit, history=history, missing=missing)


@bp.route("/exams/<int:eid>/workflow/<action>", methods=["POST"])
@roles_required("examofficer")
def workflow(eid, action):
    db = get_db()
    e = get_exam(eid)
    students = enrolled_students(e["course_id"])
    n_marks = db.execute("SELECT COUNT(*) c FROM marks WHERE exam_id=?", (eid,)).fetchone()["c"]
    flow = {"verify": ("entered", "verified"), "approve": ("verified", "approved"),
            "publish": ("approved", "published"), "return": (None, "entered")}
    if action not in flow:
        abort(404)
    need, to = flow[action]
    if action == "return":
        if e["status"] not in ("verified", "approved"):
            flash("Only verified or approved examinations can be returned for correction.", "error")
            return redirect(url_for("exams.index"))
    elif e["status"] != need:
        flash("This step is not available while the examination is '%s'." % e["status"], "error")
        return redirect(url_for("exams.index"))
    if action == "verify":
        missing = sum(1 for s in students if db.execute(
            "SELECT 1 FROM marks WHERE exam_id=? AND student_id=?", (eid, s["id"])).fetchone() is None)
        if n_marks == 0 or missing:
            flash("Cannot verify: %d enrolled student(s) have no marks entered." % (missing if n_marks else len(students)),
                  "error")
            return redirect(url_for("exams.index"))
    db.execute("UPDATE exams SET status=?, published_at=? WHERE id=?",
               (to, now() if to == "published" else None, eid))
    audit("exam_" + action, "exam", "%s %s %s" % (e["code"], e["exam_type"], e["exam_date"]), db=db)
    if action == "publish":
        ids = [s["id"] for s in students]
        dbm.notify(ids, "Results published: %s %s" % (e["code"], e["exam_type"]), url_for("exams.results"),
                   email=True, db=db)
    if action == "return" and e["teacher_id"]:
        dbm.notify([e["teacher_id"]], "Marks returned for correction: %s %s" % (e["code"], e["exam_type"]),
                   url_for("exams.marks", eid=eid), db=db)
    db.commit()
    flash({"verify": "Marks verified.", "approve": "Results approved.", "publish": "Results published to students.",
           "return": "Returned to the teacher for correction."}[action], "success")
    return redirect(url_for("exams.index"))


def student_results(db, student_id):
    rows = db.execute(
        "SELECT c.id AS course_id, c.code, c.name, e.exam_type, e.exam_date, e.max_marks, m.marks "
        "FROM marks m JOIN exams e ON e.id=m.exam_id JOIN courses c ON c.id=e.course_id "
        "WHERE m.student_id=? AND e.status='published' ORDER BY c.code, e.exam_date", (student_id,)).fetchall()
    out, totals = [], {}
    for r in rows:
        pct, gr = result_row(r["marks"], r["max_marks"], db)
        out.append({**dict(r), "pct": pct, "grade": gr})
        t = totals.setdefault(r["course_id"], {"code": r["code"], "name": r["name"], "marks": 0.0, "max": 0.0})
        t["marks"] += r["marks"]
        t["max"] += r["max_marks"]
    summary = []
    for t in totals.values():
        t["pct"], t["grade"] = result_row(t["marks"], t["max"], db)
        summary.append(t)
    return out, summary


@bp.route("/results")
@roles_required("student", "examofficer", "admin")
def results():
    db = get_db()
    if g.user["role"] == "student":
        rows, summary = student_results(db, g.user["id"])
        return render_template("results_student.html", rows=rows, summary=summary)
    exams = db.execute("SELECT e.*, c.code, c.name AS course_name, "
                       "(SELECT COUNT(*) FROM marks m WHERE m.exam_id=e.id) AS marked FROM exams e "
                       "JOIN courses c ON c.id=e.course_id WHERE e.status IN ('approved','published') "
                       "ORDER BY e.exam_date DESC").fetchall()
    return render_template("results_officer.html", exams=exams)


@bp.route("/results/exam/<int:eid>")
@roles_required("examofficer", "admin")
def exam_results(eid):
    db = get_db()
    e = get_exam(eid)
    rows = []
    for r in db.execute("SELECT m.marks, u.name, s.student_no FROM marks m JOIN users u ON u.id=m.student_id "
                        "LEFT JOIN students s ON s.user_id=u.id WHERE m.exam_id=? ORDER BY s.student_no", (eid,)):
        pct, gr = result_row(r["marks"], e["max_marks"], db)
        rows.append((r, pct, gr))
    return render_template("results_exam.html", e=e, rows=rows)


@bp.route("/results/download")
@roles_required("student")
def download():
    db = get_db()
    rows, summary = student_results(db, g.user["id"])
    st = db.execute("SELECT student_no FROM students WHERE user_id=?", (g.user["id"],)).fetchone()
    data = [[r["code"], r["exam_type"], r["exam_date"], "%g" % r["marks"], "%g" % r["max_marks"],
             "%.2f%%" % r["pct"], r["grade"]] for r in rows]
    for t in summary:
        data.append([t["code"], "TOTAL", "", "%g" % t["marks"], "%g" % t["max"], "%.2f%%" % t["pct"], t["grade"]])
    pdf = table_pdf("Examination Results", ["Course", "Exam", "Date", "Marks", "Max", "Percentage", "Grade"], data,
                    "%s (%s)" % (g.user["name"], st["student_no"] if st else ""), dbm.get_setting("college_name"))
    audit("results_download", "student", g.user["email"], db=db)
    db.commit()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=results.pdf"})
