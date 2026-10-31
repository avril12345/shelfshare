"""Seed Av's books from a Notion export (data/notion_books.json) under user 'Av'.
Usage: python import_notion.py [path] [--password PW]
The JSON is a list of {Title, Author, ISBN, Location} rows, as exported from the
Notion Books data source (collection 9839adee-151a-4e1a-8e93-fb665a849d60)."""
import sys, json, sqlite3, secrets, os
from werkzeug.security import generate_password_hash
import app as A

path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else os.path.join(A.BASE, "data", "notion_books.json")
pw = sys.argv[sys.argv.index("--password") + 1] if "--password" in sys.argv else None
rows = json.load(open(path))
con = sqlite3.connect(A.DB); con.executescript(A.SCHEMA)
u = con.execute("SELECT id FROM users WHERE username='Av'").fetchone()
if u: uid = u[0]
else:
    pw = pw or secrets.token_urlsafe(9)
    uid = con.execute("INSERT INTO users(username,password_hash) VALUES('Av',?)", (generate_password_hash(pw),)).lastrowid
    print(f"Created user Av with password: {pw}")
existing = {(t.lower(), (l or "").lower()) for t, l in con.execute("SELECT title,location FROM books WHERE owner_id=?", (uid,))}
n = 0
for r in rows:
    t = (r.get("Title") or "").strip()
    if not t or (t.lower(), (r.get("Location") or "").lower()) in existing: continue
    isbn = "".join(c for c in (r.get("ISBN") or "") if c.isalnum()) or None
    con.execute("INSERT INTO books(owner_id,title,author,isbn,cover_url,location,source) VALUES(?,?,?,?,?,?,'notion')",
        (uid, t, r.get("Author"), isbn, f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg" if isbn else None, r.get("Location")))
    n += 1
con.commit()
print(f"Imported {n} new books for Av ({len(rows)} rows in file)")
