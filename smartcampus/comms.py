"""Announcements and in-system notifications."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import db as dbm
from .db import audit, get_db, now
from .security import STAFF, login_required, remove_upload, roles_required, save_upload

bp = Blueprint("comms", __name__)
AUDIENCES = {"all": "Everyone", "students": "Students", "teachers": "Teachers", "staff": "All staff"}
CATEGORIES = ("General", "Academic", "Examination", "Event", "Fees", "Holiday")


def audience_filter(role):
    """SQL fragment + args limiting announcements to those a role may read."""
    if role == "student":
        return "a.audience IN ('all','students')"
    if role == "teacher":
        return "a.audience IN ('all','teachers','staff')"
    return "a.audience IN ('all','staff')"


def can_view_announcement(a):
    u = g.user
    if u["role"] == "admin" or a["created_by"] == u["id"]:
        return True
    if a["status"] == "draft":
        return False
    if a["status"] == "published" and (a["publish_date"] or "") > now():
        return False
    ok = {"student": ("all", "students"), "teacher": ("all", "teachers", "staff")}.get(u["role"], ("all", "staff"))
    return a["audience"] in ok


def audience_user_ids(db, audience):
    sql = "SELECT id FROM users WHERE active=1"
    if audience == "students":
        sql += " AND role='student'"
    elif audience == "teachers":
        sql += " AND role='teacher'"
    elif audience == "staff":
        sql += " AND role!='student'"
    return [r["id"] for r in db.execute(sql)]


@bp.route("/announcements")
@login_required
def announcements():
    db = get_db()
    u = g.user
    cat = request.args.get("category", "")
    show = request.args.get("show", "published")
    manage = u["role"] in STAFF
    args = []
    if manage and show in ("draft", "archived"):
        sql = "SELECT a.*, t.name AS author FROM announcements a LEFT JOIN users t ON t.id=a.created_by WHERE a.status=?"
        args.append(show)
        if u["role"] != "admin":
            sql += " AND a.created_by=?"
            args.append(u["id"])
    else:
        show = "published"
        sql = ("SELECT a.*, t.name AS author FROM announcements a LEFT JOIN users t ON t.id=a.created_by "
               "WHERE a.status='published' AND a.publish_date<=? AND " + audience_filter(u["role"]))
        args.append(now())
        if u["role"] == "admin":
            sql = ("SELECT a.*, t.name AS author FROM announcements a LEFT JOIN users t ON t.id=a.created_by "
                   "WHERE a.status='published'")
            args = []
    if cat:
        sql += " AND a.category=?"
        args.append(cat)
    rows = db.execute(sql + " ORDER BY a.publish_date DESC, a.id DESC LIMIT 200", args).fetchall()
    return render_template("announcements.html", rows=rows, cats=CATEGORIES, cat=cat, show=show, manage=manage,
                           audiences=AUDIENCES)


@bp.route("/announcements/<int:aid>")
@login_required
def view_announcement(aid):
    a = get_db().execute("SELECT a.*, t.name AS author FROM announcements a LEFT JOIN users t ON t.id=a.created_by "
                         "WHERE a.id=?", (aid,)).fetchone()
    if not a or not can_view_announcement(a):
        abort(404)
    return render_template("announcement_view.html", a=a, audiences=AUDIENCES,
                           can_manage=g.user["role"] == "admin" or a["created_by"] == g.user["id"])


def _owned(aid):
    a = get_db().execute("SELECT * FROM announcements WHERE id=?", (aid,)).fetchone()
    if not a:
        abort(404)
    if g.user["role"] != "admin" and a["created_by"] != g.user["id"]:
        abort(403)
    return a


def _publish(db, a_id):
    a = db.execute("SELECT * FROM announcements WHERE id=?", (a_id,)).fetchone()
    db.execute("UPDATE announcements SET status='published', publish_date=?, updated_at=? WHERE id=?",
               (now(), now(), a_id))
    dbm.notify(audience_user_ids(db, a["audience"]), "Announcement: " + a["title"],
               url_for("comms.view_announcement", aid=a_id), email=True, db=db)
    audit("announcement_publish", "announcement", a["title"], db=db)


@bp.route("/announcements/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def new():
    return _form(None)


@bp.route("/announcements/<int:aid>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def edit(aid):
    return _form(_owned(aid))


def _form(a):
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        cat = request.form.get("category", "General")
        aud = request.form.get("audience", "all")
        err = None
        if not title or len(title) > 150:
            err = "Title is required (max 150 characters)."
        elif not body:
            err = "Description is required."
        elif aud not in AUDIENCES:
            err = "Invalid audience."
        elif cat not in CATEGORIES:
            cat = "General"
        name, stored = (a["attachment_name"], a["attachment_path"]) if a else (None, None)
        if not err:
            try:
                n2, s2 = save_upload(request.files.get("attachment"), "announcements")
                if s2:
                    remove_upload("announcements", stored)
                    name, stored = n2, s2
            except ValueError as e:
                err = str(e)
        if err:
            flash(err, "error")
        else:
            if a:
                db.execute("UPDATE announcements SET title=?,body=?,category=?,audience=?,attachment_name=?,"
                           "attachment_path=?,updated_at=? WHERE id=?", (title, body, cat, aud, name, stored, now(), a["id"]))
                aid = a["id"]
                audit("announcement_update", "announcement", title, db=db)
            else:
                cur = db.execute("INSERT INTO announcements(title,body,category,audience,attachment_name,attachment_path,"
                                 "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (title, body, cat, aud, name, stored, g.user["id"], now(), now()))
                aid = cur.lastrowid
                audit("announcement_create", "announcement", title, db=db)
            if request.form.get("publish_now") and (not a or a["status"] != "published"):
                _publish(db, aid)
            db.commit()
            flash("Announcement saved.", "success")
            return redirect(url_for("comms.view_announcement", aid=aid))
    return render_template("announcement_form.html", a=a, cats=CATEGORIES, audiences=AUDIENCES)


@bp.route("/announcements/<int:aid>/<action>", methods=["POST"])
@roles_required(*STAFF)
def change_status(aid, action):
    db = get_db()
    a = _owned(aid)
    if action == "publish" and a["status"] != "published":
        _publish(db, aid)
        flash("Announcement published.", "success")
    elif action == "archive" and a["status"] == "published":
        db.execute("UPDATE announcements SET status='archived', updated_at=? WHERE id=?", (now(), aid))
        audit("announcement_archive", "announcement", a["title"], db=db)
        flash("Announcement archived.", "success")
    else:
        flash("That action is not available for this announcement.", "error")
    db.commit()
    return redirect(url_for("comms.view_announcement", aid=aid))


@bp.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    db = get_db()
    if request.method == "POST":
        nid = request.form.get("id", "")
        if nid.isdigit():
            db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (int(nid), g.user["id"]))
        else:
            db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
        db.commit()
        return redirect(url_for("comms.notifications"))
    rows = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 200",
                      (g.user["id"],)).fetchall()
    return render_template("notifications.html", rows=rows)
