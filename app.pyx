import os
import string
import secrets
from urllib.parse import urlparse

from flask import Flask, request, jsonify, redirect
import psycopg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/shortlink"
)

app = Flask(__name__)

def get_conn():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS links (
                    code TEXT PRIMARY KEY,
                    url  TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            conn.commit()

def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def gen_code(n=7) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/shorten")
def shorten():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not is_valid_url(url):
        return jsonify({"error": "Please provide a valid http(s) URL"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            for _ in range(10):
                code = gen_code()
                cur.execute("SELECT 1 FROM links WHERE code=%s;", (code,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO links (code, url) VALUES (%s, %s);",
                        (code, url),
                    )
                    conn.commit()
                    return jsonify({
                        "code": code,
                        "short_url": request.host_url.rstrip("/") + "/" + code,
                        "url": url,
                    }), 201

    return jsonify({"error": "Could not generate code, try again"}), 500

@app.get("/<code>")
def go(code):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM links WHERE code=%s;", (code,))
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Not found"}), 404

    return redirect(row[0], code=302)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)

