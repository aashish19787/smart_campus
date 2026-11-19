# Smart Campus – College Management and Student Service System (HCIT)

Implementation of SRS_SMART_CAMPUS_V1.0 (FR-01 … FR-96, NFR-01 … NFR-54).
Stack: Python 3 · Flask · SQLite · ReportLab (PDF). No external services required.

## Run
    ./run.sh            # Linux/macOS  (run.bat on Windows)
or manually:
    pip install -r requirements.txt
    python run.py       # http://127.0.0.1:5000

Demo data is loaded on first start (disable with `SC_SEED_DEMO=0`).

| Role | Email | Password |
|---|---|---|
| Administrator | admin@hcit.edu.np | Admin12345 |
| Teacher | teacher1@hcit.edu.np / teacher2@hcit.edu.np | Password123 |
| Student | student1@hcit.edu.np … student5@hcit.edu.np | Password123 |
| Finance Staff | finance@hcit.edu.np | Password123 |
| Examination Officer | exam@hcit.edu.np | Password123 |

**Change these passwords before real use.**

## Tests
    python -m pytest -q        # 29 tests: every page per role, RBAC, workflows, security

## Configuration (environment variables)
`HOST`, `PORT`, `SECRET_KEY`, `SC_HTTPS=1` (secure cookies, when behind HTTPS), `SC_SEED_DEMO`,
`SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/MAIL_FROM` (real e-mail). Without SMTP, e-mails
(e.g. password-reset links) are stored in **Admin → E-mail outbox**.
Academic settings (attendance threshold, late weight, grading scale, session timeout, upload limit)
are editable by the administrator in **Admin → Settings**. Install `waitress` for a production server;
put the app behind an HTTPS reverse proxy (NFR-13).

## Module map
| SRS section | Where |
|---|---|
| 3.1 Users/auth/RBAC | `auth.py`, `security.py`, `admin.py` |
| 3.2 Students, programs, courses | `admin.py` |
| 3.3 Attendance | `attendance.py` |
| 3.4 Assignments | `assignments.py` |
| 3.5 Exams, marks, results | `exams.py` (scheduled → entered → verified → approved → published) |
| 3.6 Fees, receipts | `fees.py`, `pdfutil.py` |
| 3.7 Announcements, notifications | `comms.py` |
| 3.8 Reports, activity log | `reports.py`, `admin.py` |
| 5.8 Backup/recovery | `backup.py` (24h automatic, integrity check, restore) |

## Policy assumptions (SRS leaves these "to be approved by HCIT")
* Attendance % = (Present + Late × weight) / sessions; default weight 1.0, warning below 75 % – configurable.
* Default grading: ≥80 A, ≥70 B, ≥60 C, ≥50 D, else F – configurable.
* Course total = sum of published exam marks / sum of maximum marks.
