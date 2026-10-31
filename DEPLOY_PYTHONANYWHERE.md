# Deploying ShelfShare on PythonAnywhere (free plan)

## 1. Get the code (pick one)
**A. Upload zip (easiest, no token):** Files tab → upload `shelfshare.zip` to `/home/YOURNAME/`, then in a Bash console:
    cd ~ && unzip shelfshare.zip -d shelfshare
**B. Clone the private repo:** create a GitHub *fine-grained* token (Settings → Developer settings → Fine-grained tokens), repo access = only `avril12345/shelfshare`, permission Contents: **Read-only**. Then:
    git clone https://github.com/avril12345/shelfshare.git ~/shelfshare
  (use the token as the password when prompted; don't save it in files).

## 2. Virtualenv + install
    cd ~/shelfshare
    python3.10 -m venv ~/.venvs/shelfshare
    ~/.venvs/shelfshare/bin/pip install -r requirements.txt

## 3. Upload the database
Files tab → create `/home/YOURNAME/shelfshare/data/` and upload `shelfshare.db` into it.

## 4. Web app
Web tab → Add a new web app → **Manual configuration** → Python 3.10.
- Virtualenv: `/home/YOURNAME/.venvs/shelfshare`
- Source code: `/home/YOURNAME/shelfshare`
- Edit the WSGI file (link on Web tab), replace contents with:

```python
import os, sys
path = '/home/YOURNAME/shelfshare'
if path not in sys.path:
    sys.path.insert(0, path)
os.environ['SECRET_KEY'] = 'PASTE-A-LONG-RANDOM-STRING'   # python3 -c "import secrets;print(secrets.token_hex(32))"
os.environ['SHELFSHARE_DB'] = '/home/YOURNAME/shelfshare/data/shelfshare.db'
from app import app as application
```
- Turn on **Force HTTPS** (needed for phone camera scanning).

## 5. Reload
Click **Reload** on the Web tab, open `https://YOURNAME.pythonanywhere.com`.
Free plan: click "Run until 3 months from today" / extend button periodically to keep it alive.
