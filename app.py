import secrets
"""ShelfShare - share book libraries with friends. Flask + SQLite."""
import os, secrets, sqlite3, json, re, urllib.request, urllib.parse
from functools import wraps
from flask import Flask, request, jsonify, session, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
import shelf_scanner

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SHELFSHARE_DB", os.path.join(BASE, "data", "shelfshare.db"))
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS books(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, author TEXT, isbn TEXT, cover_url TEXT, location TEXT, source TEXT DEFAULT 'manual',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS books_owner ON books(owner_id);
CREATE TABLE IF NOT EXISTS borrow_requests(id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  requester_id INTEGER NOT NULL REFERENCES users(id), message TEXT, status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY, created_by INTEGER REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  used_by_name TEXT, used_at TEXT);
"""
MAX_UNUSED_INVITES = 10

def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    with app.app_context(): db().executescript(SCHEMA); db().commit()

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"): return jsonify(error="Please log in"), 401
        return f(*a, **k)
    return w

def clean_isbn(s): return re.sub(r"[^0-9Xx]", "", s or "").upper()

# ---------- auth ----------
@app.post("/api/signup")
def signup():
    d = request.get_json(force=True)
    u, p = (d.get("username") or "").strip(), d.get("password") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]{2,30}", u): return jsonify(error="Username: 2-30 letters/numbers"), 400
    if len(p) < 6: return jsonify(error="Password must be at least 6 characters"), 400
    code = (d.get("invite") or "").strip().upper()
    if not code: return jsonify(error="An invite code is required to sign up"), 403
    c = db().cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        inv = c.execute("SELECT used_at FROM invites WHERE code=?", (code,)).fetchone()
        if not inv or inv["used_at"]:
            c.execute("ROLLBACK"); return jsonify(error="That invite code is invalid or already used"), 403
        cur = c.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", (u, generate_password_hash(p)))
        c.execute("UPDATE invites SET used_by=?, used_by_name=?, used_at=CURRENT_TIMESTAMP WHERE code=? AND used_at IS NULL",
                  (cur.lastrowid, u, code))
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK"); return jsonify(error="That username is taken"), 409
    session["uid"], session["name"] = cur.lastrowid, u
    return jsonify(ok=True, username=u)

@app.post("/api/login")
def login():
    d = request.get_json(force=True)
    r = db().execute("SELECT * FROM users WHERE username=?", ((d.get("username") or "").strip(),)).fetchone()
    if not r or not check_password_hash(r["password_hash"], d.get("password") or ""):
        return jsonify(error="Wrong username or password"), 401
    session["uid"], session["name"] = r["id"], r["username"]
    return jsonify(ok=True, username=r["username"])

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def me():
    if not session.get("uid"): return jsonify(user=None)
    n = db().execute("SELECT COUNT(*) FROM books WHERE owner_id=?", (session["uid"],)).fetchone()[0]
    return jsonify(user=session["name"], book_count=n)

# ---------- invites ----------
def new_code(): return "-".join(secrets.token_hex(2).upper() for _ in range(3))

@app.get("/api/invites")
@login_required
def list_invites():
    rows = db().execute("SELECT code, created_at, used_by_name, used_at FROM invites WHERE created_by=? ORDER BY used_at IS NOT NULL, created_at DESC",
                        (session["uid"],)).fetchall()
    return jsonify(invites=[dict(r) for r in rows], max_unused=MAX_UNUSED_INVITES)

@app.post("/api/invites")
@login_required
def create_invite():
    n = db().execute("SELECT COUNT(*) FROM invites WHERE created_by=? AND used_at IS NULL", (session["uid"],)).fetchone()[0]
    if n >= MAX_UNUSED_INVITES: return jsonify(error=f"You already have {MAX_UNUSED_INVITES} unused codes"), 400
    code = new_code()
    db().execute("INSERT INTO invites(code, created_by) VALUES(?,?)", (code, session["uid"])); db().commit()
    return jsonify(ok=True, code=code)

@app.delete("/api/invites/<code>")
@login_required
def revoke_invite(code):
    db().execute("DELETE FROM invites WHERE code=? AND created_by=? AND used_at IS NULL", (code.upper(), session["uid"])); db().commit()
    return jsonify(ok=True)

# ---------- ISBN lookup ----------
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ShelfShare/0.1 (personal project)"})
    with urllib.request.urlopen(req, timeout=8) as r: return json.load(r)

def lookup_isbn(isbn):
    try:
        j = _get(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data").get(f"ISBN:{isbn}")
        if j:
            return dict(title=j.get("title"), author=", ".join(a["name"] for a in j.get("authors", [])) or None,
                        cover_url=(j.get("cover") or {}).get("medium") or f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg",
                        isbn=isbn, source="Open Library")
    except Exception: pass
    try:
        items = _get(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}").get("items") or []
        if items:
            v = items[0]["volumeInfo"]
            return dict(title=v.get("title"), author=", ".join(v.get("authors", [])) or None,
                        cover_url=(v.get("imageLinks") or {}).get("thumbnail", "").replace("http://", "https://") or None,
                        isbn=isbn, source="Google Books")
    except Exception: pass
    return None

@app.get("/api/isbn/<raw>")
@login_required
def isbn(raw):
    i = clean_isbn(raw)
    if len(i) not in (10, 13): return jsonify(error="That doesn't look like an ISBN"), 400
    r = lookup_isbn(i)
    return (jsonify(r) if r else (jsonify(error="Not found - enter it manually", isbn=i), 404))

# ---------- books ----------
@app.get("/api/books")
@login_required
def my_books():
    rows = db().execute("SELECT * FROM books WHERE owner_id=? ORDER BY location, title", (session["uid"],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/books")
@login_required
def add_book():
    d = request.get_json(force=True)
    t = (d.get("title") or "").strip()
    if not t: return jsonify(error="Title is required"), 400
    cur = db().execute("INSERT INTO books(owner_id,title,author,isbn,cover_url,location,source) VALUES(?,?,?,?,?,?,?)",
        (session["uid"], t, (d.get("author") or "").strip() or None, clean_isbn(d.get("isbn")) or None,
         d.get("cover_url") or None, (d.get("location") or "").strip() or None, d.get("source") or "manual"))
    db().commit()
    return jsonify(id=cur.lastrowid, ok=True)

@app.delete("/api/books/<int:bid>")
@login_required
def del_book(bid):
    db().execute("DELETE FROM books WHERE id=? AND owner_id=?", (bid, session["uid"])); db().commit()
    return jsonify(ok=True)

@app.get("/api/locations")
@login_required
def locations():
    rows = db().execute("SELECT DISTINCT location FROM books WHERE owner_id=? AND location IS NOT NULL ORDER BY 1", (session["uid"],))
    return jsonify([r[0] for r in rows])

# ---------- search ----------
@app.get("/api/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2: return jsonify(query=q, results=[])
    like = f"%{q}%"
    rows = db().execute("""SELECT b.*, u.username owner FROM books b JOIN users u ON u.id=b.owner_id
        WHERE b.title LIKE ? OR b.author LIKE ? OR b.isbn = ? ORDER BY b.title LIMIT 200""",
        (like, like, clean_isbn(q) or "-")).fetchall()
    groups = {}
    for r in rows:
        key = (r["title"].lower().strip(), (r["author"] or "").lower().strip())
        g_ = groups.setdefault(key, dict(title=r["title"], author=r["author"], cover_url=r["cover_url"], mine=[], friends=[]))
        g_["cover_url"] = g_["cover_url"] or r["cover_url"]
        if r["owner_id"] == session["uid"]: g_["mine"].append(dict(id=r["id"], location=r["location"]))
        else: g_["friends"].append(dict(id=r["id"], owner=r["owner"], location=r["location"]))
    return jsonify(query=q, results=list(groups.values()))

# ---------- borrow requests ----------
@app.post("/api/borrow/<int:bid>")
@login_required
def borrow(bid):
    b = db().execute("SELECT owner_id FROM books WHERE id=?", (bid,)).fetchone()
    if not b: return jsonify(error="No such book"), 404
    if b["owner_id"] == session["uid"]: return jsonify(error="That's your own book!"), 400
    if db().execute("SELECT 1 FROM borrow_requests WHERE book_id=? AND requester_id=? AND status='pending'", (bid, session["uid"])).fetchone():
        return jsonify(ok=True, note="Already requested")
    db().execute("INSERT INTO borrow_requests(book_id,requester_id,message) VALUES(?,?,?)",
                 (bid, session["uid"], (request.get_json(silent=True) or {}).get("message")))
    db().commit(); return jsonify(ok=True)

@app.get("/api/requests")
@login_required
def requests_():
    q = """SELECT r.id, r.status, r.message, r.created_at, b.title, b.location, ru.username requester, ou.username owner
           FROM borrow_requests r JOIN books b ON b.id=r.book_id JOIN users ru ON ru.id=r.requester_id JOIN users ou ON ou.id=b.owner_id"""
    inc = db().execute(q + " WHERE b.owner_id=? ORDER BY r.created_at DESC", (session["uid"],)).fetchall()
    out = db().execute(q + " WHERE r.requester_id=? ORDER BY r.created_at DESC", (session["uid"],)).fetchall()
    return jsonify(incoming=[dict(r) for r in inc], outgoing=[dict(r) for r in out])

@app.post("/api/requests/<int:rid>")
@login_required
def answer(rid):
    st = (request.get_json(force=True).get("status"))
    if st not in ("approved", "declined", "returned"): return jsonify(error="bad status"), 400
    db().execute("""UPDATE borrow_requests SET status=? WHERE id=? AND book_id IN (SELECT id FROM books WHERE owner_id=?)""",
                 (st, rid, session["uid"])); db().commit()
    return jsonify(ok=True)

# ---------- future: AI shelf-photo scanner ----------
@app.post("/api/scan-shelf")
@login_required
def scan_shelf():
    if not shelf_scanner.ENABLED:
        return jsonify(error="Shelf-photo scanning is coming soon. Use the barcode scanner for now."), 501
    return jsonify(candidates=shelf_scanner.read_spines(request.files["photo"].read()))

@app.get("/")
def index(): return send_from_directory(BASE + "/static", "index.html")

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
