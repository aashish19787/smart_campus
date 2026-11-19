"""Demo data so the system is usable immediately. Safe to call repeatedly (only seeds an empty database)."""
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from . import db as dbm

DEMO_PASSWORD = "Password123"
ADMIN_PASSWORD = "Admin12345"


def seed(path):
    conn = dbm.connect(path)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        conn.close()
        return False
    ts = dbm.now()

    def user(email, name, role, pw=DEMO_PASSWORD):
        return conn.execute("INSERT INTO users(email,password_hash,name,role,created_at) VALUES(?,?,?,?,?)",
                            (email, generate_password_hash(pw), name, role, ts)).lastrowid

    user("admin@hcit.edu.np", "System Administrator", "admin", ADMIN_PASSWORD)
    t1 = user("teacher1@hcit.edu.np", "Ramesh Sharma", "teacher")
    t2 = user("teacher2@hcit.edu.np", "Sita Gurung", "teacher")
    user("finance@hcit.edu.np", "Anita Thapa", "finance")
    user("exam@hcit.edu.np", "Bikash Koirala", "examofficer")
    progs = {}
    for code, name in (("BSc", "BSc Computing"), ("BBA", "Bachelor of Business Administration"),
                       ("BIT", "Bachelor of Information Technology")):
        progs[code] = conn.execute("INSERT INTO programs(code,name) VALUES(?,?)", (code, name)).lastrowid
    courses = {}
    for code, name, prog, teacher in (("CS101", "Programming Fundamentals", "BSc", t1),
                                      ("CS102", "Database Systems", "BSc", t2),
                                      ("BA101", "Principles of Management", "BBA", t2),
                                      ("IT101", "Web Technologies", "BIT", t1)):
        courses[code] = conn.execute("INSERT INTO courses(code,name,program_id,teacher_id,semester) VALUES(?,?,?,?,?)",
                                     (code, name, progs[prog], teacher, "Semester 1")).lastrowid
    year = datetime.now().year
    students = []
    for i, (name, prog) in enumerate((("Aarav Adhikari", "BSc"), ("Binita Rai", "BSc"), ("Chandan Karki", "BSc"),
                                      ("Deepa Magar", "BBA"), ("Esha Tamang", "BIT")), start=1):
        uid = conn.execute("INSERT INTO users(email,password_hash,name,role,phone,created_at) VALUES(?,?,?, 'student',?,?)",
                           ("student%d@hcit.edu.np" % i, generate_password_hash(DEMO_PASSWORD), name,
                            "98000000%02d" % i, ts)).lastrowid
        conn.execute("INSERT INTO students(user_id,student_no,program_id,enrollment_date) VALUES(?,?,?,?)",
                     (uid, "HCIT-%d-%04d" % (year, i), progs[prog], "%d-01-15" % year))
        students.append((uid, prog))
    for uid, prog in students:
        for code, cid in courses.items():
            if code.startswith({"BSc": "CS", "BBA": "BA", "BIT": "IT"}[prog]):
                conn.execute("INSERT INTO enrollments(course_id,student_id) VALUES(?,?)", (cid, uid))
    # sample attendance for CS101 (Chandan ends below 75 %)
    cs101 = courses["CS101"]
    pattern = {students[0][0]: "PPPPPPPPPP", students[1][0]: "PPLPPAPPPP", students[2][0]: "PAAPAPAPPA"}
    for d in range(10):
        day = (datetime.now() - timedelta(days=14 - d)).strftime("%Y-%m-%d")
        sid = conn.execute("INSERT INTO att_sessions(course_id,date,taken_by,created_at) VALUES(?,?,?,?)",
                           (cs101, day, t1, ts)).lastrowid
        for stu, pat in pattern.items():
            conn.execute("INSERT INTO att_records(session_id,student_id,status) VALUES(?,?,?)",
                         (sid, stu, {"P": "Present", "A": "Absent", "L": "Late"}[pat[d]]))
    s1, s2, s3 = students[0][0], students[1][0], students[2][0]
    dbm.notify([s3], "Low attendance warning: CS101 Programming Fundamentals is at 40.0% (minimum 75%).", db=conn)
    # assignment, exam and fees
    pub = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    due = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO assignments(course_id,title,description,publish_date,deadline,max_marks,created_by,notified,created_at) "
                 "VALUES(?,?,?,?,?,?,?,1,?)", (cs101, "Assignment 1: Loops and Functions",
                                                "Solve the problems in the handout and upload a single PDF.", pub, due, 20, t1, ts))
    conn.execute("INSERT INTO exams(course_id,exam_type,exam_date,exam_time,max_marks,created_by) VALUES(?,?,?,?,?,1)",
                 (cs101, "Midterm", (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d"), "10:00", 100))
    for uid, _p in students:
        fid = conn.execute("INSERT INTO fee_records(student_id,fee_type,amount,due_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                           (uid, "Tuition", 50000, "%d-12-31" % year, 4, ts)).lastrowid
    conn.execute("INSERT INTO announcements(title,body,category,audience,status,publish_date,created_by,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,1,?,?)", ("Welcome to Smart Campus",
                                                "HCIT now manages attendance, assignments, results and fees online.",
                                                "General", "all", "published", ts, ts, ts))
    dbm.audit("seed", "system", "Demo data created", user_id=1, user_name="System", db=conn)
    conn.commit()
    conn.close()
    return True
