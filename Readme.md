# OpenShelf — Online Book Review System
time : 1800h 
A straightforward Flask + MySQL book discovery and review website for college submission and portfolio use.

## Feature checklist

| Feature | Status |
| --- | --- |
| Project skeleton and homepage layout | Implemented |
| Database connection and authentication | Implemented |
| Book catalogue, search, filters, pagination | Implemented |
| Book details, reviews, ratings and moderation | Implemented |
| My Reviews, reading list and editable reader profile | Implemented |
| Admin book management | Implemented |
| Comments, helpful votes and recommendations | Pending |

## Setup (Windows PowerShell)

1. Open PowerShell in the project folder.
2. Create and activate a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

If PowerShell blocks scripts, run this once for your user account, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

3. Install packages:

```powershell
pip install -r requirements.txt
```

4. Copy the example environment file and edit it with your own values. Set `DB_USER` and `DB_PASSWORD` to a MySQL account that can access the local database and create/alter its tables; do not assume the account is `root`. Keep `DB_HOST=127.0.0.1` and `DB_PORT=3306` for the usual local MySQL setup, and use the database name you create below for `DB_NAME`. Generate a unique secret with `python -c "import secrets; print(secrets.token_hex(32))"` and set it as `FLASK_SECRET_KEY`. Replace the placeholder before running the app, and do not commit `.env`.

```powershell
copy .env.example .env
```

`APP_ENV` must be `development` for local work or `production` on the hosting platform. OpenShelf will not start if `FLASK_SECRET_KEY` is missing. In production, session cookies are marked Secure, HttpOnly, and SameSite=Lax.

5. Create the local MySQL database named by `DB_NAME` if it does not already exist. With the example `DB_NAME=openshelf_local`, run this in PowerShell, replacing the admin username with an account that has permission to create databases. MySQL will prompt for that account's password:

```powershell
$mysqlAdminUser = Read-Host "Local MySQL administrator username"
mysql --user "$mysqlAdminUser" --password -e "CREATE DATABASE IF NOT EXISTS openshelf_local CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

If the database already exists, this command leaves its data intact. If `mysql` is not on `PATH`, run the same `CREATE DATABASE IF NOT EXISTS ...` statement in MySQL Workbench or another local MySQL client. The application account configured in `.env` must be allowed to read/write the tables and create/alter them for initialization.

Then create or update the tables (safe to re-run; it does not drop or reset existing data):

```powershell
python init_db.py
```

Changing `.env` only changes which database OpenShelf connects to. It does not copy books, users, reviews, ratings, or uploaded files from FreeDB; the new local database starts with an empty catalogue.

For a fresh production deployment, first create an empty MySQL database through the hosting provider's dashboard or an administrator account. Use `utf8mb4` with `utf8mb4_unicode_ci` if the provider lets you choose its character set and collation. Set `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` in the hosting platform's environment settings so `DB_NAME` is that database and the account can create and alter tables within it. Then run `python init_db.py` once as a release/setup step. It connects directly to the configured database and creates missing tables and additive columns; it does not create a database, drop tables, truncate records, or reset data. Do not point production settings at your local database.

The setup applies only additive profile and guidelines-acceptance columns. When updating an existing database, run `python init_db.py` after deploying this version and before enabling new registrations so it adds `guidelines_accepted_at` safely. Existing accounts have a `NULL` guidelines acceptance timestamp and can continue logging in; agreement is required only for new public registrations. Existing accounts, reviews, books and uploaded covers are preserved.

6. Create the first administrator (public registration cannot do this):

```powershell
python create_admin.py shelfadmin admin@example.com ChangeMe123
```

7. Run the local development server:

```powershell
python app.py
```

8. Open http://127.0.0.1:5000 in a browser.

## Production startup

The `Procfile` starts the app with Gunicorn on the hosting platform's `$PORT`. Configure `APP_ENV=production` and a unique `FLASK_SECRET_KEY` in the platform's environment/secrets settings; do not upload `.env`. The same production command can be run on a Linux host with `gunicorn --bind 0.0.0.0:$PORT app:app`. Keep `python app.py` for local development.

## Adding books

If the catalogue is empty, log in as an administrator, open **Admin Dashboard**, and use **Add a book**. Normal users cannot create books.

New reviews stay **pending** until an administrator approves them. Only approved reviews appear on book pages and in public average ratings.

## Bulk book import

Administrators can open **Bulk import books** from the admin dashboard. Import previews are private to the admin who created them and expire after 30 minutes. Existing books are never updated or overwritten.

### CSV and cover ZIP

Download the sample CSV from the import page. Use UTF-8 CSV with these required headers:

```csv
Title,Author,Genre
"The Left Hand of Darkness","Ursula K. Le Guin","Science Fiction"
```

Optional supported headers are `ISBN`, `Description`, `Publisher`, `Publication Year`, `Cover Filename`, and `Cover URL`. The database stores a publication year, not a full publication date. Rows with missing required values or values outside the database column limits are shown as errors in the preview.

CSV uploads are limited to 2 MB and 50 book rows. A cover ZIP is optional and is limited to 50 MB, 500 archive entries, 2 MB per file, and 100 MB of uncompressed content. Include JPG/JPEG, PNG, WEBP, or GIF images. Set `Cover Filename` to an image basename in the archive (matching is case-insensitive); if that column is empty, an image whose filename stem matches the normalized book title is used when the match is unique. ZIP paths are validated and are never extracted to disk. A cover URL must use HTTPS and resolve to public internet addresses; redirects, downloaded size (2 MB), and image signatures are checked. Invalid or unavailable covers do not prevent importing otherwise valid metadata.

### Google Books

Use the admin page's Google Books search by title, author, ISBN, or general query, select results from the displayed page, and preview before confirming. Searches are performed by the Flask server with timeouts and bounded results; no API key is required for basic use. To configure an optional key, set `GOOGLE_BOOKS_API_KEY` in the hosting environment (or in the local, untracked `.env`). Never place a real key in source control. The feature requests at most 10 results per API page; select up to 10 Google Books results at a time. CSV imports allow up to 50 rows. Google records without a category use the existing required genre field value `Uncategorized`.

### Duplicates, storage, and tests

Imports skip duplicates with a matching normalized ISBN or normalized title and author. Duplicate checks run again at confirmation to catch books added after preview. Each book insert uses its own database transaction; a failed row is rolled back, counted as failed, and any newly copied cover for that row is removed. Other valid rows in the batch can still import. No existing book or its cover is modified.

Imported covers use the existing `UPLOAD_FOLDER` convention and generated filenames. `UPLOAD_FOLDER` can be set to an absolute path or a path relative to the project; the default is `uploads/`. The project stores cover files on the local filesystem, which may be ephemeral or non-shared on hosting platforms. For production persistence, configure a durable mounted volume and set `UPLOAD_FOLDER` to its path. No remote storage service is configured by this feature; retain backups of both the MySQL database and the upload directory.

Run the automated tests with:

```powershell
python -m unittest discover -s tests -v
```

## Project files

- `app.py` — Flask routes
- `db.py` — MySQL connection helpers
- `schema.sql` — database tables
- `init_db.py` — applies the schema without dropping data
- `create_admin.py` — creates the first admin account
- `templates/` — HTML pages
- `static/` — CSS, JavaScript and images
- `uploads/` — locally uploaded book covers and profile pictures
