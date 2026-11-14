"""Operator dashboard entry point.

    python app.py                      local server on http://127.0.0.1:8050
    gunicorn app:server -b :8000       production (see Dockerfile)

Set WELLOPT_CONFIG to use a site config other than config/site.toml. The 2022 class-project app
is kept in legacy/app.py.
"""
import os

from wellopt.dashboard import create_app

app = create_app(os.environ.get("WELLOPT_CONFIG") or None)
server = app.server

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8050")), debug=False)
