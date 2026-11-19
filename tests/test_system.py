import io
import re

import pytest

from smartcampus import db as dbm

ALL_PAGES = {
    "admin": ["/", "/admin/users", "/admin/students", "/admin/programs", "/courses", "/attendance", "/exams", "/fees",
              "/announcements", "/reports", "/admin/audit", "/admin/settings", "/admin/backups", "/admin/outbox",
              "/admin/students/6", "/admin/courses/1", "/admin/users/2", "/assignments", "/notifications", "/profile",
              "/attendance/course/1", "/reports/attendance", "/reports/results", "/reports/fees", "/reports/outstanding",
              "/reports/audit", "/results"],
    "teacher": ["/", "/courses", "/attendance", "/attendance/take/1", "/attendance/course/1", "/assignments",
                "/assignments/new", "/assignments/1", "/exams", "/exams/1/marks", "/announcements", "/reports",
                "/reports/attendance", "/notifications", "/profile"],
    "student": ["/", "/courses", "/attendance", "/attendance/history/1", "/assignments", "/assignments/1", "/exams",
                "/results", "/fees", "/announcements", "/notifications", "/profile", "/reports/attendance"],
    "finance": ["/", "/fees", "/fees/new", "/fees/1", "/reports", "/reports/fees", "/reports/outstanding", "/announcements"],
    "examofficer": ["/", "/exams", "/exams/new", "/exams/1/edit", "/exams/1/marks", "/results", "/reports",
                    "/reports/results", "/announcements"],
}


@pytest.mark.parametrize("role,fixture", [("admin", "admin"), ("teacher", "teacher"), ("student", "student"),
                                          ("finance", "finance"), ("examofficer", "officer")])
def test_every_page_renders(role, fixture, request):
    c = request.getfixturevalue(fixture)
    for url in ALL_PAGES[role]:
        r = c.get(url)
        assert r.status_code == 200, (role, url, r.status_code)


def test_login_logout_and_errors(anon):
    assert anon.get("/").status_code == 302
    r = anon.login("admin@hcit.edu.np", "wrong")
    assert r.status_code == 401
    r = anon.login("admin@hcit.edu.np", "Admin12345")
    assert r.status_code == 302
    assert anon.get("/").status_code == 200
    assert anon.post("/logout").status_code == 302
    assert anon.get("/").status_code == 302


def test_csrf_enforced(app):
    c = app.test_client()
    assert c.post("/login", data={"email": "a@b.c", "password": "x"}).status_code == 400


def test_inactive_account_denied(app, admin, anon):
    r = admin.post("/admin/users/2", {"name": "Ramesh Sharma", "email": "teacher1@hcit.edu.np", "role": "teacher",
                                      "phone": ""})  # active unchecked -> deactivated
    assert r.status_code == 302
    assert anon.login("teacher1@hcit.edu.np").status_code == 403


def test_lockout_after_failures(anon):
    for _ in range(5):
        anon.login("student1@hcit.edu.np", "bad")
    assert anon.login("student1@hcit.edu.np", "Password123").status_code == 429


def test_password_recovery(app, anon):
    anon.token()
    anon.post("/forgot", {"email": "student1@hcit.edu.np"})
    with app.app_context():
        conn = dbm.connect(app.config["DATABASE"])
        body = conn.execute("SELECT body FROM email_outbox ORDER BY id DESC").fetchone()["body"]
    token = re.search(r"/reset/(\S+)", body).group(1)
    assert anon.get("/reset/" + token).status_code == 200
    assert anon.post("/reset/" + token, {"password": "short", "confirm": "short"}).status_code == 400
    assert anon.post("/reset/" + token, {"password": "NewPass123", "confirm": "NewPass123"}).status_code == 302
    assert anon.login("student1@hcit.edu.np", "NewPass123").status_code == 302
    assert anon.get("/reset/" + token).status_code == 302  # one-time use


def test_rbac(student, teacher, finance, officer, admin):
    assert student.get("/admin/users").status_code == 403
    assert student.get("/attendance/take/1").status_code == 403
    assert student.get("/reports/audit").status_code == 403
    assert student.get("/fees/new").status_code == 403
    assert teacher.get("/admin/settings").status_code == 403
    assert teacher.get("/fees").status_code == 403
    assert finance.get("/exams").status_code == 403
    assert officer.get("/fees").status_code == 403
    assert admin.get("/attendance/take/1").status_code == 403  # admin cannot record attendance
    assert student.post("/exams/1/workflow/publish").status_code == 403
    assert teacher.post("/exams/1/workflow/publish").status_code == 403


def test_student_registration_unique(admin):
    d = {"name": "New Stu", "email": "new@hcit.edu.np", "password": "Secret123", "program_id": 1,
         "enrollment_date": "2026-02-01", "phone": "1", "address": "x"}
    assert admin.post("/admin/students", d).status_code == 302
    r = admin.post("/admin/students", d)
    assert r.status_code == 200 and b"already exists" in r.data
    r = admin.get("/admin/students?q=New+Stu")
    assert b"HCIT-2026-0006" in r.data
    assert b"New Stu" in admin.get("/admin/students?q=new@hcit").data


def test_teacher_course_isolation(teacher, teacher2):
    assert teacher2.get("/attendance/take/1").status_code == 403   # CS101 belongs to teacher1
    assert teacher2.get("/assignments/1").status_code == 403
    assert teacher2.get("/exams/1/marks").status_code == 403
    assert teacher.get("/attendance/take/3").status_code == 403


def test_attendance_flow(app, teacher, student3):
    date = "2026-01-05"
    r = teacher.post("/attendance/take/1", {"save": 1, "date": date, "status_6": "Present"})
    assert b"Incomplete" in r.data
    form = {"save": 1, "date": date, "status_6": "Absent", "status_7": "Absent", "status_8": "Absent"}
    assert teacher.post("/attendance/take/1", form).status_code == 302
    # existing session -> correction needs reason
    form["status_6"] = "Present"
    assert b"reason is required" in teacher.post("/attendance/take/1", form).data
    form["reason"] = "Marked wrongly"
    assert teacher.post("/attendance/take/1", form).status_code == 302
    r = teacher.post("/attendance/take/1", {"save": 1, "date": "2999-01-01", "status_6": "Present", "status_7": "Present",
                                           "status_8": "Present"})
    assert b"future" in r.data
    # student 3 has low attendance -> warning notification
    assert b"Low attendance" in student3.get("/notifications").data
    page = student3.get("/attendance").data
    assert b"bad" in page
    conn = dbm.connect(app.config["DATABASE"])
    from smartcampus.attendance import attendance_stats
    with app.app_context():
        s = attendance_stats(conn, 8, 1)
    assert s["total"] == 11 and s["pct"] is not None
    assert conn.execute("SELECT COUNT(*) c FROM audit_log WHERE action='attendance_correct'").fetchone()["c"] == 1


def test_assignment_submission_and_grading(app, teacher, student):
    data = {"file": (io.BytesIO(b"hello"), "answer.pdf")}
    r = student.c.post("/assignments/1/submit", data={**data, "_csrf": student.token()},
                       content_type="multipart/form-data")
    assert r.status_code == 302
    r = student.get("/assignments/1")
    assert b"Submitted" in r.data and b"answer.pdf" in r.data
    bad = student.c.post("/assignments/1/submit", data={"file": (io.BytesIO(b"x"), "evil.exe"), "_csrf": student.token()},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"not allowed" in bad.data
    assert student.get("/files/submission/1").status_code == 200
    assert teacher.get("/files/submission/1").status_code == 200
    # grading validation + release
    assert b"between 0 and 20" in teacher.post("/submissions/1/grade", {"marks": "99"}, follow_redirects=True).data
    assert teacher.post("/submissions/1/grade", {"marks": "18", "feedback": "Good", "release": "1"}).status_code == 302
    page = student.get("/assignments/1").data
    assert b"18 / 20" in page and b"Good" in page


def test_late_submission_flag(app, teacher, student):
    r = teacher.c.post("/assignments/new", data={"_csrf": teacher.token(), "course_id": "1", "title": "Late one",
                       "description": "d", "publish_date": "2026-01-01T09:00", "deadline": "2026-01-02T09:00",
                       "max_marks": "10"})
    assert r.status_code == 302
    aid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    student.c.post("/assignments/%d/submit" % aid, data={"file": (io.BytesIO(b"x"), "a.txt"), "_csrf": student.token()},
                   content_type="multipart/form-data")
    assert b"Late" in student.get("/assignments/%d" % aid).data


def test_exam_workflow_and_results(app, teacher, officer, student, student3):
    assert student.get("/results").status_code == 200
    # marks validation
    r = teacher.post("/exams/1/marks", {"marks_6": "150"})
    assert b"between 0 and 100" in r.data
    assert teacher.post("/exams/1/marks", {"marks_6": "85", "marks_7": "72", "marks_8": "40"}).status_code == 302
    # cannot publish before verify/approve
    officer.post("/exams/1/workflow/publish")
    assert b"unpublished" not in student.get("/results").data
    assert b"85" not in student.get("/results").data
    officer.post("/exams/1/workflow/verify")
    officer.post("/exams/1/workflow/approve")
    assert b"85" not in student.get("/results").data  # approved but not published
    officer.post("/exams/1/workflow/publish")
    page = student.get("/results").data
    assert b"85" in page and b"<b>A</b>" in page
    assert student.get("/results/download").data[:4] == b"%PDF"
    assert b"Results published" in student.get("/notifications").data
    # student cannot see others' results; published marks locked for teachers
    assert teacher.post("/exams/1/marks", {"marks_6": "10"}).status_code == 403
    assert b"40" in student3.get("/results").data and b"85" not in student3.get("/results").data


def test_marks_correction_requires_reason_and_audit(app, teacher, officer):
    teacher.post("/exams/1/marks", {"marks_6": "50", "marks_7": "60", "marks_8": "70"})
    officer.post("/exams/1/workflow/verify")
    r = teacher.post("/exams/1/marks", {"marks_6": "55"})
    assert b"reason is required" in r.data
    assert teacher.post("/exams/1/marks", {"marks_6": "55", "reason": "Recheck"}).status_code == 302
    conn = dbm.connect(app.config["DATABASE"])
    assert conn.execute("SELECT status FROM exams WHERE id=1").fetchone()["status"] == "entered"  # needs re-verification
    row = conn.execute("SELECT * FROM mark_audit WHERE old_marks=50 AND new_marks=55").fetchone()
    assert row and row["reason"] == "Recheck"


def test_verify_requires_all_marks(officer, teacher):
    teacher.post("/exams/1/marks", {"marks_6": "50"})
    r = officer.post("/exams/1/workflow/verify", follow_redirects=True)
    assert b"no marks entered" in r.data


def test_fee_payment_and_receipt(app, finance, student):
    r = finance.post("/fees/1", {"action": "pay", "amount": "99999", "method": "Cash", "paid_on": "2026-01-10"})
    assert r.status_code == 302
    conn = dbm.connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"] == 0  # overpayment rejected
    finance.post("/fees/1", {"action": "pay", "amount": "20000", "method": "Cash", "paid_on": "2026-01-10"})
    f = conn.execute("SELECT status FROM fee_records WHERE id=1").fetchone()
    assert f["status"] == "Partially Paid"
    finance.post("/fees/1", {"action": "pay", "amount": "30000", "method": "Bank Transfer", "paid_on": "2026-01-11"})
    assert conn.execute("SELECT status FROM fee_records WHERE id=1").fetchone()["status"] == "Paid"
    rn = [r["receipt_no"] for r in conn.execute("SELECT receipt_no FROM payments")]
    assert len(set(rn)) == 2 and all(rn)
    pdf = finance.get("/receipts/1")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    assert student.get("/receipts/1").status_code == 200
    page = student.get("/fees").data
    assert b"Paid" in page and b"RCPT" in page
    assert conn.execute("SELECT COUNT(*) c FROM audit_log WHERE action='payment_record'").fetchone()["c"] == 2


def test_student_cannot_touch_other_fees_or_edit(student, student3):
    assert student.get("/fees/3").status_code == 403
    assert student.get("/receipts/1").status_code in (404, 200)
    assert student.post("/fees/1", {"action": "pay", "amount": "1", "method": "Cash"}).status_code == 403


def test_announcements_and_notifications(admin, student, teacher):
    r = admin.post("/announcements/new", {"title": "Holiday", "body": "Closed Friday", "category": "Holiday",
                                          "audience": "students", "publish_now": "1"})
    assert r.status_code == 302
    assert b"Closed Friday" in student.get("/announcements").data
    assert b"Closed Friday" not in teacher.get("/announcements").data
    assert b"Announcement: Holiday" in student.get("/notifications").data
    admin.post("/announcements/2/archive")
    assert b"Closed Friday" not in student.get("/announcements").data
    # draft invisible to students
    admin.post("/announcements/new", {"title": "Secret draft", "body": "x", "audience": "all"})
    assert b"Secret draft" not in student.get("/announcements").data
    assert student.get("/announcements/3").status_code == 404
    student.post("/notifications", {})
    assert b"unread" not in student.get("/notifications").data


def test_reports_downloads(admin, finance, officer, teacher):
    for c, kind in ((admin, "attendance"), (finance, "fees"), (finance, "outstanding"), (officer, "results"),
                    (admin, "audit"), (teacher, "attendance")):
        csv_ = c.get("/reports/%s?format=csv" % kind)
        pdf = c.get("/reports/%s?format=pdf" % kind)
        assert csv_.status_code == 200 and csv_.mimetype == "text/csv"
        assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    assert teacher.get("/reports/fees").status_code == 403
    assert finance.get("/reports/attendance").status_code == 403


def test_settings_validation_and_grade(admin):
    r = admin.post("/admin/settings", {**{k: v[0] for k, v in dbm.SETTINGS_DEFAULTS.items()},
                                       "attendance_threshold": "150"})
    assert b"between 0 and 100" in r.data
    r = admin.post("/admin/settings", {**{k: v[0] for k, v in dbm.SETTINGS_DEFAULTS.items()},
                                       "grading_scale": "90:A,0:B"})
    assert r.status_code == 302


def test_backup_and_restore(app, admin):
    assert admin.post("/admin/backups", {"action": "backup"}).status_code == 302
    from smartcampus.backup import list_backups
    items = list_backups(app)
    assert len(items) == 1
    admin.post("/admin/programs", {"code": "ZZ", "name": "Temp prog"})
    assert admin.post("/admin/backups", {"action": "restore", "name": items[0]["name"]}).status_code == 302
    conn = dbm.connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) c FROM programs WHERE code='ZZ'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM audit_log WHERE action='restore'").fetchone()["c"] == 1
    assert b"not found" in admin.post("/admin/backups", {"action": "restore", "name": "../../etc/passwd"},
                                      follow_redirects=True).data


def test_session_timeout(app, student):
    import time
    with student.c.session_transaction() as s:
        s["last_active"] = time.time() - 99999
    assert student.get("/").status_code == 302


def test_xss_escaped(admin, student):
    admin.post("/announcements/new", {"title": "<script>alert(1)</script>", "body": "b", "audience": "all",
                                      "publish_now": "1"})
    page = student.get("/announcements").data
    assert b"<script>alert(1)" not in page and b"&lt;script&gt;" in page


def test_profile_student_cannot_change_name(student):
    student.post("/profile", {"action": "profile", "name": "Hacker", "phone": "123", "address": "Kathmandu"})
    assert b"Aarav Adhikari" in student.get("/profile").data
    assert b"Kathmandu" in student.get("/profile").data


def test_security_headers_and_404(student):
    r = student.get("/nope")
    assert r.status_code == 404
    assert "Content-Security-Policy" in r.headers
