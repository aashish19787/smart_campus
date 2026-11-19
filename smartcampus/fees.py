"""Fee records, payments, receipts and payment history."""
import sqlite3

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import db as dbm
from .db import audit, get_db, now, to_float, valid_date
from .pdfutil import receipt_pdf
from .security import login_required, roles_required

bp = Blueprint("fees", __name__)
METHODS = ("Cash", "Bank Transfer", "Cheque", "Mobile Wallet", "Card (POS)")
FEE_SELECT = ("SELECT f.*, s.student_no, u.name AS student_name, "
              "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.fee_id=f.id),0) AS paid "
              "FROM fee_records f JOIN users u ON u.id=f.student_id LEFT JOIN students s ON s.user_id=f.student_id ")


def refresh_status(db, fee_id):
    f = db.execute("SELECT amount, COALESCE((SELECT SUM(amount) FROM payments WHERE fee_id=?),0) AS paid "
                   "FROM fee_records WHERE id=?", (fee_id, fee_id)).fetchone()
    paid = round(f["paid"], 2)
    status = "Paid" if paid + 1e-9 >= f["amount"] else ("Partially Paid" if paid > 0 else "Unpaid")
    db.execute("UPDATE fee_records SET status=? WHERE id=?", (status, fee_id))
    return status


def get_fee(fee_id):
    f = get_db().execute(FEE_SELECT + "WHERE f.id=?", (fee_id,)).fetchone()
    if not f:
        abort(404)
    if g.user["role"] == "student" and f["student_id"] != g.user["id"]:
        abort(403)
    if g.user["role"] not in ("student", "finance", "admin"):
        abort(403)
    return f


@bp.route("/fees")
@roles_required("finance", "admin", "student")
def index():
    db = get_db()
    if g.user["role"] == "student":
        rows = db.execute(FEE_SELECT + "WHERE f.student_id=? ORDER BY f.due_date DESC", (g.user["id"],)).fetchall()
        pays = db.execute("SELECT p.*, f.fee_type, t.name AS staff FROM payments p JOIN fee_records f ON f.id=p.fee_id "
                          "LEFT JOIN users t ON t.id=p.recorded_by WHERE f.student_id=? ORDER BY p.id DESC",
                          (g.user["id"],)).fetchall()
        outstanding = sum(r["amount"] - r["paid"] for r in rows)
        return render_template("fees_student.html", rows=rows, pays=pays, outstanding=outstanding, today=dbm.today())
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    sql, args = FEE_SELECT + "WHERE 1=1", []
    if q:
        sql += " AND (s.student_no LIKE ? OR u.name LIKE ? OR u.email LIKE ?)"
        args += ["%" + q + "%"] * 3
    if status in ("Paid", "Partially Paid", "Unpaid"):
        sql += " AND f.status=?"
        args.append(status)
    rows = db.execute(sql + " ORDER BY f.due_date DESC, f.id DESC LIMIT 500", args).fetchall()
    return render_template("fees.html", rows=rows, q=q, status=status, today=dbm.today())


@bp.route("/fees/new", methods=["GET", "POST"])
@roles_required("finance")
def new_fee():
    db = get_db()
    progs = db.execute("SELECT * FROM programs ORDER BY code").fetchall()
    if request.method == "POST":
        no = request.form.get("student_no", "").strip()
        prog = request.form.get("program_id", "")
        ftype = request.form.get("fee_type", "").strip()
        amount = to_float(request.form.get("amount"))
        due = request.form.get("due_date", "").strip()
        err, targets = None, []
        if not ftype or len(ftype) > 60:
            err = "Fee type is required."
        elif amount is None or amount <= 0 or amount > 100000000:
            err = "Enter a valid payable amount greater than zero."
        elif not valid_date(due):
            err = "Enter a valid due date."
        elif no:
            r = db.execute("SELECT user_id FROM students WHERE student_no=? COLLATE NOCASE", (no,)).fetchone()
            if r:
                targets = [r["user_id"]]
            else:
                err = "Student ID not found."
        elif prog.isdigit():
            targets = [r["user_id"] for r in db.execute(
                "SELECT s.user_id FROM students s JOIN users u ON u.id=s.user_id WHERE s.program_id=? AND u.active=1",
                (int(prog),))]
            if not targets:
                err = "No active students in that program."
        else:
            err = "Enter a Student ID or choose a program."
        if err:
            flash(err, "error")
        else:
            db.executemany("INSERT INTO fee_records(student_id,fee_type,amount,due_date,created_by,created_at) "
                           "VALUES(?,?,?,?,?,?)", [(t, ftype, round(amount, 2), due, g.user["id"], now()) for t in targets])
            audit("fee_create", "fee", "%s %.2f due %s for %d student(s)" % (ftype, amount, due, len(targets)), db=db)
            dbm.notify(targets, "New fee posted: %s %.2f due %s" % (ftype, amount, due), url_for("fees.index"), db=db)
            db.commit()
            flash("Fee record(s) created for %d student(s)." % len(targets), "success")
            return redirect(url_for("fees.index"))
    return render_template("fee_form.html", progs=progs, f=None, today=dbm.today())


@bp.route("/fees/<int:fid>", methods=["GET", "POST"])
@roles_required("finance", "admin", "student")
def detail(fid):
    db = get_db()
    f = get_fee(fid)
    if request.method == "POST":
        if g.user["role"] != "finance":
            abort(403)
        if request.form.get("action") == "edit":
            ftype = request.form.get("fee_type", "").strip()
            amount = to_float(request.form.get("amount"))
            due = request.form.get("due_date", "")
            if not ftype or amount is None or amount < f["paid"] or amount <= 0 or not valid_date(due):
                flash("Enter valid details; the amount cannot be below what has already been paid.", "error")
            else:
                db.execute("UPDATE fee_records SET fee_type=?, amount=?, due_date=? WHERE id=?",
                           (ftype, round(amount, 2), due, fid))
                refresh_status(db, fid)
                audit("fee_update", "fee", "fee %d: %s %.2f due %s" % (fid, ftype, amount, due), db=db)
                db.commit()
                flash("Fee record updated.", "success")
        else:
            amount = to_float(request.form.get("amount"))
            method = request.form.get("method", "")
            paid_on = request.form.get("paid_on", dbm.today()).strip()
            balance = round(f["amount"] - f["paid"], 2)
            err = None
            if amount is None or amount <= 0:
                err = "Enter a valid payment amount."
            elif round(amount, 2) > balance + 1e-9:
                err = "Payment exceeds the outstanding balance (%.2f)." % balance
            elif method not in METHODS:
                err = "Select a payment method."
            elif not valid_date(paid_on) or paid_on > dbm.today():
                err = "Payment date must be a valid date that is not in the future."
            if err:
                flash(err, "error")
            else:
                try:
                    cur = db.execute("INSERT INTO payments(fee_id,amount,paid_on,method,reference,recorded_by,created_at) "
                                     "VALUES(?,?,?,?,?,?,?)",
                                     (fid, round(amount, 2), paid_on, method, request.form.get("reference", "").strip()[:80],
                                      g.user["id"], now()))
                    pid = cur.lastrowid
                    rno = "RCPT-%s-%06d" % (paid_on[:4], pid)
                    db.execute("UPDATE payments SET receipt_no=? WHERE id=?", (rno, pid))
                    status = refresh_status(db, fid)
                    audit("payment_record", "payment", "%s %.2f %s fee %d (%s)" % (rno, amount, f["student_no"], fid, status),
                          db=db)
                    dbm.notify([f["student_id"]], "Payment of %.2f received (%s). Receipt %s." % (amount, f["fee_type"], rno),
                               url_for("fees.index"), db=db)
                    db.commit()
                    flash("Payment recorded. Receipt %s generated." % rno, "success")
                except sqlite3.Error:
                    db.rollback()
                    flash("The payment could not be saved and was not recorded. Please try again.", "error")
        return redirect(url_for("fees.detail", fid=fid))
    pays = db.execute("SELECT p.*, t.name AS staff FROM payments p LEFT JOIN users t ON t.id=p.recorded_by "
                      "WHERE p.fee_id=? ORDER BY p.id", (fid,)).fetchall()
    return render_template("fee_detail.html", f=f, pays=pays, methods=METHODS, today=dbm.today())


@bp.route("/receipts/<int:pid>")
@roles_required("finance", "admin", "student")
def receipt(pid):
    db = get_db()
    p = db.execute("SELECT p.*, f.fee_type, f.amount AS fee_amount, f.student_id, u.name AS student_name, s.student_no, "
                   "t.name AS staff_name, (SELECT SUM(x.amount) FROM payments x WHERE x.fee_id=p.fee_id AND x.id<=p.id) "
                   "AS paid_total FROM payments p JOIN fee_records f ON f.id=p.fee_id JOIN users u ON u.id=f.student_id "
                   "LEFT JOIN students s ON s.user_id=f.student_id LEFT JOIN users t ON t.id=p.recorded_by "
                   "WHERE p.id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    if g.user["role"] == "student" and p["student_id"] != g.user["id"]:
        abort(403)
    d = dict(p)
    d["staff_name"] = d["staff_name"] or "-"
    d["student_no"] = d["student_no"] or "-"
    d["balance"] = round(d["fee_amount"] - d["paid_total"], 2)
    pdf = receipt_pdf(d, dbm.get_setting("college_name"))
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=%s.pdf" % p["receipt_no"]})
