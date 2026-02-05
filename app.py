import os
import string
import secrets
from urllib.parse import urlparse

from flask import Flask, request, jsonify, redirect, render_template_string
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg

def get_database_url() -> str:
    """Resolve DB URL.

    - Locally: default to localhost.
    - On Railway/any deployed env (PORT is set): require DATABASE_URL.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    # If we're deployed, Railway will set PORT. Don't silently fall back to localhost.
    if os.getenv("PORT"):
        raise RuntimeError(
            "DATABASE_URL is not set. In Railway, add a Postgres service and set the Web service DATABASE_URL to the Postgres connection string."
        )

    return "postgresql://postgres:postgres@localhost:5432/shortlink"

app = Flask(__name__)
# Trust Railway/Reverse-proxy headers so request.host_url uses the public domain + https
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

_db_initialized = False

def ensure_db():
    """Create the table once (safe to call many times)."""
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True

def get_conn():
    return psycopg.connect(get_database_url(), connect_timeout=5)

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

@app.get("/")
def home():
    # Simple single-file UI (no templates folder needed)
    return render_template_string(
        """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Shortlink</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 40px; max-width: 720px; }
    h1 { margin: 0 0 12px; }
    p { color: #444; }
    .row { display: flex; gap: 10px; margin-top: 16px; }
    input { flex: 1; padding: 12px; font-size: 16px; }
    button { padding: 12px 16px; font-size: 16px; cursor: pointer; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
    .out { margin-top: 18px; padding: 12px; border: 1px solid #ddd; border-radius: 10px; }
    .err { color: #b00020; }
  </style>
</head>
<body>
  <h1>Shortlink</h1>
  <p>Paste a URL and click <b>Shorten</b>.</p>

  <div class=\"row\">
    <input id=\"url\" type=\"url\" placeholder=\"https://example.com\" autocomplete=\"off\" />
    <button id=\"btn\">Shorten</button>
  </div>

  <div id=\"out\" class=\"out\" style=\"display:none\"></div>

  <script>
    const urlEl = document.getElementById('url');
    const btn = document.getElementById('btn');
    const out = document.getElementById('out');

    function show(html, isErr=false) {
      out.style.display = 'block';
      out.innerHTML = isErr ? `<div class="err">${html}</div>` : html;
    }

    async function shorten() {
      const url = (urlEl.value || '').trim();
      if (!url) return show('Please paste a URL.', true);

      btn.disabled = true;
      btn.textContent = 'Working…';
      try {
        const resp = await fetch('/shorten', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });

        const text = await resp.text();
        let data;
        try { data = JSON.parse(text); } catch { data = null; }

        if (!resp.ok) {
          const msg = data?.error || `Request failed (${resp.status})`;
          const details = data?.details ? `<br/><small><code>${data.details}</code></small>` : '';
          return show(msg + details, true);
        }

        const shortUrl = data.short_url;
        show(
          `Short URL: <a href="${shortUrl}" target="_blank" rel="noreferrer">${shortUrl}</a>` +
          `<br/>Code: <code>${data.code}</code>`
        );
      } catch (e) {
        show('Network error: ' + (e?.message || e), true);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Shorten';
      }
    }

    btn.addEventListener('click', shorten);
    urlEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') shorten(); });
  </script>
</body>
</html>"""
    )


@app.get("/health")
def health():
    return {"ok": True}

@app.post("/shorten")
def shorten():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not is_valid_url(url):
        return jsonify({"error": "Please provide a valid http(s) URL"}), 400

    # Make sure DB schema exists (handles first-boot / race conditions)
    try:
        ensure_db()

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
                        # Build a public URL using proxy headers (Railway terminates TLS)
                        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
                        host = request.headers.get("X-Forwarded-Host", request.host)
                        base = f"{proto}://{host}".rstrip("/")
                        return jsonify({
                            "code": code,
                            "short_url": base + "/" + code,
                            "url": url,
                        }), 201
    except Exception as e:
        # Surface a useful error instead of hanging / returning generic 500.
        return jsonify({"error": "Database not configured or unavailable", "details": str(e)}), 503

    return jsonify({"error": "Could not generate code, try again"}), 500

@app.get("/<code>")
def go(code):
    try:
        # Make sure DB schema exists (handles first-boot / race conditions)
        ensure_db()

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT url FROM links WHERE code=%s;", (code,))
                row = cur.fetchone()
    except Exception as e:
        return jsonify({"error": "Database not configured or unavailable", "details": str(e)}), 503

    if not row:
        return jsonify({"error": "Not found"}), 404

    return redirect(row[0], code=302)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
