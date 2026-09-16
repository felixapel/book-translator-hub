"""Minimal exact Kavita account boundary for container smoke tests.

This is not a Kavita substitute. It exposes only the exact authenticated
account response and stock EPUB DOM/route shape consumed by the connector.
"""

import os

from flask import Flask, jsonify, request


app = Flask(__name__)

NATIVE_TOKEN = "container-smoke-kavita-access"
OIDC_COOKIE = "container-smoke-kavita-oidc"
KAVITA_VERSION = os.environ.get("KAVITA_FIXTURE_VERSION", "0.9.0.2")
if KAVITA_VERSION not in {"0.9.0.2", "0.9.1.4"}:
    raise RuntimeError("KAVITA_FIXTURE_VERSION is not a certified fixture contract")


@app.get("/api/Account")
def account():
    bearer_ok = request.headers.get("Authorization") == f"Bearer {NATIVE_TOKEN}"
    oidc_ok = request.cookies.get(".AspNetCore.Cookies") == OIDC_COOKIE
    if bearer_ok == oidc_ok:
        return jsonify({"error": "unauthorized"}), 401
    # authKeys is deliberately present to prove the broker selects only the
    # bounded id/version fields and never needs to retain the full DTO.
    return jsonify(
        {
            "id": 73,
            "username": "fixture-user",
            "kavitaVersion": KAVITA_VERSION,
            "authKeys": [{"name": "must-not-leave-fixture", "key": "secret"}],
        }
    )


@app.get("/library/<int:library_id>/series/<int:series_id>/book/<int:chapter_id>")
def epub_reader(library_id: int, series_id: int, chapter_id: int):
    return f"""<!doctype html><html><head><title>Kavita fixture</title></head>
<body><main class=\"book-container\"><div class=\"book-content\">
<p>EPUB {library_id}:{series_id}:{chapter_id}</p>
</div></main></body></html>"""


@app.get("/library/<int:library_id>/series/<int:series_id>/manga/<int:chapter_id>")
def manga_reader(library_id: int, series_id: int, chapter_id: int):
    return f"manga {library_id}:{series_id}:{chapter_id}"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
