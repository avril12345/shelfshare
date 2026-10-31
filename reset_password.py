"""Set a new ShelfShare password. Run: python reset_password.py Av"""
import os, sys, sqlite3, getpass
from werkzeug.security import generate_password_hash
DB = os.environ.get("SHELFSHARE_DB", os.path.expanduser("~/shelfshare/data/shelfshare.db"))
user = sys.argv[1] if len(sys.argv) > 1 else "Av"
p1 = getpass.getpass(f"New password for {user}: ")
p2 = getpass.getpass("Type it again: ")
if p1 != p2 or len(p1) < 8:
    sys.exit("Passwords didn't match or were under 8 characters. Nothing changed.")
c = sqlite3.connect(DB)
n = c.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(p1), user)).rowcount
c.commit()
print("Password updated. You can log in now." if n else f"No user named {user}. Nothing changed.")
