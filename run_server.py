# Run server via waitress (recommended on Windows)
# Usage: python run_server.py
import os

try:
    from waitress import serve
except Exception:
    serve = None

from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    if serve:
        print("Starting with waitress on port", port)
        serve(app, host="0.0.0.0", port=port)
    else:
        print("waitress not installed, falling back to Flask dev server (debug=False)")
        app.run(host="0.0.0.0", port=port, debug=False)
