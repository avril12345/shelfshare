"""Usage: python make_reset_link.py <username>  -> prints a one-time set-password URL valid 48h."""
import os, sys, secrets, sqlite3
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SHELFSHARE_DB", os.path.join(BASE, "data", "shelfshare.db"))
SITE = os.environ.get("SHELFSHARE_URL", "https://avrilw.pythonanywhere.com")
if len(sys.argv) != 2: sys.exit(__doc__)
con = sqlite3.connect(DB)
con.execute("""CREATE TABLE IF NOT EXISTS reset_tokens(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, used INTEGER DEFAULT 0)""")
u = con.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (sys.argv[1],)).fetchone()
if not u: sys.exit(f"No user named {sys.argv[1]!r}")
tok = secrets.token_urlsafe(24)
con.execute("INSERT INTO reset_tokens(token,user_id,expires_at) VALUES(?,?,datetime('now','+48 hours'))", (tok, u[0]))
con.commit()
print(f"{SITE}/set-password?token={tok}")
