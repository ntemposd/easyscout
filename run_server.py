# Run server via waitress (recommended on Windows)
# Usage: python run_server.py
import os
import argparse

try:
    from waitress import serve
except Exception:
    serve = None


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Run with Flask's development reloader instead of waitress.",
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.reload:
        os.environ["EASYSCOUT_DEV_SERVER"] = "1"
    from app import app

    port = int(os.environ.get("PORT", "5000"))
    if args.reload:
        print("Starting with Flask dev server and auto-reload on port", port)
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=True)
    elif serve:
        print("Starting with waitress on port", port)
        serve(app, host="0.0.0.0", port=port)
    else:
        print("waitress not installed, falling back to Flask dev server (debug=False)")
        app.run(host="0.0.0.0", port=port, debug=False)
