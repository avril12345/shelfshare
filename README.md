# > SHELFSHARE_

Share your bookshelves with friends so you can borrow instead of buy.
It runs on Flask and SQLite, with one static page and a green-on-black terminal look that works on phones.

## Features
- **Invite-only**: signing up needs a single-use invite code. Logged-in users open the **INVITE** tab to generate codes (up to 10 unused at a time), copy a sign-up link like `/?invite=CODE` that fills the code in, and revoke unused ones. Used codes are marked with who used them and when. The `invites` table records `created_by`, `used_by`, `used_by_name` and `used_at`.
- **Groups (circles)**: users belong to one or more groups (`groups` + `group_members` tables). Av owns the seeded **Book Club** and **Family** groups and is in both. Each invite code is tied to one group, so signing up with it joins that group. Logged-in users can join another group by entering a code on the **GROUPS** tab, which also lists your groups and their members. You only see, search and request books from people who share at least one group with you, and your own books always show. Only members of a group can make codes for it.
- **Migration**: `migrate()` runs at startup. It is additive and idempotent: it creates the new tables, adds `invites.group_id`, seeds the two groups once (`PRAGMA user_version=1`), puts any existing users in Book Club and assigns old unused codes to Book Club. No data is dropped.
- Every book, search, borrow, request and invite API needs a login. Logged-out calls get a 401.
- Sign up and log in. Passwords are hashed with Werkzeug PBKDF2/scrypt, and sessions use signed cookies.
- **Add books by barcode**: the phone camera uses the native `BarcodeDetector` (Android Chrome) or falls back to `html5-qrcode` (iOS Safari and others). ISBN lookup goes to Open Library first, then Google Books. You can also type an ISBN or enter a book manually, and give each book a location.
- **One search box** across everyone's books, matching title, author or ISBN. Results say "You own it — <location>", "<friend> has a copy" or "Nobody has this yet".
- **Request to borrow**: the request is recorded and the owner can mark it OK or NO on the Requests tab. No email is sent.
- **Notion import**: `import_notion.py` seeds `data/notion_books.json` under user **Av**, skipping duplicates.
- **Future AI shelf-photo scanner**: there's a stub in `shelf_scanner.py`, and `POST /api/scan-shelf` currently returns 501.

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python import_notion.py --password 'choose-one'   # optional seed
SECRET_KEY=change-me .venv/bin/gunicorn -w 2 -b 0.0.0.0:8080 app:app
# open http://localhost:8080
```
The first account (Av) is created by `import_notion.py`. Everyone after that needs an invite code from an existing user.

Env vars: `SECRET_KEY` (required in production), `SHELFSHARE_DB` (SQLite path, default `data/shelfshare.db`) and `PORT`.

**Camera note:** browsers only allow camera access over **https** (or localhost). Once the app is deployed, scanning works on phones.

## Deploy so friends can use it
Every option below gives you an https URL. SQLite needs a **persistent disk**, so point `SHELFSHARE_DB` at it.

| Host | Cost | Notes |
|---|---|---|
| **Fly.io** | ~free–$2/mo (small VM + 1 GB volume) | `fly launch`, `fly volumes create data --size 1`, mount at `/data`, `SHELFSHARE_DB=/data/shelfshare.db`. Best fit for SQLite. |
| **Railway** | $5/mo Hobby (includes usage) | Connect the GitHub repo, add a Volume at `/data`, and set the env vars. The Procfile is detected automatically. |
| **Render** | Free web tier has **no persistent disk** (data is wiped on redeploy and it sleeps when idle) | Use the Starter plan ($7/mo) plus a Disk, or swap SQLite for Render's free Postgres (expires after 30 days). |
| **PythonAnywhere** | Free tier | Has a persistent filesystem, so SQLite just works. Your URL is `yourname.pythonanywhere.com`. |

Start command: `gunicorn -w 2 -b 0.0.0.0:$PORT app:app`. Remember to set a long random `SECRET_KEY`.

## Next ideas
- AI shelf-photo scanning: implement `shelf_scanner.read_spines`, then show a confirm-the-titles checklist with a spending cap.
- Let the NFC tags and the Raspberry Pi terminal call `/api/search`.
