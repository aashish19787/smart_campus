import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from smartcampus import create_app  # noqa: E402
from smartcampus.seed import seed  # noqa: E402


class Client:
    def __init__(self, app):
        self.c = app.test_client()

    def token(self):
        with self.c.session_transaction() as s:
            if "_csrf" not in s:
                s["_csrf"] = "testtoken"
            return s["_csrf"]

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["_csrf"] = self.token()
        return self.c.post(url, data=data, **kw)

    def login(self, email, pw="Password123"):
        self.token()
        return self.post("/login", {"email": email, "password": pw})


@pytest.fixture()
def app():
    d = tempfile.mkdtemp()
    a = create_app({"TESTING": True, "DATABASE": os.path.join(d, "t.db"), "UPLOAD_FOLDER": os.path.join(d, "up"),
                    "BACKUP_FOLDER": os.path.join(d, "bk")})
    seed(a.config["DATABASE"])
    return a


@pytest.fixture()
def anon(app):
    return Client(app)


def _as(app, email, pw="Password123"):
    c = Client(app)
    r = c.login(email, pw)
    assert r.status_code == 302, r.data[:300]
    return c


@pytest.fixture()
def admin(app):
    return _as(app, "admin@hcit.edu.np", "Admin12345")


@pytest.fixture()
def teacher(app):
    return _as(app, "teacher1@hcit.edu.np")


@pytest.fixture()
def teacher2(app):
    return _as(app, "teacher2@hcit.edu.np")


@pytest.fixture()
def student(app):
    return _as(app, "student1@hcit.edu.np")


@pytest.fixture()
def student3(app):
    return _as(app, "student3@hcit.edu.np")


@pytest.fixture()
def finance(app):
    return _as(app, "finance@hcit.edu.np")


@pytest.fixture()
def officer(app):
    return _as(app, "exam@hcit.edu.np")
