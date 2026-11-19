"""Assignment creation, submission, evaluation and secured file downloads."""
import sqlite3

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory, url_for)

from . import db as dbm
from .auth import release_due_assignments
from .db import audit, get_db, norm_dt, now, to_float
from .security import (course_access, enrolled_students, is_enrolled, login_required, remove_upload, roles_required,
                       save_upload, upload_dir)

bp = Blueprint("assignments", __name__)


def get_assignment(aid):
    a = get_db().execute("SELECT a.*, c.code, c.name AS course_name, c.teacher_id FROM assignments a "
                         "JOIN courses c ON c.id=a.course_id WHERE a.id=?", (aid,)).fetchone()
    if not a:
        abort(404)
    return a


def status_of(sub):
    if not sub:
        return "Not Submitted"
    return "Late" if sub["is_late"] else "Submitted"


@bp.route("/assignments")
@login_required
def index():
    db = get_db()
    role = g.user["role"]
    release_due_assignments(db)
    if role == "student":
        rows = db.execute(
            "SELECT a.*, c.code, c.name AS course_name, s.id AS sub_id, s.is_late, s.marks, s.released "
            "FROM assignments a JOIN courses c ON c.id=a.course_id "
            "JOIN enrollments e ON e.course_id=c.id AND e.student_id=? "
            "LEFT JOIN submissions s ON s.assignment_id=a.id AND s.student_id=? "
            "WHERE a.publish_date<=? ORDER BY a.deadline DESC", (g.user["id"], g.user["id"], now())).fetchall()
        return render_template("assignments_student.html", rows=rows, now=now())
    if role == "teacher":
        rows = db.execute(
            "SELECT a.*, c.code, c.name AS course_name, "
            "(SELECT COUNT(*) FROM submissions s WHERE s.assignment_id=a.id) AS subs, "
            "(SELECT COUNT(*) FROM enrollments e WHERE e.course_id=a.course_id) AS enrolled "
            "FROM assignments a JOIN courses c ON c.id=a.course_id WHERE c.teacher_id=? "
            "ORDER BY a.deadline DESC", (g.user["id"],)).fetchall()
    elif role == "admin":
        rows = db.execute(
            "SELECT a.*, c.code, c.name AS course_name, "
            "(SELECT COUNT(*) FROM submissions s WHERE s.assignment_id=a.id) AS subs, "
            "(SELECT COUNT(*) FROM enrollments e WHERE e.course_id=a.course_id) AS enrolled "
            "FROM assignments a JOIN courses c ON c.id=a.course_id ORDER BY a.deadline DESC").fetchall()
    else:
        abort(403)
    return render_template("assignments_teacher.html", rows=rows)


def _form_values(a=None):
    return {
        "title": request.form.get("title", "").strip(),
        "description": request.form.get("description", "").strip(),
        "publish": norm_dt(request.form.get("publish_date")),
        "deadline": norm_dt(request.form.get("deadline")),
        "max": to_float(request.form.get("max_marks"), None),
    }


def _validate(v):
    if not v["title"] or len(v["title"]) > 150:
        return "Title is required (max 150 characters)."
    if not v["publish"] or not v["deadline"]:
        return "Valid publication date and deadline are required."
    if v["deadline"] <= v["publish"]:
        return "Deadline must be after the publication date."
    if v["max"] is None or v["max"] <= 0 or v["max"] > 1000:
        return "Maximum marks must be a positive number."
    return None


@bp.route("/assignments/new", methods=["GET", "POST"])
@roles_required("teacher")
def new():
    db = get_db()
    courses = db.execute("SELECT * FROM courses WHERE teacher_id=? ORDER BY code", (g.user["id"],)).fetchall()
    if request.method == "POST":
        cid = request.form.get("course_id", "")
        v = _form_values()
        err = None
        if not cid.isdigit():
            err = "Select a course."
        else:
            course_access(int(cid), write=True)  # 403 if not assigned
            err = _validate(v)
        if not err:
            try:
                name, stored = save_upload(request.files.get("attachment"), "assignments")
            except ValueError as e:
                err = str(e)
        if err:
            flash(err, "error")
        else:
            cur = db.execute(
                "INSERT INTO assignments(course_id,title,description,publish_date,deadline,max_marks,"
                "attachment_name,attachment_path,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(cid), v["title"], v["description"], v["publish"], v["deadline"], v["max"], name, stored,
                 g.user["id"], now()))
            audit("assignment_create", "assignment", "%s (course %s)" % (v["title"], cid), db=db)
            db.commit()
            release_due_assignments(db)
            flash("Assignment created.", "success")
            return redirect(url_for("assignments.view", aid=cur.lastrowid))
    return render_template("assignment_form.html", a=None, courses=courses, now=now())


@bp.route("/assignments/<int:aid>/edit", methods=["GET", "POST"])
@roles_required("teacher")
def edit(aid):
    db = get_db()
    a = get_assignment(aid)
    course_access(a["course_id"], write=True)
    if request.method == "POST":
        v = _form_values()
        err = _validate(v)
        name, stored = a["attachment_name"], a["attachment_path"]
        if not err:
            try:
                n2, s2 = save_upload(request.files.get("attachment"), "assignments")
                if s2:
                    remove_upload("assignments", stored)
                    name, stored = n2, s2
            except ValueError as e:
                err = str(e)
        if err:
            flash(err, "error")
        else:
            db.execute("UPDATE assignments SET title=?,description=?,publish_date=?,deadline=?,max_marks=?,"
                       "attachment_name=?,attachment_path=? WHERE id=?",
                       (v["title"], v["description"], v["publish"], v["deadline"], v["max"], name, stored, aid))
            audit("assignment_update", "assignment", v["title"], db=db)
            db.commit()
            flash("Assignment updated.", "success")
            return redirect(url_for("assignments.view", aid=aid))
    return render_template("assignment_form.html", a=a, courses=[], now=now())


@bp.route("/assignments/<int:aid>/delete", methods=["POST"])
@roles_required("teacher")
def delete(aid):
    db = get_db()
    a = get_assignment(aid)
    course_access(a["course_id"], write=True)
    files = [r["file_path"] for r in db.execute("SELECT file_path FROM submissions WHERE assignment_id=?", (aid,))]
    db.execute("DELETE FROM assignments WHERE id=?", (aid,))
    audit("assignment_delete", "assignment", a["title"], db=db)
    db.commit()
    remove_upload("assignments", a["attachment_path"])
    for f in files:
        remove_upload("submissions", f)
    flash("Assignment deleted.", "success")
    return redirect(url_for("assignments.index"))


@bp.route("/assignments/<int:aid>")
@login_required
def view(aid):
    db = get_db()
    a = get_assignment(aid)
    role = g.user["role"]
    if role == "student":
        if not is_enrolled(a["course_id"], g.user["id"]) or a["publish_date"] > now():
            abort(403)
        sub = db.execute("SELECT * FROM submissions WHERE assignment_id=? AND student_id=?",
                         (aid, g.user["id"])).fetchone()
        return render_template("assignment_student_view.html", a=a, sub=sub, status=status_of(sub), now=now())
    course_access(a["course_id"])
    subs = {s["student_id"]: s for s in db.execute("SELECT * FROM submissions WHERE assignment_id=?", (aid,))}
    roster = [(st, subs.get(st["id"])) for st in enrolled_students(a["course_id"])]
    return render_template("assignment_teacher_view.html", a=a, roster=roster, status_of=status_of)


@bp.route("/assignments/<int:aid>/submit", methods=["POST"])
@roles_required("student")
def submit(aid):
    db = get_db()
    a = get_assignment(aid)
    if not is_enrolled(a["course_id"], g.user["id"]) or a["publish_date"] > now():
        abort(403)
    old = db.execute("SELECT * FROM submissions WHERE assignment_id=? AND student_id=?", (aid, g.user["id"])).fetchone()
    if old and old["released"]:
        flash("Marks have been released; this submission can no longer be changed.", "error")
        return redirect(url_for("assignments.view", aid=aid))
    try:
        name, stored = save_upload(request.files.get("file"), "submissions", required=True)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("assignments.view", aid=aid))
    ts = now()
    late = 1 if ts > a["deadline"] else 0
    try:
        if old:
            db.execute("UPDATE submissions SET file_name=?,file_path=?,submitted_at=?,is_late=?,marks=NULL,"
                       "feedback='',released=0,graded_by=NULL,graded_at=NULL WHERE id=?",
                       (name, stored, ts, late, old["id"]))
        else:
            db.execute("INSERT INTO submissions(assignment_id,student_id,file_name,file_path,submitted_at,is_late) "
                       "VALUES(?,?,?,?,?,?)", (aid, g.user["id"], name, stored, ts, late))
        audit("assignment_submit", "assignment", "%s%s" % (a["title"], " (LATE)" if late else ""), db=db)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        remove_upload("submissions", stored)
        flash("Submission failed; nothing was saved. Please try again.", "error")
        return redirect(url_for("assignments.view", aid=aid))
    if old:
        remove_upload("submissions", old["file_path"])
    flash("Submission received%s." % (" (marked LATE)" if late else ""), "success")
    return redirect(url_for("assignments.view", aid=aid))


@bp.route("/submissions/<int:sid>/grade", methods=["POST"])
@roles_required("teacher")
def grade(sid):
    db = get_db()
    s = db.execute("SELECT s.*, a.max_marks, a.course_id, a.title FROM submissions s "
                   "JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    course_access(s["course_id"], write=True)
    marks = to_float(request.form.get("marks"))
    if marks is None or marks < 0 or marks > s["max_marks"]:
        flash("Marks must be a number between 0 and %g." % s["max_marks"], "error")
    else:
        release = 1 if request.form.get("release") else 0
        db.execute("UPDATE submissions SET marks=?,feedback=?,released=?,graded_by=?,graded_at=? WHERE id=?",
                   (marks, request.form.get("feedback", "").strip()[:2000], release, g.user["id"], now(), sid))
        audit("assignment_grade", "submission", "assignment '%s' student %s marks=%g released=%d" % (
            s["title"], s["student_id"], marks, release), db=db)
        if release:
            dbm.notify([s["student_id"]], "Marks released for assignment: %s" % s["title"],
                       url_for("assignments.view", aid=s["assignment_id"]), db=db)
        db.commit()
        flash("Evaluation saved.", "success")
    return redirect(url_for("assignments.view", aid=s["assignment_id"]))


@bp.route("/files/<kind>/<int:fid>")
@login_required
def download(kind, fid):
    db = get_db()
    u = g.user
    if kind == "assignment":
        a = get_assignment(fid)
        if not a["attachment_path"]:
            abort(404)
        if u["role"] == "student":
            if not is_enrolled(a["course_id"], u["id"]) or a["publish_date"] > now():
                abort(403)
        else:
            course_access(a["course_id"])
        folder, stored, name = "assignments", a["attachment_path"], a["attachment_name"]
    elif kind == "submission":
        s = db.execute("SELECT s.*, a.course_id FROM submissions s JOIN assignments a ON a.id=s.assignment_id "
                       "WHERE s.id=?", (fid,)).fetchone()
        if not s:
            abort(404)
        if u["role"] == "student":
            if s["student_id"] != u["id"]:
                abort(403)
        else:
            course_access(s["course_id"])
        folder, stored, name = "submissions", s["file_path"], s["file_name"]
    elif kind == "announcement":
        from .comms import can_view_announcement
        an = db.execute("SELECT * FROM announcements WHERE id=?", (fid,)).fetchone()
        if not an or not an["attachment_path"] or not can_view_announcement(an):
            abort(404)
        folder, stored, name = "announcements", an["attachment_path"], an["attachment_name"]
    else:
        abort(404)
    return send_from_directory(upload_dir(folder), stored, as_attachment=True, download_name=name)
