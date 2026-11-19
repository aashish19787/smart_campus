"""Attendance, result, fee and audit reports - HTML view, CSV and PDF downloads."""
import csv
import io

from flask import Blueprint, Response, abort, g, render_template, request

from . import db as dbm
from .attendance import attendance_stats
from .db import audit, get_db, get_float, grade_for, valid_date
from .pdfutil import table_pdf
from .security import login_required

bp = Blueprint("reports", __name__)

ACCESS = {
    "attendance": ("admin", "teacher", "student"),
    "results": ("admin", "examofficer"),
    "fees": ("admin", "finance"),
    "outstanding": ("admin", "finance"),
    "audit": ("admin",),
}
TITLES = {"attendance": "Attendance Report", "results": "Examination Result Report",
          "fees": "Fee Payment Report", "outstanding": "Outstanding Balance Report", "audit": "Activity Log Report"}


def build(kind):
    """Return (headers, rows, subtitle, error) for the report; enforces row-level permissions."""
    db = get_db()
    u = g.user
    a = request.args
    if kind == "attendance":
        d_from = a.get("from", "") if valid_date(a.get("from", "")) else None
        d_to = a.get("to", "") if valid_date(a.get("to", "")) else None
        cid = a.get("course", type=int)
        sno = a.get("student", "").strip()
        sql = ("SELECT e.course_id, e.student_id, c.code, c.name AS cname, u.name, s.student_no FROM enrollments e "
               "JOIN courses c ON c.id=e.course_id JOIN users u ON u.id=e.student_id "
               "LEFT JOIN students s ON s.user_id=e.student_id WHERE 1=1")
        args = []
        if u["role"] == "teacher":
            sql += " AND c.teacher_id=?"
            args.append(u["id"])
        elif u["role"] == "student":
            sql += " AND e.student_id=?"
            args.append(u["id"])
        if cid:
            sql += " AND e.course_id=?"
            args.append(cid)
        if sno and u["role"] != "student":
            sql += " AND (s.student_no LIKE ? OR u.name LIKE ?)"
            args += ["%" + sno + "%"] * 2
        thr = get_float("attendance_threshold")
        rows = []
        for r in db.execute(sql + " ORDER BY c.code, s.student_no LIMIT 3000", args):
            st = attendance_stats(db, r["student_id"], r["course_id"], d_from, d_to)
            rows.append([r["code"], r["student_no"] or "", r["name"], st["present"], st["late"], st["absent"], st["total"],
                         "-" if st["pct"] is None else "%.2f%%" % st["pct"],
                         "LOW" if st["pct"] is not None and st["pct"] < thr else "OK"])
        sub = "Period: %s to %s | Threshold: %g%%" % (d_from or "start", d_to or "today", thr)
        return ["Course", "Student ID", "Name", "Present", "Late", "Absent", "Sessions", "Attendance", "Status"], rows, sub
    if kind == "results":
        eid = a.get("exam", type=int)
        sql = ("SELECT c.code, e.exam_type, e.exam_date, e.max_marks, e.status, s.student_no, u.name, m.marks "
               "FROM marks m JOIN exams e ON e.id=m.exam_id JOIN courses c ON c.id=e.course_id "
               "JOIN users u ON u.id=m.student_id LEFT JOIN students s ON s.user_id=m.student_id "
               "WHERE e.status IN ('approved','published')")
        args = []
        if eid:
            sql += " AND e.id=?"
            args.append(eid)
        rows = []
        for r in db.execute(sql + " ORDER BY e.exam_date DESC, c.code, s.student_no LIMIT 5000", args):
            pct = round(r["marks"] / r["max_marks"] * 100, 2)
            rows.append([r["code"], r["exam_type"], r["exam_date"], r["student_no"] or "", r["name"],
                         "%g" % r["marks"], "%g" % r["max_marks"], "%.2f%%" % pct, grade_for(pct, db), r["status"]])
        return ["Course", "Exam", "Date", "Student ID", "Name", "Marks", "Max", "Percentage", "Grade", "Status"], rows, ""
    if kind in ("fees", "outstanding"):
        status = a.get("status", "")
        sql = ("SELECT s.student_no, u.name, f.fee_type, f.amount, f.due_date, f.status, "
               "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.fee_id=f.id),0) AS paid "
               "FROM fee_records f JOIN users u ON u.id=f.student_id LEFT JOIN students s ON s.user_id=f.student_id "
               "WHERE 1=1")
        args = []
        if kind == "outstanding":
            sql += " AND f.status!='Paid'"
        elif status in ("Paid", "Partially Paid", "Unpaid"):
            sql += " AND f.status=?"
            args.append(status)
        rows, tot_amt, tot_paid = [], 0.0, 0.0
        for r in db.execute(sql + " ORDER BY s.student_no, f.due_date LIMIT 5000", args):
            bal = round(r["amount"] - r["paid"], 2)
            tot_amt += r["amount"]
            tot_paid += r["paid"]
            rows.append([r["student_no"] or "", r["name"], r["fee_type"], "%.2f" % r["amount"], "%.2f" % r["paid"],
                         "%.2f" % bal, r["due_date"], r["status"]])
        rows.append(["", "TOTAL", "", "%.2f" % tot_amt, "%.2f" % tot_paid, "%.2f" % (tot_amt - tot_paid), "", ""])
        return ["Student ID", "Name", "Fee type", "Payable", "Paid", "Outstanding", "Due date", "Status"], rows, ""
    if kind == "audit":
        rows = [[r["created_at"], r["user_name"], r["action"], r["entity"], r["detail"]] for r in
                db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 5000")]
        return ["Date/Time", "User", "Action", "Entity", "Detail"], rows, ""
    abort(404)


@bp.route("/reports")
@login_required
def index():
    kinds = [(k, TITLES[k]) for k, roles in ACCESS.items() if g.user["role"] in roles]
    if not kinds:
        abort(403)
    return render_template("reports_index.html", kinds=kinds)


@bp.route("/reports/<kind>")
@login_required
def show(kind):
    if kind not in ACCESS:
        abort(404)
    if g.user["role"] not in ACCESS[kind]:
        abort(403)
    headers, rows, sub = build(kind)
    fmt = request.args.get("format", "html")
    if fmt in ("csv", "pdf"):
        audit("report_download", "report", "%s.%s" % (kind, fmt))
        get_db().commit()
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(headers)
        for r in rows:
            w.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" and c else c for c in r])  # CSV-injection safe
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=%s.csv" % kind})
    if fmt == "pdf":
        pdf = table_pdf(TITLES[kind], headers, rows, sub, dbm.get_setting("college_name"))
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=%s.pdf" % kind})
    db = get_db()
    courses = []
    if kind == "attendance":
        if g.user["role"] == "teacher":
            courses = db.execute("SELECT id,code,name FROM courses WHERE teacher_id=? ORDER BY code", (g.user["id"],)).fetchall()
        elif g.user["role"] == "admin":
            courses = db.execute("SELECT id,code,name FROM courses ORDER BY code").fetchall()
        else:
            courses = db.execute("SELECT c.id,c.code,c.name FROM courses c JOIN enrollments e ON e.course_id=c.id "
                                 "WHERE e.student_id=? ORDER BY c.code", (g.user["id"],)).fetchall()
    exams = db.execute("SELECT e.id, c.code, e.exam_type, e.exam_date FROM exams e JOIN courses c ON c.id=e.course_id "
                       "WHERE e.status IN ('approved','published') ORDER BY e.exam_date DESC").fetchall() \
        if kind == "results" else []
    return render_template("report_view.html", kind=kind, title=TITLES[kind], headers=headers, rows=rows[:1000],
                           total=len(rows), sub=sub, courses=courses, exams=exams, args=request.args)
