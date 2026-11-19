"""Start Smart Campus:  python run.py   ->  http://127.0.0.1:5000"""
import os

from smartcampus import create_app
from smartcampus.backup import start_scheduler
from smartcampus.seed import seed

app = create_app()
if os.environ.get("SC_SEED_DEMO", "1") == "1" and seed(app.config["DATABASE"]):
    print("Demo data loaded. Log in with admin@hcit.edu.np / Admin12345 (other roles: see README).")

if __name__ == "__main__":
    start_scheduler(app)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve  # production-grade server when installed
        print("Smart Campus running on http://%s:%d" % (host, port))
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)
