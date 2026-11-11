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
CREATE TABLE IF NOT EXISTS friendships(from_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  to_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(from_id, to_id));
CREATE INDEX IF NOT EXISTS fr_to ON friendships(to_id);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS msg_group ON messages(group_id, id);
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, work_key TEXT NOT NULL,
  title TEXT NOT NULL, author TEXT, cover_url TEXT, stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
  rated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, work_key));
CREATE INDEX IF NOT EXISTS ratings_work ON ratings(work_key);
"""
MAX_MSG = 2000
MAX_CLUB_MEMBERS = 50
# Accepted friends of :me
FRIENDS_SEL = "SELECT to_id FROM friendships WHERE from_id=:me AND status='accepted' UNION SELECT from_id FROM friendships WHERE to_id=:me AND status='accepted'"
FRIENDS = "(" + FRIENDS_SEL + ")"
MAX_UNUSED_INVITES = 10
DEFAULT_GROUPS = ("Book Club", "Family")
# Users whose personal shelf :me can see: anyone sharing a circle or book club with :me, accepted friends, and :me
VISIBLE = "(SELECT gm2.user_id FROM group_members gm1 JOIN group_members gm2 ON gm2.group_id=gm1.group_id WHERE gm1.user_id=:me UNION " + FRIENDS_SEL + " UNION SELECT :me)"
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
    ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if "shelf_name" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN shelf_name TEXT")
        con.commit()
    con.execute("UPDATE users SET shelf_name=? WHERE lower(username)='av' AND shelf_name IS NULL", ("Avril's shelf",))
    con.commit()
    gcols = [r[1] for r in con.execute("PRAGMA table_info(groups)")]
    if "kind" not in gcols:  # 'circle' = invite-code circle (Book Club, Family); 'club' = chat-style mini book club
        con.execute("ALTER TABLE groups ADD COLUMN kind TEXT NOT NULL DEFAULT 'circle'")
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

def norm(s): return re.sub(r"\s+", " ", (s or "").strip().lower())
def work_key(title, author):
    """One key per work (not per copy), so a rating follows the book across everyone's shelves."""
    return norm(title) + "|" + norm(author)
def authors_of(author): return [a for a in (norm(x) for x in re.split(r",|&| and ", author or "")) if a]

def my_ratings():
    return {r["work_key"]: r["stars"] for r in db().execute("SELECT work_key, stars FROM ratings WHERE user_id=?", (session["uid"],))}

def network_ratings():
    """Ratings by people whose shelves I can see (never strangers'), excluding mine."""
    return db().execute("SELECT r.user_id, r.work_key, r.stars, r.title, r.author, r.cover_url, u.username FROM ratings r JOIN users u ON u.id=r.user_id "
                        "WHERE r.user_id IN " + VISIBLE + " AND r.user_id != :me", dict(me=session["uid"])).fetchall()

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
    sn = db().execute("SELECT COALESCE(shelf_name, username || '''s shelf') FROM users WHERE id=?", (session["uid"],)).fetchone()[0]
    return jsonify(user=session["name"], book_count=n, shelves=manageable_shelves(), shelf_name=sn)

@app.post("/api/my-shelf-name")
@login_required
def set_shelf_name():
    n = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not n or len(n) > 60: return jsonify(error="Shelf name must be 1-60 characters"), 400
    db().execute("UPDATE users SET shelf_name=? WHERE id=?", (n, session["uid"])); db().commit()
    return jsonify(ok=True, shelf_name=n)

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
    if db().execute("SELECT kind FROM groups WHERE id=?", (gid,)).fetchone()[0] != "circle":
        return jsonify(error="Book clubs don't use invite codes. Add friends to them from the Book Clubs tab"), 400
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
        LEFT JOIN users ou ON ou.id=g.owner_id WHERE m.user_id=? AND g.kind='circle' ORDER BY g.name""", (session["uid"],)).fetchall()
    out = []
    for g_ in gs:
        mem = db().execute("""SELECT u.username, (SELECT COUNT(*) FROM books b WHERE b.owner_id=u.id AND b.shelf_id IS NULL) books
            FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? ORDER BY u.username""", (g_["id"],)).fetchall()
        sh = db().execute("SELECT s.id, s.name, (SELECT COUNT(*) FROM books b WHERE b.shelf_id=s.id) books FROM shelves s WHERE s.group_id=?", (g_["id"],)).fetchall()
        out.append(dict(g_, members=[dict(m) for m in mem], shelves=[dict(x) for x in sh]))
    return jsonify(groups=out)

# ---------- friends ----------
def user_by_name(name):
    return db().execute("SELECT id, username FROM users WHERE username=?", ((name or "").strip(),)).fetchone()

def is_friend(a, b):
    return db().execute("""SELECT 1 FROM friendships WHERE status='accepted' AND
        ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))""", (a, b, b, a)).fetchone() is not None

@app.get("/api/friends")
@login_required
def list_friends():
    me_ = session["uid"]
    shelf = "COALESCE(u.shelf_name, u.username || '''s shelf') shelf_name, (SELECT COUNT(*) FROM books b WHERE b.owner_id=u.id AND b.shelf_id IS NULL) books"
    friends = db().execute("SELECT u.id, u.username, " + shelf + " FROM users u WHERE u.id IN " + FRIENDS + " ORDER BY u.username",
                           dict(me=me_)).fetchall()
    inc = db().execute("SELECT u.id, u.username, f.created_at FROM friendships f JOIN users u ON u.id=f.from_id WHERE f.to_id=? AND f.status='pending' ORDER BY f.created_at DESC", (me_,)).fetchall()
    out = db().execute("SELECT u.id, u.username, f.created_at FROM friendships f JOIN users u ON u.id=f.to_id WHERE f.from_id=? AND f.status='pending' ORDER BY f.created_at DESC", (me_,)).fetchall()
    return jsonify(friends=[dict(r) for r in friends], incoming=[dict(r) for r in inc], outgoing=[dict(r) for r in out])

@app.post("/api/friends")
@login_required
def add_friend():
    me_ = session["uid"]
    u = user_by_name((request.get_json(silent=True) or {}).get("username"))
    if not u: return jsonify(error="No member with that name"), 404
    if u["id"] == me_: return jsonify(error="That's you!"), 400
    if is_friend(me_, u["id"]): return jsonify(ok=True, note=f"You and {u['username']} are already friends")
    rev = db().execute("SELECT 1 FROM friendships WHERE from_id=? AND to_id=? AND status='pending'", (u["id"], me_)).fetchone()
    if rev:  # they already asked us: sending one back accepts it
        db().execute("UPDATE friendships SET status='accepted' WHERE from_id=? AND to_id=?", (u["id"], me_)); db().commit()
        return jsonify(ok=True, note=f"You and {u['username']} are now friends")
    db().execute("INSERT OR IGNORE INTO friendships(from_id, to_id) VALUES(?,?)", (me_, u["id"])); db().commit()
    return jsonify(ok=True, note=f"Friend request sent to {u['username']}")

@app.post("/api/friends/<int:uid>/<action>")
@login_required
def answer_friend(uid, action):
    me_ = session["uid"]
    if action == "accept":
        n = db().execute("UPDATE friendships SET status='accepted' WHERE from_id=? AND to_id=? AND status='pending'", (uid, me_)).rowcount
        db().commit()
        return jsonify(ok=True) if n else (jsonify(error="No such request"), 404)
    if action == "decline":
        db().execute("DELETE FROM friendships WHERE from_id=? AND to_id=? AND status='pending'", (uid, me_)); db().commit()
        return jsonify(ok=True)
    return jsonify(error="bad action"), 400

@app.delete("/api/friends/<int:uid>")
@login_required
def remove_friend(uid):
    """Unfriend, or cancel a request you sent."""
    me_ = session["uid"]
    db().execute("DELETE FROM friendships WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=? AND status='accepted')", (me_, uid, uid, me_))
    db().commit(); return jsonify(ok=True)

# ---------- mini book clubs (chat-style groups) ----------
def club_or_404(cid):
    r = db().execute("""SELECT g.id, g.name, g.owner_id FROM groups g JOIN group_members m ON m.group_id=g.id
        WHERE g.id=? AND g.kind='club' AND m.user_id=?""", (cid, session["uid"])).fetchone()
    return r

def club_members(cid):
    return [dict(r) for r in db().execute("""SELECT u.id, u.username FROM group_members m JOIN users u ON u.id=m.user_id
        WHERE m.group_id=? ORDER BY m.joined_at""", (cid,)).fetchall()]

def msg_rows(cid, after=0, limit=200):
    rows = db().execute("""SELECT * FROM (SELECT m.id, m.body, m.created_at, m.user_id, COALESCE(u.username, 'former member') author
        FROM messages m LEFT JOIN users u ON u.id=m.user_id WHERE m.group_id=? AND m.id>? ORDER BY m.id DESC LIMIT ?) ORDER BY id""",
                        (cid, after, limit)).fetchall()
    return [dict(r) for r in rows]

def post_system(cid, text):
    db().execute("INSERT INTO messages(group_id, user_id, body) VALUES(?, NULL, ?)", (cid, text))

@app.get("/api/clubs")
@login_required
def list_clubs():
    rows = db().execute("""SELECT g.id, g.name,
          (SELECT COUNT(*) FROM group_members x WHERE x.group_id=g.id) member_count,
          (SELECT body FROM messages WHERE group_id=g.id ORDER BY id DESC LIMIT 1) last_body,
          (SELECT COALESCE(u.username, '') FROM messages mm LEFT JOIN users u ON u.id=mm.user_id WHERE mm.group_id=g.id ORDER BY mm.id DESC LIMIT 1) last_author,
          (SELECT MAX(id) FROM messages WHERE group_id=g.id) last_id,
          COALESCE((SELECT MAX(created_at) FROM messages WHERE group_id=g.id), g.created_at) active_at
        FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? AND g.kind='club'
        ORDER BY active_at DESC""", (session["uid"],)).fetchall()
    out = []
    for r in rows:
        out.append(dict(r, members=[m["username"] for m in club_members(r["id"])]))
    return jsonify(clubs=out)

@app.post("/api/clubs")
@login_required
def create_club():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name or len(name) > 60: return jsonify(error="Club name must be 1-60 characters"), 400
    ids = []
    for x in d.get("members") or []:
        try: x = int(x)
        except (TypeError, ValueError): return jsonify(error="Bad member"), 400
        if x != session["uid"] and x not in ids:
            if not is_friend(session["uid"], x): return jsonify(error="You can only add friends to a book club"), 403
            ids.append(x)
    if len(ids) + 1 > MAX_CLUB_MEMBERS: return jsonify(error=f"Book clubs can have up to {MAX_CLUB_MEMBERS} members"), 400
    cid = db().execute("INSERT INTO groups(name, owner_id, kind) VALUES(?,?,'club')", (name, session["uid"])).lastrowid
    for uid in [session["uid"]] + ids:
        db().execute("INSERT OR IGNORE INTO group_members(group_id, user_id) VALUES(?,?)", (cid, uid))
    post_system(cid, f"{session['name']} started the book club")
    db().commit()
    return jsonify(ok=True, id=cid)

@app.get("/api/clubs/<int:cid>")
@login_required
def get_club(cid):
    c = club_or_404(cid)
    if not c: return jsonify(error="No such book club"), 404
    try: after = max(0, int(request.args.get("after") or 0))
    except ValueError: after = 0
    return jsonify(id=c["id"], name=c["name"], members=club_members(cid), messages=msg_rows(cid, after), me=session["uid"])

@app.post("/api/clubs/<int:cid>/messages")
@login_required
def post_message(cid):
    if not club_or_404(cid): return jsonify(error="No such book club"), 404
    body = ((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not body: return jsonify(error="Message is empty"), 400
    if len(body) > MAX_MSG: return jsonify(error=f"Messages can be up to {MAX_MSG} characters"), 400
    mid = db().execute("INSERT INTO messages(group_id, user_id, body) VALUES(?,?,?)", (cid, session["uid"], body)).lastrowid
    db().commit(); return jsonify(ok=True, id=mid)

@app.post("/api/clubs/<int:cid>/members")
@login_required
def add_club_member(cid):
    if not club_or_404(cid): return jsonify(error="No such book club"), 404
    try: uid = int((request.get_json(silent=True) or {}).get("user_id"))
    except (TypeError, ValueError): return jsonify(error="Pick a friend to add"), 400
    if not is_friend(session["uid"], uid): return jsonify(error="You can only add your friends"), 403
    if is_member(cid, uid): return jsonify(ok=True, note="Already in the club")
    if len(club_members(cid)) >= MAX_CLUB_MEMBERS: return jsonify(error=f"Book clubs can have up to {MAX_CLUB_MEMBERS} members"), 400
    db().execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (cid, uid))
    name = db().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()[0]
    post_system(cid, f"{session['name']} added {name}")
    db().commit(); return jsonify(ok=True)

@app.post("/api/clubs/<int:cid>/rename")
@login_required
def rename_club(cid):
    if not club_or_404(cid): return jsonify(error="No such book club"), 404
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name or len(name) > 60: return jsonify(error="Club name must be 1-60 characters"), 400
    db().execute("UPDATE groups SET name=? WHERE id=?", (name, cid))
    post_system(cid, f"{session['name']} renamed the club to “{name}”")
    db().commit(); return jsonify(ok=True)

@app.post("/api/clubs/<int:cid>/leave")
@login_required
def leave_club(cid):
    c = club_or_404(cid)
    if not c: return jsonify(error="No such book club"), 404
    db().execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (cid, session["uid"]))
    rest = club_members(cid)
    if not rest:
        db().execute("DELETE FROM groups WHERE id=?", (cid,))  # last one out: messages cascade
    else:
        if c["owner_id"] == session["uid"]:
            db().execute("UPDATE groups SET owner_id=? WHERE id=?", (rest[0]["id"], cid))
        post_system(cid, f"{session['name']} left the club")
    db().commit(); return jsonify(ok=True)

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
    rows = db().execute("""SELECT b.*, COALESCE(s.name, COALESCE(u.shelf_name, u.username || '''s shelf')) shelf FROM books b JOIN users u ON u.id=b.owner_id LEFT JOIN shelves s ON s.id=b.shelf_id
        WHERE """ + BOOK_MANAGED + " ORDER BY b.shelf_id IS NOT NULL, s.name, b.location, b.title", dict(me=session["uid"])).fetchall()
    mr = my_ratings()
    return jsonify([dict(r, my_rating=mr.get(work_key(r["title"], r["author"]))) for r in rows])

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
    rows = db().execute("""SELECT b.*, u.username owner, s.name shelf, COALESCE(u.shelf_name, u.username || '''s shelf') pshelf FROM books b JOIN users u ON u.id=b.owner_id
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
            name = r["shelf"].split(" (")[0] if r["shelf"] else r["pshelf"]
            g_["friends"].append(dict(id=r["id"], owner=name, shared=r["shelf_id"] is not None, location=r["location"]))
    mr, net = my_ratings(), {}
    for r in network_ratings(): net.setdefault(r["work_key"], []).append(r["stars"])
    for g_ in groups.values():
        k = work_key(g_["title"], g_["author"]); n = net.get(k, [])
        g_.update(my_rating=mr.get(k), net_avg=round(sum(n) / len(n), 1) if n else None, net_count=len(n))
    return jsonify(query=q, results=list(groups.values()))

# ---------- ratings & suggestions ----------
@app.post("/api/ratings")
@login_required
def rate():
    d = request.get_json(silent=True) or {}
    t = (d.get("title") or "").strip()
    if not t: return jsonify(error="Title is required"), 400
    try: st = int(d.get("stars"))
    except (TypeError, ValueError): return jsonify(error="Stars must be 1-5 (0 clears)"), 400
    if not 0 <= st <= 5: return jsonify(error="Stars must be 1-5 (0 clears)"), 400
    a = (d.get("author") or "").strip() or None
    k = work_key(t, a)
    if st == 0:
        db().execute("DELETE FROM ratings WHERE user_id=? AND work_key=?", (session["uid"], k))
    else:
        db().execute("""INSERT INTO ratings(user_id, work_key, title, author, cover_url, stars) VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id, work_key) DO UPDATE SET stars=excluded.stars, rated_at=CURRENT_TIMESTAMP,
            cover_url=COALESCE(excluded.cover_url, ratings.cover_url)""", (session["uid"], k, t, a, d.get("cover_url") or None, st))
    db().commit(); return jsonify(ok=True, stars=st or None)

@app.get("/api/suggestions")
@login_required
def suggestions():
    """Suggest books from your network, scored by:
      1. taste-matching: people who rated books like you did; their 4-5★ books push up, 1-2★ push down;
      2. author affinity: other books by authors you rated well (or badly);
      3. a small boost for books someone in your network can lend you right now.
    Books you own, manage or have already rated are never suggested."""
    me_ = session["uid"]
    mine = {r["work_key"]: dict(r) for r in db().execute("SELECT * FROM ratings WHERE user_id=?", (me_,))}
    owned = {work_key(r["title"], r["author"]) for r in db().execute("SELECT b.title, b.author FROM books b WHERE " + BOOK_MANAGED, dict(me=me_))}
    lendable = db().execute("""SELECT b.id, b.title, b.author, b.cover_url, s.name shelf, COALESCE(u.shelf_name, u.username || '''s shelf') pshelf
        FROM books b JOIN users u ON u.id=b.owner_id LEFT JOIN shelves s ON s.id=b.shelf_id
        WHERE """ + BOOK_VISIBLE + " AND NOT COALESCE(" + BOOK_MANAGED + ", 0)", dict(me=me_)).fetchall()  # COALESCE: NULL IN (...) is NULL, not false
    others = network_ratings()

    # 1. how much each person's taste agrees with mine (per shared book: +1 same stars, 0 two apart, -1 four apart)
    by_user = {}
    for r in others: by_user.setdefault(r["user_id"], []).append(r)
    sim, agree_n = {}, {}
    for uid, rs in by_user.items():
        shared = [(mine[r["work_key"]]["stars"], r["stars"]) for r in rs if r["work_key"] in mine]
        agree_n[uid] = len(shared)
        sim[uid] = max(0.0, sum(1 - abs(a - b) / 2 for a, b in shared) / (len(shared) + 1)) if shared else 0.0

    # 2. author affinity from my ratings: +2 for 5★ ... -2 for 1★, averaged per author
    aff, aff_src = {}, {}
    for m in mine.values():
        for au in authors_of(m["author"]):
            aff.setdefault(au, []).append(m["stars"] - 3)
            if m["stars"] >= 4: aff_src.setdefault(au, m["title"])
    aff = {au: sum(v) / len(v) for au, v in aff.items()}

    cand = {}
    def c(k, title, author, cover):
        x = cand.setdefault(k, dict(title=title, author=author, cover_url=cover, score=0.0, reasons={}, copies=[], ratings=[]))
        x["cover_url"] = x["cover_url"] or cover
        return x
    for r in others:
        k = r["work_key"]
        if k in mine or k in owned: continue
        x = c(k, r["title"], r["author"], r["cover_url"]); x["ratings"].append(r["stars"])
        w = 0.25 + sim[r["user_id"]]  # everyone counts a little; people who share your taste count a lot more
        x["score"] += w * (r["stars"] - 3)
        if r["stars"] >= 4:
            if sim[r["user_id"]] >= 0.3 and agree_n[r["user_id"]]:
                x["reasons"].setdefault("taste", f"{r['username']} rated it {r['stars']}★ and shares your taste on {agree_n[r['user_id']]} book{'s' * (agree_n[r['user_id']] > 1)}")
            else:
                x["reasons"].setdefault("liked", f"{r['username']} rated it {r['stars']}★")
    for b in lendable:
        k = work_key(b["title"], b["author"])
        if k in mine or k in owned: continue
        x = c(k, b["title"], b["author"], b["cover_url"])
        x["copies"].append(dict(id=b["id"], owner=b["shelf"].split(" (")[0] if b["shelf"] else b["pshelf"]))
    for k, x in cand.items():
        best = max((aff[a] for a in authors_of(x["author"]) if a in aff), default=None)
        if best is not None:
            x["score"] += 1.5 * best
            src = next((aff_src[a] for a in authors_of(x["author"]) if a in aff_src), None)
            if best > 0 and src: x["reasons"].setdefault("author", f"Because you liked “{src}”")
        if x["copies"] and x["score"] > 0: x["score"] += 0.3
    out = []
    for x in sorted(cand.values(), key=lambda x: -x["score"]):
        if x["score"] <= 0 or not x["reasons"]: continue
        rs = x.pop("ratings"); x["net_avg"] = round(sum(rs) / len(rs), 1) if rs else None; x["net_count"] = len(rs)
        x["reasons"] = [x["reasons"][k] for k in ("author", "taste", "liked") if k in x["reasons"]][:2]
        x["copies"] = x["copies"][:3]; x["score"] = round(x["score"], 2)
        out.append(x)
        if len(out) >= 12: break
    return jsonify(suggestions=out, rated=len(mine))

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
    q = q.replace("ou.username owner", "COALESCE(s.name, ou.username) owner, COALESCE(s.name, ou.shelf_name, ou.username || '''s shelf') shelf").replace(
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
