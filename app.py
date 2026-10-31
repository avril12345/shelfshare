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
CREATE TABLE IF NOT EXISTS reset_tokens(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, used INTEGER DEFAULT 0);
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
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(group_id, user_id));
CREATE INDEX IF NOT EXISTS gm_user ON group_members(user_id);
CREATE TABLE IF NOT EXISTS shelves(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  group_id INTEGER NOT NULL REFERENCES groups(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS shelf_managers(shelf_id INTEGER NOT NULL REFERENCES shelves(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(shelf_id, user_id));
"""
MAX_UNUSED_INVITES = 10
DEFAULT_GROUPS = ("Book Club", "Family")
# Users who share at least one group with :me (including :me)
VISIBLE = "(SELECT gm2.user_id FROM group_members gm1 JOIN group_members gm2 ON gm2.group_id=gm1.group_id WHERE gm1.user_id=:me UNION SELECT :me)"
# Shared household shelves visible to :me (via group membership)
MY_SHELVES = "(SELECT s.id FROM shelves s JOIN group_members gm ON gm.group_id=s.group_id WHERE gm.user_id=:me)"
# Shared shelves :me manages (manager AND still in the shelf's group)
MANAGED = "(SELECT s.id FROM shelves s JOIN shelf_managers sm ON sm.shelf_id=s.id JOIN group_members gm ON gm.group_id=s.group_id AND gm.user_id=sm.user_id WHERE sm.user_id=:me)"
BOOK_VISIBLE = "((b.shelf_id IS NULL AND b.owner_id IN " + VISIBLE + ") OR b.shelf_id IN " + MY_SHELVES + ")"
BOOK_MANAGED = "((b.shelf_id IS NULL AND b.owner_id=:me) OR b.shelf_id IN " + MANAGED + ")"
FAMILY_SHELF = "Family home (Hordle)"
HORDLE_PATTERN = "%Hordle UK%"

def migrate(con):
    """Idempotent, additive migration. Safe on an existing DB: never drops or rewrites data."""
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(invites)")]
    if "group_id" not in cols:
        con.execute("ALTER TABLE invites ADD COLUMN group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE")
    if con.execute("PRAGMA user_version").fetchone()[0] < 1:
        av = con.execute("SELECT id FROM users WHERE username='Av'").fetchone()
        if av:
            ids = {}
            for name in DEFAULT_GROUPS:
                r = con.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()
                ids[name] = r[0] if r else con.execute("INSERT INTO groups(name, owner_id) VALUES(?,?)", (name, av[0])).lastrowid
                con.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (ids[name], av[0]))
            bc = ids["Book Club"]
            # Pre-groups users could all see each other; keep them in Book Club so nothing disappears.
            con.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) SELECT ?, id FROM users", (bc,))
            con.execute("UPDATE invites SET group_id=? WHERE group_id IS NULL", (bc,))
            con.execute("PRAGMA user_version=1")
    con.commit()
    bcols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "shelf_id" not in bcols:
        con.execute("ALTER TABLE books ADD COLUMN shelf_id INTEGER REFERENCES shelves(id)")
        con.commit()
    if con.execute("PRAGMA user_version").fetchone()[0] == 1:
        migrate_v2_family_shelf(con)

def migrate_v2_family_shelf(con):
    """One-time: create the Family home shelf and move Av's Hordle UK books onto it.
    Only sets books.shelf_id (no rows deleted or rewritten); runs in one transaction; bumps user_version to 2."""
    av = con.execute("SELECT id FROM users WHERE username='Av'").fetchone()
    fam = con.execute("SELECT id FROM groups WHERE name='Family'").fetchone()
    if not av or not fam: return
    if DB != ":memory:" and os.path.exists(DB):
        bak = DB + ".pre-v2.bak"
        if not os.path.exists(bak):
            b = sqlite3.connect(bak); con.backup(b); b.close()
    before = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT id FROM shelves WHERE name=?", (FAMILY_SHELF,)).fetchone()
        sid = r[0] if r else con.execute("INSERT INTO shelves(name, group_id) VALUES(?,?)", (FAMILY_SHELF, fam[0])).lastrowid
        con.execute("INSERT OR IGNORE INTO shelf_managers(shelf_id,user_id) VALUES(?,?)", (sid, av[0]))
        moved = con.execute("UPDATE books SET shelf_id=? WHERE owner_id=? AND shelf_id IS NULL AND location LIKE ?",
                            (sid, av[0], HORDLE_PATTERN)).rowcount
        if con.execute("SELECT COUNT(*) FROM books").fetchone()[0] != before: raise RuntimeError("book count changed")
        con.execute("PRAGMA user_version=2")
        con.execute("COMMIT")
        print(f"[shelfshare] migration v2: moved {moved} Hordle UK books to '{FAMILY_SHELF}'")
    except Exception:
        con.execute("ROLLBACK"); raise

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
    with app.app_context(): migrate(db())

def shares_group(a, b):
    return a == b or db().execute("""SELECT 1 FROM group_members x JOIN group_members y ON x.group_id=y.group_id
        WHERE x.user_id=? AND y.user_id=?""", (a, b)).fetchone() is not None

def is_member(gid, uid):
    return db().execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone() is not None

def redeem(c, code, uid, uname):
    """Inside a transaction: mark the invite used and join its group. Returns group name or None."""
    inv = c.execute("SELECT i.used_at, i.group_id, g.name FROM invites i LEFT JOIN groups g ON g.id=i.group_id WHERE i.code=?", (code,)).fetchone()
    if not inv or inv["used_at"] or not inv["group_id"]: return None
    c.execute("UPDATE invites SET used_by=?, used_by_name=?, used_at=CURRENT_TIMESTAMP WHERE code=? AND used_at IS NULL", (uid, uname, code))
    c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (inv["group_id"], uid))
    return inv["name"]

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
        inv = c.execute("SELECT used_at, group_id FROM invites WHERE code=?", (code,)).fetchone()
        if not inv or inv["used_at"] or not inv["group_id"]:
            c.execute("ROLLBACK"); return jsonify(error="That invite code is invalid or already used"), 403
        uid = c.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", (u, generate_password_hash(p))).lastrowid
        redeem(c, code, uid, u)
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK"); return jsonify(error="That username is taken"), 409
    session["uid"], session["name"] = uid, u
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
    n = db().execute("SELECT COUNT(*) FROM books WHERE owner_id=? AND shelf_id IS NULL", (session["uid"],)).fetchone()[0]
    return jsonify(user=session["name"], book_count=n, shelves=manageable_shelves())

# ---------- invites ----------
def new_code(): return "-".join(secrets.token_hex(2).upper() for _ in range(3))

@app.get("/api/invites")
@login_required
def list_invites():
    rows = db().execute("""SELECT i.code, i.created_at, i.used_by_name, i.used_at, i.group_id, g.name group_name
        FROM invites i LEFT JOIN groups g ON g.id=i.group_id WHERE i.created_by=? ORDER BY i.used_at IS NOT NULL, i.created_at DESC""",
                        (session["uid"],)).fetchall()
    return jsonify(invites=[dict(r) for r in rows], max_unused=MAX_UNUSED_INVITES)

@app.post("/api/invites")
@login_required
def create_invite():
    n = db().execute("SELECT COUNT(*) FROM invites WHERE created_by=? AND used_at IS NULL", (session["uid"],)).fetchone()[0]
    if n >= MAX_UNUSED_INVITES: return jsonify(error=f"You already have {MAX_UNUSED_INVITES} unused codes"), 400
    try: gid = int((request.get_json(silent=True) or {}).get("group_id"))
    except (TypeError, ValueError): return jsonify(error="Pick a group for this invite"), 400
    if not is_member(gid, session["uid"]): return jsonify(error="You can only invite people to groups you're in"), 403
    code = new_code()
    db().execute("INSERT INTO invites(code, created_by, group_id) VALUES(?,?,?)", (code, session["uid"], gid)); db().commit()
    return jsonify(ok=True, code=code)

@app.delete("/api/invites/<code>")
@login_required
def revoke_invite(code):
    db().execute("DELETE FROM invites WHERE code=? AND created_by=? AND used_at IS NULL", (code.upper(), session["uid"])); db().commit()
    return jsonify(ok=True)

@app.post("/api/redeem")
@login_required
def redeem_invite():
    code = ((request.get_json(silent=True) or {}).get("code") or "").strip().upper()
    c = db().cursor(); c.execute("BEGIN IMMEDIATE")
    name = redeem(c, code, session["uid"], session["name"])
    if not name:
        c.execute("ROLLBACK"); return jsonify(error="That invite code is invalid or already used"), 403
    c.execute("COMMIT"); return jsonify(ok=True, group=name)

# ---------- groups ----------
@app.get("/api/groups")
@login_required
def my_groups():
    gs = db().execute("""SELECT g.id, g.name, ou.username owner FROM groups g JOIN group_members m ON m.group_id=g.id
        LEFT JOIN users ou ON ou.id=g.owner_id WHERE m.user_id=? ORDER BY g.name""", (session["uid"],)).fetchall()
    out = []
    for g_ in gs:
        mem = db().execute("""SELECT u.username, (SELECT COUNT(*) FROM books b WHERE b.owner_id=u.id AND b.shelf_id IS NULL) books
            FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? ORDER BY u.username""", (g_["id"],)).fetchall()
        sh = db().execute("SELECT s.id, s.name, (SELECT COUNT(*) FROM books b WHERE b.shelf_id=s.id) books FROM shelves s WHERE s.group_id=?", (g_["id"],)).fetchall()
        out.append(dict(g_, members=[dict(m) for m in mem], shelves=[dict(x) for x in sh]))
    return jsonify(groups=out)

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
def manageable_shelves():
    rows = db().execute("SELECT s.id, s.name FROM shelves s WHERE s.id IN " + MANAGED + " ORDER BY s.name", dict(me=session["uid"])).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/books")
@login_required
def my_books():
    rows = db().execute("""SELECT b.*, COALESCE(s.name, 'My shelf') shelf FROM books b LEFT JOIN shelves s ON s.id=b.shelf_id
        WHERE """ + BOOK_MANAGED + " ORDER BY b.shelf_id IS NOT NULL, s.name, b.location, b.title", dict(me=session["uid"])).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/books")
@login_required
def add_book():
    d = request.get_json(force=True)
    t = (d.get("title") or "").strip()
    if not t: return jsonify(error="Title is required"), 400
    sid = d.get("shelf_id") or None
    if sid is not None:
        try: sid = int(sid)
        except (TypeError, ValueError): return jsonify(error="Bad shelf"), 400
        if sid not in [x["id"] for x in manageable_shelves()]: return jsonify(error="You don't manage that shelf"), 403
    cur = db().execute("INSERT INTO books(owner_id,title,author,isbn,cover_url,location,source,shelf_id) VALUES(?,?,?,?,?,?,?,?)",
        (session["uid"], t, (d.get("author") or "").strip() or None, clean_isbn(d.get("isbn")) or None,
         d.get("cover_url") or None, (d.get("location") or "").strip() or None, d.get("source") or "manual", sid))
    db().commit()
    return jsonify(id=cur.lastrowid, ok=True)

@app.delete("/api/books/<int:bid>")
@login_required
def del_book(bid):
    db().execute("DELETE FROM books WHERE id=:bid AND id IN (SELECT b.id FROM books b WHERE " + BOOK_MANAGED + ")", dict(bid=bid, me=session["uid"])); db().commit()
    return jsonify(ok=True)

@app.get("/api/locations")
@login_required
def locations():
    rows = db().execute("SELECT DISTINCT b.location FROM books b WHERE " + BOOK_MANAGED + " AND b.location IS NOT NULL ORDER BY 1", dict(me=session["uid"]))
    return jsonify([r[0] for r in rows])

# ---------- search ----------
@app.get("/api/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2: return jsonify(query=q, results=[])
    like = f"%{q}%"
    rows = db().execute("""SELECT b.*, u.username owner, s.name shelf FROM books b JOIN users u ON u.id=b.owner_id
        LEFT JOIN shelves s ON s.id=b.shelf_id
        WHERE (b.title LIKE :l OR b.author LIKE :l OR b.isbn = :i) AND """ + BOOK_VISIBLE + """
        ORDER BY b.title LIMIT 200""", dict(l=like, i=clean_isbn(q) or "-", me=session["uid"])).fetchall()
    groups = {}
    for r in rows:
        key = (r["title"].lower().strip(), (r["author"] or "").lower().strip())
        g_ = groups.setdefault(key, dict(title=r["title"], author=r["author"], cover_url=r["cover_url"], mine=[], friends=[]))
        g_["cover_url"] = g_["cover_url"] or r["cover_url"]
        if r["shelf_id"] is None and r["owner_id"] == session["uid"]: g_["mine"].append(dict(id=r["id"], location=r["location"]))
        else:
            name = r["shelf"].split(" (")[0] if r["shelf"] else r["owner"]
            g_["friends"].append(dict(id=r["id"], owner=name, shared=r["shelf_id"] is not None, location=r["location"]))
    return jsonify(query=q, results=list(groups.values()))

# ---------- borrow requests ----------
@app.post("/api/borrow/<int:bid>")
@login_required
def borrow(bid):
    me_ = dict(bid=bid, me=session["uid"])
    if not db().execute("SELECT 1 FROM books b WHERE b.id=:bid AND " + BOOK_VISIBLE, me_).fetchone(): return jsonify(error="No such book"), 404
    if db().execute("SELECT 1 FROM books b WHERE b.id=:bid AND " + BOOK_MANAGED, me_).fetchone():
        return jsonify(error="That's on a shelf you manage!"), 400
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
    q = q.replace("ou.username owner", "COALESCE(s.name, ou.username) owner, s.name shelf").replace(
        "JOIN users ou ON ou.id=b.owner_id", "JOIN users ou ON ou.id=b.owner_id LEFT JOIN shelves s ON s.id=b.shelf_id")
    inc = db().execute(q + " WHERE " + BOOK_MANAGED + " ORDER BY r.created_at DESC", dict(me=session["uid"])).fetchall()
    out = db().execute(q + " WHERE r.requester_id=:me ORDER BY r.created_at DESC", dict(me=session["uid"])).fetchall()
    return jsonify(incoming=[dict(r) for r in inc], outgoing=[dict(r) for r in out])

@app.post("/api/requests/<int:rid>")
@login_required
def answer(rid):
    st = (request.get_json(force=True).get("status"))
    if st not in ("approved", "declined", "returned"): return jsonify(error="bad status"), 400
    db().execute("UPDATE borrow_requests SET status=:st WHERE id=:rid AND book_id IN (SELECT b.id FROM books b WHERE " + BOOK_MANAGED + ")",
                 dict(st=st, rid=rid, me=session["uid"])); db().commit()
    return jsonify(ok=True)

# ---------- future: AI shelf-photo scanner ----------
@app.post("/api/scan-shelf")
@login_required
def scan_shelf():
    if not shelf_scanner.ENABLED:
        return jsonify(error="Shelf-photo scanning is coming soon. Use the barcode scanner for now."), 501
    return jsonify(candidates=shelf_scanner.read_spines(request.files["photo"].read()))

@app.get("/set-password")
def set_password_page(): return send_from_directory(BASE + "/static", "set-password.html")

@app.post("/api/set-password")
def set_password():
    d = request.get_json(force=True, silent=True) or {}
    tok, pw = (d.get("token") or "").strip(), d.get("password") or ""
    if len(pw) < 8: return jsonify(error="Password must be at least 8 characters"), 400
    c = db()
    c.execute("BEGIN IMMEDIATE")
    r = c.execute("SELECT user_id FROM reset_tokens WHERE token=? AND used=0 AND expires_at > datetime('now')", (tok,)).fetchone()
    if not tok or not r:
        c.execute("ROLLBACK"); return jsonify(error="This link is invalid, expired or already used"), 400
    c.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), r[0]))
    c.execute("UPDATE reset_tokens SET used=1 WHERE token=?", (tok,))
    c.commit()
    session.clear()
    return jsonify(ok=True)

@app.get("/")
def index(): return send_from_directory(BASE + "/static", "index.html")

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
