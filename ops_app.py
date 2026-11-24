"""Operations support page entry point: what to do with each pump this hour, and where it runs on its curve.

    python ops_app.py                        local server on http://127.0.0.1:8051
    gunicorn ops_app:server -b :8001         production

Set WELLOPT_CONFIG to use a site config other than config/site.toml, and HOST / OPS_PORT to change where
it listens. docs/OPS_PAGE.md describes the page.
"""
import os

from wellopt.ops_page import create_app

server = create_app(os.environ.get("WELLOPT_CONFIG") or None)

if __name__ == "__main__":
    server.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("OPS_PORT", "8051")), debug=False)
