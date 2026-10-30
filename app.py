import os
import re
import secrets
import shutil
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from mysql.connector import Error, IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import bulk_import
from bulk_import import (
    GoogleBooksError,
    ImportValidationError,
    create_batch,
    download_cover,
    image_extension,
    load_batch,
    normalized_isbn,
    normalized_text,
    parse_csv,
    remove_batch,
    save_batch,
    search_google_books,
    staged_cover_path,
    store_staged_cover,
    validate_book_record,
    validate_zip_archive,
)
from db import execute_query, fetch_all, fetch_one, get_connection

load_dotenv()

app = Flask(__name__)
APP_ENV = os.getenv("APP_ENV", "").strip().lower()
if APP_ENV not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be set to either 'development' or 'production'.")

secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()
if not secret_key:
    raise RuntimeError("FLASK_SECRET_KEY must be configured before starting OpenShelf.")

app.config.update(
    SECRET_KEY=secret_key,
    DEBUG=APP_ENV == "development",
    SESSION_COOKIE_SECURE=APP_ENV == "production",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=bulk_import.MAX_REQUEST_BYTES,
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,50}$")
ALLOWED_RATINGS = [Decimal("1.0"), Decimal("1.5"), Decimal("2.0"), Decimal("2.5"),
                   Decimal("3.0"), Decimal("3.5"), Decimal("4.0"), Decimal("4.5"),
                   Decimal("5.0")]
COVER_TONES = ["terracotta", "ink", "sage", "lilac", "coral"]
ALLOWED_COVER_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_COVER_BYTES = 2 * 1024 * 1024
MAX_PROFILE_BIO_LENGTH = 500
MAX_PROFILE_TEXT_LENGTH = 120
MAX_READING_GOAL = 500
PAGE_SIZE = 12
_upload_folder_setting = Path(os.getenv("UPLOAD_FOLDER", "uploads"))
UPLOAD_FOLDER = (
    _upload_folder_setting
    if _upload_folder_setting.is_absolute()
    else Path(__file__).parent / _upload_folder_setting
).resolve()
CURRENT_YEAR = datetime.now().year

BOOKS_WITH_STATS = """
    SELECT b.*, stats.avg_rating, stats.review_count
    FROM books b
    LEFT JOIN (
        SELECT r.book_id, AVG(r.rating) AS avg_rating, COUNT(*) AS review_count
        FROM reviews r
        JOIN users u ON u.id = r.user_id
        WHERE r.status = 'approved' AND u.is_active = 1
        GROUP BY r.book_id
    ) stats ON stats.book_id = b.id
"""
REVIEW_SORTS = {
    "newest": "r.created_at DESC",
    "oldest": "r.created_at ASC",
    "highest": "r.rating DESC, r.created_at DESC",
    "lowest": "r.rating ASC, r.created_at DESC",
    "helpful": "helpful_count DESC, r.created_at DESC",
}
READING_STATUSES = {
    "want_to_read": "Want to Read",
    "currently_reading": "Currently Reading",
    "finished": "Finished",
}


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def csrf_ok():
    sent = request.form.get("csrf_token", "")
    saved = session.get("csrf_token", "")
    return bool(sent) and secrets.compare_digest(sent, saved)


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        user = fetch_one(
            """
            SELECT id, username, email, role, is_active, created_at,
                   display_name, bio, favourite_genres, favourite_book,
                   currently_reading, annual_reading_goal, profile_filename
            FROM users WHERE id = %s
            """,
            (user_id,),
        )
        if user and not user.get("is_active", 1):
            session.clear()
            return None
        return user
    except Error as error:
        app.logger.exception("Current-user lookup failed: %s", error)
        raise


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def cover_tone(book_id):
    return COVER_TONES[int(book_id) % len(COVER_TONES)]


def cover_src(book):
    filename = book.get("cover_filename")
    if filename:
        return url_for("uploaded_cover", filename=filename)
    url = book.get("cover_url")
    if url:
        return url
    return None


def format_rating(value):
    if value is None:
        return None
    return f"{float(value):.1f}"


def decorate_book(book):
    if not book:
        return book
    book["cover_tone"] = cover_tone(book["id"])
    book["cover_src"] = cover_src(book)
    book["avg_rating_display"] = format_rating(book.get("avg_rating"))
    book["review_count"] = int(book.get("review_count") or 0)
    return book


def parse_rating(value):
    try:
        rating = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    if rating not in ALLOWED_RATINGS:
        return None
    return rating


def book_stats_row(book_id):
    return fetch_one(BOOKS_WITH_STATS + " WHERE b.id = %s", (book_id,))


def list_genres():
    rows = fetch_all("SELECT DISTINCT genre FROM books ORDER BY genre")
    return [row["genre"] for row in rows]


def list_catalogue_genres():
    rows = fetch_all("SELECT genre FROM books ORDER BY genre")
    genres_by_key = {}
    for row in rows:
        for genre in (part.strip() for part in (row["genre"] or "").split(",")):
            if genre:
                genres_by_key.setdefault(genre.casefold(), genre)
    return sorted(genres_by_key.values(), key=str.casefold)


def query_books(search="", genre="", limit=None, offset=0):
    sql = BOOKS_WITH_STATS + " WHERE 1=1"
    params = []
    if search:
        sql += " AND (b.title LIKE %s OR b.author LIKE %s)"
        like = f"%{search}%"
        params.extend([like, like])
    if genre:
        sql += """
            AND FIND_IN_SET(
                %s,
                REGEXP_REPLACE(TRIM(b.genre), '[[:space:]]*,[[:space:]]*', ',')
            ) > 0
        """
        params.append(genre)
    sql += " ORDER BY b.title ASC"
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params.extend([int(limit), int(offset)])
    return [decorate_book(row) for row in fetch_all(sql, tuple(params))]


def review_sort_clause(sort_key):
    return REVIEW_SORTS.get(sort_key, REVIEW_SORTS["newest"])


def attach_review_extras(reviews, user):
    if not reviews:
        return reviews
    review_ids = [row["id"] for row in reviews]
    placeholders = ", ".join(["%s"] * len(review_ids))
    comments = fetch_all(
        f"""
        SELECT c.id, c.review_id, c.user_id, c.comment_text, c.created_at, u.username
        FROM review_comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.review_id IN ({placeholders})
        ORDER BY c.created_at ASC
        """,
        tuple(review_ids),
    )
    comments_by_review = {}
    for comment in comments:
        comments_by_review.setdefault(comment["review_id"], []).append(comment)
    voted_ids = set()
    if user:
        votes = fetch_all(
            f"""
            SELECT review_id FROM review_votes
            WHERE user_id = %s AND review_id IN ({placeholders})
            """,
            (user["id"], *review_ids),
        )
        voted_ids = {row["review_id"] for row in votes}
    for review in reviews:
        review["comments"] = comments_by_review.get(review["id"], [])
        review["user_voted"] = review["id"] in voted_ids
        review["helpful_count"] = int(review.get("helpful_count") or 0)
    return reviews


def top_rated_books(limit=4):
    rows = fetch_all(
        BOOKS_WITH_STATS
        + """
        WHERE stats.review_count >= 1
        ORDER BY stats.avg_rating DESC, stats.review_count DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [decorate_book(row) for row in rows]


def trending_books(limit=4):
    rows = fetch_all(
        """
        SELECT b.*, stats.avg_rating, stats.review_count,
               COALESCE(recent.recent_count, 0) AS recent_count
        FROM books b
        LEFT JOIN (
            SELECT r.book_id, AVG(r.rating) AS avg_rating, COUNT(*) AS review_count
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            WHERE r.status = 'approved' AND u.is_active = 1
            GROUP BY r.book_id
        ) stats ON stats.book_id = b.id
        LEFT JOIN (
            SELECT r.book_id, COUNT(*) AS recent_count
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            WHERE r.status = 'approved'
              AND u.is_active = 1
              AND r.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY r.book_id
        ) recent ON recent.book_id = b.id
        WHERE COALESCE(recent.recent_count, 0) > 0
        ORDER BY recent.recent_count DESC, stats.avg_rating DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [decorate_book(row) for row in rows]


def recent_books(limit=8):
    rows = fetch_all(
        BOOKS_WITH_STATS + " ORDER BY b.created_at DESC LIMIT %s",
        (limit,),
    )
    return [decorate_book(row) for row in rows]


def popular_books(limit=4, exclude_ids=None):
    sql = BOOKS_WITH_STATS + " WHERE COALESCE(stats.review_count, 0) > 0"
    params = []
    if exclude_ids:
        placeholders = ", ".join(["%s"] * len(exclude_ids))
        sql += f" AND b.id NOT IN ({placeholders})"
        params.extend(exclude_ids)
    sql += " ORDER BY stats.review_count DESC, stats.avg_rating DESC LIMIT %s"
    params.append(limit)
    return [decorate_book(row) for row in fetch_all(sql, tuple(params))]


def recommended_books_for(user, limit=4):
    """Books in genres the user rated 4+ (approved). Fallback: popular books."""
    exclude_rows = fetch_all(
        """
        SELECT book_id FROM reviews WHERE user_id = %s
        UNION
        SELECT book_id FROM reading_list WHERE user_id = %s
        """,
        (user["id"], user["id"]),
    )
    exclude_ids = [row["book_id"] for row in exclude_rows]
    liked = fetch_all(
        """
        SELECT b.genre
        FROM reviews r
        JOIN books b ON b.id = r.book_id
        WHERE r.user_id = %s AND r.status = 'approved' AND r.rating >= 4
        """,
        (user["id"],),
    )
    genres = list({row["genre"] for row in liked})
    if genres:
        placeholders = ", ".join(["%s"] * len(genres))
        sql = BOOKS_WITH_STATS + f" WHERE b.genre IN ({placeholders})"
        params = list(genres)
        if exclude_ids:
            exclude_ph = ", ".join(["%s"] * len(exclude_ids))
            sql += f" AND b.id NOT IN ({exclude_ph})"
            params.extend(exclude_ids)
        sql += " ORDER BY COALESCE(stats.avg_rating, 0) DESC, b.title ASC LIMIT %s"
        params.append(limit)
        rows = fetch_all(sql, tuple(params))
        if rows:
            return [decorate_book(row) for row in rows], "genre"
    fallback = popular_books(limit, exclude_ids)
    if fallback:
        return fallback, "popular"
    return recent_books(limit), "recent"


def count_books(search="", genre=""):
    sql = "SELECT COUNT(*) AS total FROM books b WHERE 1=1"
    params = []
    if search:
        sql += " AND (b.title LIKE %s OR b.author LIKE %s)"
        like = f"%{search}%"
        params.extend([like, like])
    if genre:
        sql += """
            AND FIND_IN_SET(
                %s,
                REGEXP_REPLACE(TRIM(b.genre), '[[:space:]]*,[[:space:]]*', ',')
            ) > 0
        """
        params.append(genre)
    row = fetch_one(sql, tuple(params))
    return int(row["total"] if row else 0)


def allowed_cover_bytes(header):
    if header.startswith(b"\xff\xd8\xff"):
        return True
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return True
    if header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True
    return False


def save_cover_file(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None
    filename = secure_filename(file_storage.filename)
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_COVER_EXTS:
        return None, "Cover images must be JPG, PNG, GIF or WEBP."
    data = file_storage.read(MAX_COVER_BYTES + 1)
    if len(data) > MAX_COVER_BYTES:
        return None, "Cover images must be 2 MB or smaller."
    if not allowed_cover_bytes(data[:16]):
        return None, "That file does not look like a valid image."
    stored_name = f"{uuid.uuid4().hex}{extension}"
    UPLOAD_FOLDER.mkdir(exist_ok=True)
    (UPLOAD_FOLDER / stored_name).write_bytes(data)
    return stored_name, None


def delete_cover_file(filename):
    if not filename:
        return
    path = UPLOAD_FOLDER / secure_filename(filename)
    if path.is_file() and path.parent == UPLOAD_FOLDER:
        path.unlink()


def validate_book_form(form):
    title = form.get("title", "").strip()
    author = form.get("author", "").strip()
    genre = form.get("genre", "").strip()
    description = form.get("description", "").strip()
    isbn = form.get("isbn", "").strip()
    publisher = form.get("publisher", "").strip()
    year_raw = form.get("publication_year", "").strip()
    cover_url = form.get("cover_url", "").strip()

    if not title or len(title) > 200:
        return None, "Enter a title of up to 200 characters."
    if not author or len(author) > 200:
        return None, "Enter an author of up to 200 characters."
    if not genre or len(genre) > 80:
        return None, "Enter a genre of up to 80 characters."
    if isbn and len(isbn) > 20:
        return None, "ISBN must be 20 characters or fewer."
    if publisher and len(publisher) > 200:
        return None, "Publisher must be 200 characters or fewer."
    if cover_url and not (cover_url.startswith("http://") or cover_url.startswith("https://")):
        return None, "Cover URL must start with http:// or https://."
    if cover_url and len(cover_url) > 500:
        return None, "Cover URL is too long."

    year = None
    if year_raw:
        if not year_raw.isdigit() or not (1000 <= int(year_raw) <= CURRENT_YEAR + 1):
            return None, "Enter a valid publication year."
        year = int(year_raw)

    return {
        "title": title,
        "author": author,
        "genre": genre,
        "description": description or None,
        "isbn": isbn or None,
        "publisher": publisher or None,
        "publication_year": year,
        "cover_url": cover_url or None,
    }, None


def empty_book_form():
    return {
        "title": "",
        "author": "",
        "genre": "",
        "description": "",
        "isbn": "",
        "publisher": "",
        "publication_year": "",
        "cover_url": "",
    }


def book_to_form(book):
    return {
        "title": book.get("title") or "",
        "author": book.get("author") or "",
        "genre": book.get("genre") or "",
        "description": book.get("description") or "",
        "isbn": book.get("isbn") or "",
        "publisher": book.get("publisher") or "",
        "publication_year": book.get("publication_year") or "",
        "cover_url": book.get("cover_url") or "",
    }


@app.context_processor
def inject_globals():
    user = get_current_user()
    return {
        "current_user": user,
        "is_logged_in": user is not None,
        "is_admin": bool(user and user["role"] == "admin"),
        "csrf_token": get_csrf_token(),
    }


@app.errorhandler(403)
def forbidden(_error):
    return render_template("forbidden.html", current_page=""), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("not_found.html", current_page=""), 404


@app.route("/")
def home():
    user = get_current_user()
    try:
        featured = top_rated_books(4)
        trending = trending_books(4)
        latest = recent_books(8)
        genres = list_genres()
        recommended, recommend_reason = [], None
        if user:
            recommended, recommend_reason = recommended_books_for(user, 4)
        elif featured:
            recommended, recommend_reason = featured, "popular"
    except Error as error:
        app.logger.exception("Homepage database error: %s", error)
        flash("Could not load books from the database.", "danger")
        featured, trending, latest, genres, recommended, recommend_reason = [], [], [], [], [], None
    return render_template(
        "home.html",
        featured_books=featured,
        trending_books=trending,
        books=latest,
        recommended_books=recommended,
        recommend_reason=recommend_reason,
        genres=genres,
        current_page="home",
    )


@app.route("/books")
def books():
    search = request.args.get("q", "").strip()
    genre = request.args.get("genre", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    offset = (page - 1) * PAGE_SIZE
    try:
        total = count_books(search, genre)
        book_rows = query_books(search, genre, PAGE_SIZE, offset)
        genres = list_catalogue_genres()
    except Error as error:
        app.logger.exception("Catalogue database error: %s", error)
        flash("Could not load the book catalogue.", "danger")
        total, book_rows, genres = 0, [], []
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    return render_template(
        "books.html",
        books=book_rows,
        genres=genres,
        search=search,
        selected_genre=genre,
        page=page,
        total_pages=total_pages,
        total=total,
        current_page="books",
    )


@app.route("/guidelines")
def guidelines():
    return render_template("guidelines.html", current_page="guidelines")


@app.route("/books/<int:book_id>")
def book_detail(book_id):
    user = get_current_user()
    sort_key = request.args.get("sort", "newest")
    if sort_key not in REVIEW_SORTS:
        sort_key = "newest"
    try:
        book = book_stats_row(book_id)
        if not book:
            abort(404)
        order_sql = review_sort_clause(sort_key)
        reviews = fetch_all(
            f"""
            SELECT r.id, r.user_id, r.rating, r.review_text, r.is_spoiler, r.created_at,
                   u.username, COALESCE(v.helpful_count, 0) AS helpful_count
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            LEFT JOIN (
                SELECT review_id, COUNT(*) AS helpful_count
                FROM review_votes
                GROUP BY review_id
            ) v ON v.review_id = r.id
            WHERE r.book_id = %s AND r.status = 'approved' AND u.is_active = 1
            ORDER BY {order_sql}
            """,
            (book_id,),
        )
        reviews = attach_review_extras(reviews, user)
        own_review = None
        list_entry = None
        if user:
            own_review = fetch_one(
                """
                SELECT id, rating, review_text, is_spoiler, status, created_at, updated_at
                FROM reviews
                WHERE user_id = %s AND book_id = %s
                """,
                (user["id"], book_id),
            )
            list_entry = fetch_one(
                "SELECT id, status FROM reading_list WHERE user_id = %s AND book_id = %s",
                (user["id"], book_id),
            )
    except Error as error:
        app.logger.exception("Book detail database error: %s", error)
        flash("Could not load this book.", "danger")
        return redirect(url_for("books"))

    return render_template(
        "book_detail.html",
        book=decorate_book(book),
        reviews=reviews,
        own_review=own_review,
        list_entry=list_entry,
        reading_statuses=READING_STATUSES,
        ratings=ALLOWED_RATINGS,
        sort_key=sort_key,
        current_page="books",
    )


@app.route("/books/<int:book_id>/reviews", methods=["POST"])
@login_required
def save_review(book_id):
    user = get_current_user()
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("book_detail", book_id=book_id))

    book = fetch_one("SELECT id FROM books WHERE id = %s", (book_id,))
    if not book:
        abort(404)

    rating = parse_rating(request.form.get("rating", ""))
    text = request.form.get("review_text", "").strip()
    is_spoiler = 1 if request.form.get("is_spoiler") else 0
    if rating is None:
        flash("Choose a rating from 1 to 5 in half-star steps.", "danger")
        return redirect(url_for("book_detail", book_id=book_id))
    if len(text) < 10:
        flash("Please write a review of at least 10 characters.", "danger")
        return redirect(url_for("book_detail", book_id=book_id))
    if len(text) > 5000:
        flash("Reviews must be 5000 characters or fewer.", "danger")
        return redirect(url_for("book_detail", book_id=book_id))

    existing = fetch_one(
        "SELECT id FROM reviews WHERE user_id = %s AND book_id = %s",
        (user["id"], book_id),
    )
    try:
        if existing:
            execute_query(
                """
                UPDATE reviews
                SET rating = %s, review_text = %s, is_spoiler = %s, status = 'pending'
                WHERE id = %s AND user_id = %s
                """,
                (str(rating), text, is_spoiler, existing["id"], user["id"]),
            )
            flash("Your review was updated and is waiting for approval.", "success")
        else:
            execute_query(
                """
                INSERT INTO reviews (user_id, book_id, rating, review_text, is_spoiler, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
                """,
                (user["id"], book_id, str(rating), text, is_spoiler),
            )
            flash("Your review was submitted and is waiting for approval.", "success")
    except IntegrityError:
        flash("You already have a review for this book.", "danger")
    except Error as error:
        app.logger.exception("Review save error: %s", error)
        flash("Could not save your review.", "danger")
    return redirect(url_for("book_detail", book_id=book_id))


@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@login_required
def delete_review(review_id):
    user = get_current_user()
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("my_reviews"))

    review = fetch_one("SELECT id, user_id, book_id FROM reviews WHERE id = %s", (review_id,))
    if not review:
        abort(404)
    if review["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    try:
        execute_query("DELETE FROM reviews WHERE id = %s", (review_id,))
        flash("The review was deleted.", "success")
    except Error as error:
        app.logger.exception("Review delete error: %s", error)
        flash("Could not delete the review.", "danger")
    next_page = request.form.get("next")
    if next_page == "admin":
        return redirect(url_for("admin_dashboard"))
    if next_page == "book":
        return redirect(url_for("book_detail", book_id=review["book_id"]))
    return redirect(url_for("my_reviews"))


@app.route("/books/<int:book_id>/reading-list", methods=["POST"])
@login_required
def toggle_reading_list(book_id):
    user = get_current_user()
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("book_detail", book_id=book_id))
    book = fetch_one("SELECT id FROM books WHERE id = %s", (book_id,))
    if not book:
        abort(404)
    action = request.form.get("action", "add")
    status = request.form.get("status", "want_to_read")
    if action not in {"add", "update", "remove"}:
        flash("Choose a valid reading-list action.", "danger")
        return redirect(url_for("book_detail", book_id=book_id))
    if action != "remove" and status not in READING_STATUSES:
        flash("Choose a valid reading-list status.", "danger")
        return redirect(url_for("book_detail", book_id=book_id))
    try:
        if action == "remove":
            execute_query(
                "DELETE FROM reading_list WHERE user_id = %s AND book_id = %s",
                (user["id"], book_id),
            )
            flash("Removed from your reading list.", "success")
        else:
            execute_query(
                """
                INSERT INTO reading_list (user_id, book_id, status)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE status = VALUES(status)
                """,
                (user["id"], book_id, status),
            )
            flash("Reading list updated.", "success")
    except Error as error:
        app.logger.exception("Reading list error: %s", error)
        flash("Could not update your reading list.", "danger")
    next_page = request.form.get("next")
    if next_page == "list":
        return redirect(url_for("reading_list", status=request.form.get("filter_status", "")))
    return redirect(url_for("book_detail", book_id=book_id, sort=request.args.get("sort", "")))


@app.route("/my-reviews")
@login_required
def my_reviews():
    user = get_current_user()
    try:
        reviews = fetch_all(
            """
            SELECT r.id, r.rating, r.review_text, r.is_spoiler, r.status,
                   r.created_at, r.updated_at, b.id AS book_id, b.title, b.author
            FROM reviews r
            JOIN books b ON b.id = r.book_id
            WHERE r.user_id = %s
            ORDER BY r.updated_at DESC
            """,
            (user["id"],),
        )
    except Error as error:
        app.logger.exception("My reviews error: %s", error)
        flash("Could not load your reviews.", "danger")
        reviews = []
    return render_template("my_reviews.html", reviews=reviews, current_page="my_reviews")


@app.route("/reading-list")
@login_required
def reading_list():
    user = get_current_user()
    status_filter = request.args.get("status", "").strip()
    if status_filter and status_filter not in READING_STATUSES:
        flash("Choose a valid reading-list status.", "warning")
        return redirect(url_for("reading_list"))
    sql = """
        SELECT b.*, stats.avg_rating, stats.review_count, rl.status AS list_status
        FROM books b
        LEFT JOIN (
            SELECT r.book_id, AVG(r.rating) AS avg_rating, COUNT(*) AS review_count
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            WHERE r.status = 'approved' AND u.is_active = 1
            GROUP BY r.book_id
        ) stats ON stats.book_id = b.id
        JOIN reading_list rl ON rl.book_id = b.id
        WHERE rl.user_id = %s
    """
    params = [user["id"]]
    if status_filter in READING_STATUSES:
        sql += " AND rl.status = %s"
        params.append(status_filter)
    sql += " ORDER BY rl.created_at DESC"
    try:
        books = fetch_all(sql, tuple(params))
        for book in books:
            decorate_book(book)
            book["reading_status"] = book.get("list_status")
            book["reading_status_label"] = READING_STATUSES.get(book.get("list_status"), "")
    except Error as error:
        app.logger.exception("Reading list error: %s", error)
        flash("Could not load your reading list.", "danger")
        books = []
    return render_template(
        "reading_list.html",
        books=books,
        reading_statuses=READING_STATUSES,
        status_filter=status_filter,
        current_page="reading_list",
    )


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = get_current_user()
    fields = (
        "display_name",
        "bio",
        "favourite_genres",
        "favourite_book",
        "currently_reading",
        "annual_reading_goal",
    )

    def profile_form():
        form = {field: user.get(field) or "" for field in fields}
        form["email"] = user["email"]
        return form

    if request.method == "POST":
        if not csrf_ok():
            flash("The form expired. Please try again.", "warning")
            return redirect(url_for("profile"))
        email = request.form.get("email", "").strip().lower()
        form = {field: request.form.get(field, "").strip() for field in fields}
        form["email"] = email
        form["display_name"] = form["display_name"] or None
        form["bio"] = form["bio"] or None
        form["favourite_genres"] = form["favourite_genres"] or None
        form["favourite_book"] = form["favourite_book"] or None
        form["currently_reading"] = form["currently_reading"] or None

        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 120:
            flash("Please enter a valid email address of 120 characters or fewer.", "danger")
            return render_template("profile.html", form=form, current_page="profile")
        if form["display_name"] and len(form["display_name"]) > 80:
            flash("Display name must be 80 characters or fewer.", "danger")
            return render_template("profile.html", form=form, current_page="profile")
        if form["bio"] and len(form["bio"]) > MAX_PROFILE_BIO_LENGTH:
            flash("Bio must be 500 characters or fewer.", "danger")
            return render_template("profile.html", form=form, current_page="profile")
        if form["favourite_genres"] and len(form["favourite_genres"]) > 255:
            flash("Favourite genres must be 255 characters or fewer.", "danger")
            return render_template("profile.html", form=form, current_page="profile")
        for field in ("favourite_book", "currently_reading"):
            if form[field] and len(form[field]) > MAX_PROFILE_TEXT_LENGTH:
                flash("Book titles must be 120 characters or fewer.", "danger")
                return render_template("profile.html", form=form, current_page="profile")

        goal_raw = form["annual_reading_goal"]
        goal = None
        if goal_raw:
            if not goal_raw.isdigit() or not (1 <= int(goal_raw) <= MAX_READING_GOAL):
                flash("Annual reading goal must be between 1 and 500 books.", "danger")
                return render_template("profile.html", form=form, current_page="profile")
            goal = int(goal_raw)

        filename, file_error = save_cover_file(request.files.get("profile_picture"))
        if file_error:
            flash(file_error, "danger")
            return render_template("profile.html", form=form, current_page="profile")

        remove_picture = request.form.get("remove_profile_picture") == "1"
        profile_filename = filename or (
            None if remove_picture else user.get("profile_filename")
        )
        try:
            execute_query(
                """
                UPDATE users
                SET email = %s, display_name = %s, bio = %s, favourite_genres = %s,
                    favourite_book = %s, currently_reading = %s,
                    annual_reading_goal = %s, profile_filename = %s
                WHERE id = %s
                """,
                (
                    email, form["display_name"], form["bio"], form["favourite_genres"],
                    form["favourite_book"], form["currently_reading"], goal,
                    profile_filename, user["id"],
                ),
            )
            if (filename or remove_picture) and user.get("profile_filename"):
                delete_cover_file(user["profile_filename"])
            flash("Your profile was updated.", "success")
            return redirect(url_for("profile"))
        except IntegrityError:
            delete_cover_file(filename)
            flash("That email is already in use.", "danger")
        except Error as error:
            delete_cover_file(filename)
            app.logger.exception("Profile update error: %s", error)
            flash("Could not update your profile.", "danger")
        form["annual_reading_goal"] = goal_raw
        return render_template("profile.html", form=form, current_page="profile")
    return render_template(
        "profile.html",
        form=profile_form(),
        current_page="profile",
    )


@app.route("/uploads/<path:filename>")
def uploaded_cover(filename):
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != Path(filename).name:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, safe_name)


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user():
        return redirect(url_for("home"))

    form = {"username": "", "email": ""}
    guidelines_accepted = False
    if request.method == "POST":
        if not csrf_ok():
            flash("The form expired. Please try again.", "warning")
            return redirect(url_for("register"))

        form["username"] = request.form.get("username", "").strip()
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        guidelines_accepted = request.form.get("guidelines_agreement") == "1"

        if not guidelines_accepted:
            flash("You must accept the Community Guidelines to create an account.", "danger")
            return render_template(
                "register.html",
                form=form,
                guidelines_accepted=guidelines_accepted,
                current_page="register",
            )

        error = validate_registration(form["username"], form["email"], password, confirm)
        if error:
            flash(error, "danger")
            return render_template(
                "register.html",
                form=form,
                guidelines_accepted=guidelines_accepted,
                current_page="register",
            )

        try:
            execute_query(
                """
                INSERT INTO users
                    (username, email, password_hash, role, guidelines_accepted_at)
                VALUES (%s, %s, %s, 'user', CURRENT_TIMESTAMP)
                """,
                (form["username"], form["email"], generate_password_hash(password)),
            )
        except IntegrityError:
            flash("That username or email is already registered.", "danger")
            return render_template(
                "register.html",
                form=form,
                guidelines_accepted=guidelines_accepted,
                current_page="register",
            )
        except Error as error:
            app.logger.exception("Registration database error: %s", error)
            flash("Could not reach the database. Please try again later.", "danger")
            return render_template(
                "register.html",
                form=form,
                guidelines_accepted=guidelines_accepted,
                current_page="register",
            )

        flash("Account created. You can log in now.", "success")
        return redirect(url_for("login"))

    return render_template(
        "register.html",
        form=form,
        guidelines_accepted=guidelines_accepted,
        current_page="register",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user():
        return redirect(url_for("home"))

    if request.method == "POST":
        if not csrf_ok():
            flash("The form expired. Please try again.", "warning")
            return redirect(url_for("login"))

        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        if not identifier or not password:
            flash("Enter your username or email and password.", "danger")
            return render_template("login.html", identifier=identifier, current_page="login")

        try:
            user = fetch_one(
                """
                SELECT id, username, email, password_hash, role, is_active
                FROM users
                WHERE username = %s OR email = %s
                """,
                (identifier, identifier.lower()),
            )
        except Error as error:
            app.logger.exception("Login database error: %s", error)
            flash("Could not reach the database. Please try again later.", "danger")
            return render_template("login.html", identifier=identifier, current_page="login")

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username/email or password.", "danger")
            return render_template("login.html", identifier=identifier, current_page="login")
        if not user.get("is_active", 1):
            flash(
                "This account is suspended. Please contact the site administrator if you believe this is a mistake.",
                "warning",
            )
            return render_template("login.html", identifier=identifier, current_page="login")

        session.clear()
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_hex(16)
        flash(f"Welcome back, {user['username']}.", "success")
        return redirect(url_for("home"))

    return render_template("login.html", identifier="", current_page="login")


@app.route("/logout", methods=["POST"])
def logout():
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("home"))
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    try:
        books = fetch_all(
            BOOKS_WITH_STATS + " ORDER BY b.created_at DESC"
        )
        pending = fetch_all(
            """
            SELECT r.id, r.rating, r.review_text, r.is_spoiler, r.created_at,
                   u.username, b.id AS book_id, b.title
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            JOIN books b ON b.id = r.book_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC
            """
        )
        stats = fetch_one(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS user_count,
                (SELECT COUNT(*) FROM books) AS book_count,
                (SELECT COUNT(*) FROM reviews) AS review_count,
                (
                    SELECT AVG(r.rating)
                    FROM reviews r
                    JOIN users u ON u.id = r.user_id
                    WHERE r.status = 'approved' AND u.is_active = 1
                ) AS avg_rating
            """
        )
        user_search = request.args.get("user_q", "").strip()
        user_sql = """
            SELECT id, username, role, is_active, created_at
            FROM users
        """
        user_params = []
        if user_search:
            user_sql += " WHERE username LIKE %s OR email LIKE %s"
            like = f"%{user_search}%"
            user_params.extend([like, like])
        user_sql += " ORDER BY created_at DESC"
        users = fetch_all(user_sql, tuple(user_params))
    except Error as error:
        app.logger.exception("Admin dashboard error: %s", error)
        flash("Could not load admin data.", "danger")
        books, pending, stats, users, user_search = [], [], {}, [], ""
    return render_template(
        "admin.html",
        books=[decorate_book(book) for book in books],
        pending_reviews=pending,
        users=users,
        user_search=user_search,
        stats=stats or {},
        avg_rating_display=format_rating((stats or {}).get("avg_rating")),
        current_page="admin",
    )


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    try:
        user = fetch_one(
            """
            SELECT id, username, role, is_active, created_at, display_name, bio,
                   favourite_genres, favourite_book, currently_reading,
                   annual_reading_goal, profile_filename
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        if not user:
            abort(404)

        reviews = fetch_all(
            """
            SELECT r.id, r.rating, r.review_text, r.is_spoiler, r.status,
                   r.created_at, r.updated_at, b.id AS book_id, b.title, b.author
            FROM reviews r
            JOIN books b ON b.id = r.book_id
            WHERE r.user_id = %s
            ORDER BY r.updated_at DESC
            """,
            (user_id,),
        )
        reading_list = fetch_all(
            """
            SELECT b.id AS book_id, b.title, b.author, rl.status, rl.created_at
            FROM reading_list rl
            JOIN books b ON b.id = rl.book_id
            WHERE rl.user_id = %s
            ORDER BY rl.created_at DESC
            """,
            (user_id,),
        )
    except Error as error:
        app.logger.exception("Admin user detail error: %s", error)
        flash("Could not load this user's details.", "danger")
        return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin_user.html",
        profile=user,
        reviews=reviews,
        reading_list=reading_list,
        reading_statuses=READING_STATUSES,
        current_page="admin",
    )


def set_regular_user_active(user_id, is_active):
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("admin_dashboard"))

    if session.get("user_id") == user_id:
        flash("You cannot change your own account status.", "danger")
        return redirect(url_for("admin_dashboard"))

    try:
        target = fetch_one(
            "SELECT id, role, is_active FROM users WHERE id = %s",
            (user_id,),
        )
        if not target:
            abort(404)
        if target["role"] != "user":
            flash("Only regular user accounts can be banned or unbanned.", "danger")
            return redirect(url_for("admin_dashboard"))
        if bool(target["is_active"]) == is_active:
            status = "active" if is_active else "banned"
            flash(f"This account is already {status}.", "warning")
            return redirect(url_for("admin_dashboard"))

        execute_query(
            """
            UPDATE users
            SET is_active = %s
            WHERE id = %s AND role = 'user' AND is_active = %s
            """,
            (int(is_active), user_id, int(not is_active)),
        )
        updated = fetch_one(
            "SELECT role, is_active FROM users WHERE id = %s",
            (user_id,),
        )
        if not updated or updated["role"] != "user" or bool(updated["is_active"]) != is_active:
            flash("The account status could not be changed.", "danger")
            return redirect(url_for("admin_dashboard"))
    except Error as error:
        app.logger.exception("Account status update error: %s", error)
        flash("Could not change the account status.", "danger")
        return redirect(url_for("admin_dashboard"))

    action = "banned" if not is_active else "unbanned"
    flash(f"User account {action}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/ban", methods=["POST"])
@admin_required
def admin_ban_user(user_id):
    return set_regular_user_active(user_id, False)


@app.route("/admin/users/<int:user_id>/unban", methods=["POST"])
@admin_required
def admin_unban_user(user_id):
    return set_regular_user_active(user_id, True)


def find_duplicate_book(data):
    isbn = normalized_isbn(data.get("isbn"))
    title = normalized_text(data.get("title"))
    author = normalized_text(data.get("author"))
    duplicate = fetch_one(
        """
        SELECT id, title, author, isbn
        FROM books
        WHERE (%s <> '' AND REGEXP_REPLACE(UPPER(COALESCE(isbn, '')), '[^0-9X]', '') = %s)
           OR (
                REGEXP_REPLACE(LOWER(title), '[^a-z0-9]', '') = %s
                AND REGEXP_REPLACE(LOWER(author), '[^a-z0-9]', '') = %s
           )
        LIMIT 1
        """,
        (isbn, isbn, title, author),
    )
    if not duplicate:
        return None
    if isbn and normalized_isbn(duplicate.get("isbn")) == isbn:
        return f"ISBN already exists on book #{duplicate['id']}."
    return f"Matching title and author already exist on book #{duplicate['id']}."


def _process_import_row(row, batch_token, zip_images=None):
    source_data = row.get("data") or {}
    image_name = (source_data.get("cover_filename") or "").strip()
    cover_url = (source_data.get("cover_url") or "").strip()
    data, validation_errors = validate_book_record(source_data)
    row["errors"].extend(validation_errors)
    if row["errors"]:
        return row
    data["cover_url"] = None
    row["data"] = data
    row["duplicate_reason"] = find_duplicate_book(data)
    if row["duplicate_reason"]:
        return row

    cover_bytes = None
    cover_extension = None
    if zip_images:
        if image_name:
            cover_bytes = zip_images.get(Path(image_name.replace("\\", "/")).name.casefold())
        else:
            title_key = normalized_text(data["title"])
            title_matches = [
                (name, image)
                for name, image in zip_images.items()
                if normalized_text(Path(name).stem) == title_key
            ]
            if len(title_matches) == 1:
                _name, cover_bytes = title_matches[0]
            elif len(title_matches) > 1:
                row["warnings"].append("Multiple ZIP images match the title; no cover was assigned.")
        if image_name and not cover_bytes:
            row["warnings"].append("The requested cover filename was not found in the ZIP.")
        if cover_bytes:
            cover_extension = image_extension(cover_bytes)

    if not cover_bytes and cover_url:
        cover_bytes, cover_extension, download_error = download_cover(cover_url)
        if download_error:
            row["warnings"].append(download_error)

    if cover_bytes and cover_extension:
        row["cover_stage"] = store_staged_cover(batch_token, cover_bytes, cover_extension)
        row["cover_ready"] = True
    else:
        row["cover_ready"] = False

    return row


def _render_import_page(**context):
    defaults = {
        "current_page": "admin",
        "search_query": "",
        "results": [],
        "search_total": 0,
        "search_start": 0,
        "search_token": None,
        "error_message": None,
        "summary": None,
    }
    defaults.update(context)
    return render_template("admin_import.html", **defaults)


def _render_import_preview(token, manifest):
    return render_template(
        "admin_import_preview.html",
        batch_token=token,
        records=manifest["records"],
        current_page="admin",
        import_kind=manifest["kind"],
        importable_count=sum(
            not row.get("errors") and not row.get("duplicate_reason")
            for row in manifest["records"]
        ),
    )


def _request_has_valid_csrf():
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return False
    return True


@app.route("/admin/books/import")
@admin_required
def admin_book_import():
    return _render_import_page()


@app.route("/admin/books/import/sample.csv")
@admin_required
def admin_book_import_sample():
    sample = (
        "Title,Author,ISBN,Description,Genre,Publisher,Publication Year,Cover Filename,Cover URL\n"
        '"The Left Hand of Darkness","Ursula K. Le Guin","9780441478125",'
        '"A science fiction novel.","Science Fiction","Ace Books",1969,"left-hand.jpg",\n'
    )
    return Response(
        sample,
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="openshelf-books-sample.csv"'},
    )


@app.route("/admin/books/import/csv/preview", methods=["POST"])
@admin_required
def admin_book_import_csv_preview():
    if not _request_has_valid_csrf():
        return redirect(url_for("admin_book_import"))
    csv_file = request.files.get("csv_file")
    if not csv_file or not csv_file.filename:
        return _render_import_page(error_message="Choose a CSV file to preview.")
    if Path(csv_file.filename).suffix.lower() != ".csv":
        return _render_import_page(error_message="The metadata file must have a .csv extension.")
    csv_bytes = csv_file.read(bulk_import.MAX_CSV_BYTES + 1)
    zip_file = request.files.get("covers_zip")
    zip_bytes = b""
    if zip_file and zip_file.filename:
        if Path(zip_file.filename).suffix.lower() != ".zip":
            return _render_import_page(error_message="The cover archive must have a .zip extension.")
        zip_bytes = zip_file.read(bulk_import.MAX_ZIP_BYTES + 1)
    token = None
    try:
        records = parse_csv(csv_bytes)
        zip_images = validate_zip_archive(zip_bytes) if zip_bytes else {}
        token = create_batch(session["user_id"], "csv", records)
        manifest = load_batch(token, session["user_id"])
        for row in manifest["records"]:
            _process_import_row(row, token, zip_images)
        _mark_import_batch_duplicates(manifest["records"])
        save_batch(token, manifest)
    except ImportValidationError as error:
        if token:
            remove_batch(token)
        return _render_import_page(error_message=str(error))
    except Error as error:
        if token:
            remove_batch(token)
        app.logger.exception("Book import preview database error: %s", error)
        return _render_import_page(error_message="Could not check for existing books.")
    except OSError as error:
        if token:
            remove_batch(token)
        app.logger.exception("Book import staging error: %s", error)
        return _render_import_page(error_message="Could not stage this import preview.")
    except ValueError as error:
        if token:
            remove_batch(token)
        app.logger.warning("Book import preview validation error: %s", error)
        return _render_import_page(error_message="A cover image could not be safely staged.")
    return _render_import_preview(token, manifest)


@app.route("/admin/books/import/google/search", methods=["POST"])
@admin_required
def admin_book_import_google_search():
    if not _request_has_valid_csrf():
        return redirect(url_for("admin_book_import"))
    query = request.form.get("query", "").strip()
    try:
        try:
            start_index = int(request.form.get("start_index", "0"))
        except ValueError as error:
            raise GoogleBooksError("Choose a valid Google Books results page.") from error
        result = search_google_books(
            query,
            start_index=start_index,
            api_key=os.getenv("GOOGLE_BOOKS_API_KEY"),
        )
    except GoogleBooksError as error:
        return _render_import_page(search_query=query, error_message=str(error))
    except (OSError, ValueError) as error:
        app.logger.exception("Google Books search connection error: %s", error)
        return _render_import_page(
            search_query=query,
            error_message="Google Books search failed. Try again later.",
        )

    results = result["results"]
    search_token = None
    if results:
        search_token = create_batch(
            session["user_id"],
            "google_search",
            [{"data": row} for row in results],
        )
    if result["total_items"] and not results:
        error_message = "Google Books returned no usable results for this page."
    else:
        error_message = None
    return _render_import_page(
        search_query=query,
        results=results,
        search_total=result["total_items"],
        search_start=result["start_index"],
        search_token=search_token,
        error_message=error_message,
    )


@app.route("/admin/books/import/google/preview", methods=["POST"])
@admin_required
def admin_book_import_google_preview():
    if not _request_has_valid_csrf():
        return redirect(url_for("admin_book_import"))
    search_token = request.form.get("search_token", "")
    search_batch = load_batch(search_token, session["user_id"])
    if not search_batch or search_batch.get("kind") != "google_search":
        return _render_import_page(error_message="Google Books results expired; search again.")
    selected_ids = set(request.form.getlist("selected_volume_ids"))
    selected_records = [
        row for row in search_batch["records"]
        if row.get("data", {}).get("google_volume_id") in selected_ids
    ]
    if not selected_records:
        return _render_import_page(error_message="Select at least one book to preview.")
    if len(selected_records) > bulk_import.MAX_BATCH_IMPORT:
        return _render_import_page(error_message=f"Select no more than {bulk_import.MAX_BATCH_IMPORT} books at once.")

    records = []
    token = None
    try:
        token = create_batch(session["user_id"], "google", [])
        manifest = load_batch(token, session["user_id"])
        for row_number, selected in enumerate(selected_records, start=1):
            row = {
                "row_number": row_number,
                "data": selected["data"],
                "errors": [],
                "warnings": [],
                "cover_stage": None,
            }
            _process_import_row(row, token)
            records.append(row)
        _mark_import_batch_duplicates(records)
        manifest["records"] = records
        save_batch(token, manifest)
    except Error as error:
        if token:
            remove_batch(token)
        app.logger.exception("Google Books import preview database error: %s", error)
        return _render_import_page(error_message="Could not check for existing books.")
    except OSError as error:
        if token:
            remove_batch(token)
        app.logger.exception("Google Books import staging error: %s", error)
        return _render_import_page(error_message="Could not stage selected Google Books records.")
    except ValueError as error:
        if token:
            remove_batch(token)
        app.logger.warning("Google Books import preview validation error: %s", error)
        return _render_import_page(error_message="A cover image could not be safely staged.")
    finally:
        remove_batch(search_token)
    return _render_import_preview(token, manifest)


@app.route("/admin/books/import/<token>/cover/<filename>")
@admin_required
def admin_book_import_cover(token, filename):
    manifest = load_batch(token, session["user_id"])
    if not manifest:
        abort(404)
    cover_path = staged_cover_path(token, filename)
    if not cover_path:
        abort(404)
    return send_file(cover_path, mimetype={
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }[cover_path.suffix.lower()])


def _same_imported_book(first, second):
    first_isbn = normalized_isbn(first.get("isbn"))
    second_isbn = normalized_isbn(second.get("isbn"))
    if first_isbn and second_isbn and first_isbn == second_isbn:
        return True
    return (
        normalized_text(first.get("title")) == normalized_text(second.get("title"))
        and normalized_text(first.get("author")) == normalized_text(second.get("author"))
    )


def _mark_import_batch_duplicates(records):
    seen = []
    for row in records:
        if row.get("errors") or row.get("duplicate_reason"):
            continue
        matching_row = next(
            (
                previous
                for previous in seen
                if _same_imported_book(row["data"], previous["data"])
            ),
            None,
        )
        if matching_row:
            row["duplicate_reason"] = (
                f"Duplicates another row in this import (row {matching_row['row_number']})."
            )
        else:
            seen.append(row)


def _insert_import_record(record, token):
    data = record["data"]
    cover_stage = record.get("cover_stage")
    staged_path = staged_cover_path(token, cover_stage) if cover_stage else None
    if cover_stage and not staged_path:
        raise OSError("Staged cover image is no longer available.")

    cover_filename = None
    final_cover_path = None
    cover_created = False
    connection = None
    cursor = None
    try:
        if staged_path:
            extension = image_extension(staged_path.read_bytes())
            if not extension:
                raise OSError("Staged cover image is invalid.")
            UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
            cover_filename = f"{uuid.uuid4().hex}{extension}"
            final_cover_path = UPLOAD_FOLDER / cover_filename
            with final_cover_path.open("xb") as destination:
                cover_created = True
                with staged_path.open("rb") as source:
                    shutil.copyfileobj(source, destination)
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO books
                (title, author, genre, cover_url, cover_filename, description,
                 isbn, publisher, publication_year)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                data["title"],
                data["author"],
                data["genre"],
                None,
                cover_filename,
                data.get("description"),
                data.get("isbn"),
                data.get("publisher"),
                data.get("publication_year"),
            ),
        )
        connection.commit()
        return cursor.lastrowid
    except Error:
        if connection:
            try:
                connection.rollback()
            except Error as rollback_error:
                app.logger.exception("Could not roll back a failed book import: %s", rollback_error)
        if cover_created and final_cover_path and final_cover_path.exists():
            try:
                final_cover_path.unlink()
            except OSError as cleanup_error:
                app.logger.exception("Could not remove a failed book-import cover: %s", cleanup_error)
        raise
    except OSError:
        if connection:
            try:
                connection.rollback()
            except Error as rollback_error:
                app.logger.exception("Could not roll back a failed book import: %s", rollback_error)
        if cover_created and final_cover_path and final_cover_path.exists():
            try:
                final_cover_path.unlink()
            except OSError as cleanup_error:
                app.logger.exception("Could not remove a failed book-import cover: %s", cleanup_error)
        raise
    finally:
        if cursor:
            try:
                cursor.close()
            except Error as error:
                app.logger.warning("Could not close the book-import cursor: %s", error)
        if connection:
            try:
                connection.close()
            except Error as error:
                app.logger.warning("Could not close the book-import connection: %s", error)


@app.route("/admin/books/import/<token>/confirm", methods=["POST"])
@admin_required
def admin_book_import_confirm(token):
    if not _request_has_valid_csrf():
        return redirect(url_for("admin_book_import"))
    manifest = load_batch(token, session["user_id"])
    if not manifest or manifest.get("kind") not in {"csv", "google"}:
        flash("Import preview expired; please prepare it again.", "warning")
        return redirect(url_for("admin_book_import"))

    imported = skipped = failed = 0
    imported_records = []
    skipped_rows = []
    failed_rows = []
    for row in manifest["records"]:
        if row.get("errors"):
            failed += 1
            failed_rows.append({
                "row_number": row.get("row_number"),
                "title": row.get("data", {}).get("title") or "Untitled row",
                "reason": "; ".join(row["errors"]),
            })
            continue
        if row.get("duplicate_reason"):
            skipped += 1
            skipped_rows.append({
                "row_number": row.get("row_number"),
                "title": row.get("data", {}).get("title") or "Untitled row",
                "reason": row["duplicate_reason"],
            })
            continue
        if any(_same_imported_book(row["data"], existing) for existing in imported_records):
            skipped += 1
            skipped_rows.append({
                "row_number": row.get("row_number"),
                "title": row.get("data", {}).get("title") or "Untitled row",
                "reason": "Duplicates another row that was imported in this batch.",
            })
            continue
        try:
            duplicate_reason = find_duplicate_book(row["data"])
            if duplicate_reason:
                skipped += 1
                skipped_rows.append({
                    "row_number": row.get("row_number"),
                    "title": row.get("data", {}).get("title") or "Untitled row",
                    "reason": duplicate_reason,
                })
                continue
            _insert_import_record(row, token)
            imported_records.append(row["data"])
            imported += 1
        except Error as error:
            app.logger.exception("Book import insert failed for row %s: %s", row.get("row_number"), error)
            row["failure"] = "Database insert failed; the row was rolled back."
            failed_rows.append({
                "row_number": row.get("row_number"),
                "title": row.get("data", {}).get("title") or "Untitled row",
                "reason": row["failure"],
            })
            failed += 1
        except OSError as error:
            app.logger.warning("Book import file operation failed for row %s: %s", row.get("row_number"), error)
            row["failure"] = "Cover file could not be saved; no book was imported."
            failed_rows.append({
                "row_number": row.get("row_number"),
                "title": row.get("data", {}).get("title") or "Untitled row",
                "reason": row["failure"],
            })
            failed += 1
    remove_batch(token)
    return _render_import_page(
        summary={
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "skipped_rows": skipped_rows,
            "failed_rows": failed_rows,
        }
    )


@app.route("/admin/books/new", methods=["GET", "POST"])
@admin_required
def admin_new_book():
    form = empty_book_form()
    if request.method == "POST":
        if not csrf_ok():
            flash("The form expired. Please try again.", "warning")
            return redirect(url_for("admin_new_book"))
        form = {key: request.form.get(key, "") for key in empty_book_form()}
        data, error = validate_book_form(form)
        if error:
            flash(error, "danger")
            return render_template(
                "admin_book_form.html", form=form, mode="new", current_page="admin"
            )
        filename, file_error = save_cover_file(request.files.get("cover_file"))
        if file_error:
            flash(file_error, "danger")
            return render_template(
                "admin_book_form.html", form=form, mode="new", current_page="admin"
            )
        try:
            execute_query(
                """
                INSERT INTO books
                    (title, author, genre, cover_url, cover_filename, description,
                     isbn, publisher, publication_year)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    data["title"], data["author"], data["genre"], data["cover_url"],
                    filename, data["description"], data["isbn"], data["publisher"],
                    data["publication_year"],
                ),
            )
            flash("Book added to the catalogue.", "success")
            return redirect(url_for("admin_dashboard"))
        except Error as error:
            delete_cover_file(filename)
            app.logger.exception("Add book error: %s", error)
            flash("Could not save the book.", "danger")
    return render_template(
        "admin_book_form.html", form=form, mode="new", current_page="admin"
    )


@app.route("/admin/books/<int:book_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_book(book_id):
    book = fetch_one("SELECT * FROM books WHERE id = %s", (book_id,))
    if not book:
        abort(404)
    form = book_to_form(book)
    if request.method == "POST":
        if not csrf_ok():
            flash("The form expired. Please try again.", "warning")
            return redirect(url_for("admin_edit_book", book_id=book_id))
        form = {key: request.form.get(key, "") for key in empty_book_form()}
        data, error = validate_book_form(form)
        if error:
            flash(error, "danger")
            return render_template(
                "admin_book_form.html",
                form=form,
                mode="edit",
                book=book,
                current_page="admin",
            )
        filename, file_error = save_cover_file(request.files.get("cover_file"))
        if file_error:
            flash(file_error, "danger")
            return render_template(
                "admin_book_form.html",
                form=form,
                mode="edit",
                book=book,
                current_page="admin",
            )
        cover_filename = filename or book.get("cover_filename")
        try:
            execute_query(
                """
                UPDATE books
                SET title = %s, author = %s, genre = %s, cover_url = %s,
                    cover_filename = %s, description = %s, isbn = %s,
                    publisher = %s, publication_year = %s
                WHERE id = %s
                """,
                (
                    data["title"], data["author"], data["genre"], data["cover_url"],
                    cover_filename, data["description"], data["isbn"],
                    data["publisher"], data["publication_year"], book_id,
                ),
            )
            if filename and book.get("cover_filename"):
                delete_cover_file(book["cover_filename"])
            flash("Book details were updated.", "success")
            return redirect(url_for("admin_dashboard"))
        except Error as error:
            delete_cover_file(filename)
            app.logger.exception("Edit book error: %s", error)
            flash("Could not update the book.", "danger")
    return render_template(
        "admin_book_form.html",
        form=form,
        mode="edit",
        book=book,
        current_page="admin",
    )


@app.route("/admin/books/<int:book_id>/delete", methods=["POST"])
@admin_required
def admin_delete_book(book_id):
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("admin_dashboard"))
    book = fetch_one("SELECT id, cover_filename FROM books WHERE id = %s", (book_id,))
    if not book:
        abort(404)
    try:
        execute_query("DELETE FROM books WHERE id = %s", (book_id,))
        delete_cover_file(book.get("cover_filename"))
        flash("Book deleted.", "success")
    except Error as error:
        app.logger.exception("Delete book error: %s", error)
        flash("Could not delete the book.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reviews/<int:review_id>/moderate", methods=["POST"])
@admin_required
def admin_moderate_review(review_id):
    if not csrf_ok():
        flash("The form expired. Please try again.", "warning")
        return redirect(url_for("admin_dashboard"))
    review = fetch_one("SELECT id FROM reviews WHERE id = %s", (review_id,))
    if not review:
        abort(404)
    action = request.form.get("action")
    if action not in {"approved", "rejected"}:
        flash("Choose approve or reject.", "danger")
        return redirect(url_for("admin_dashboard"))
    try:
        execute_query(
            "UPDATE reviews SET status = %s WHERE id = %s",
            (action, review_id),
        )
        flash(f"Review {action}.", "success")
    except Error as error:
        app.logger.exception("Moderation error: %s", error)
        flash("Could not update the review.", "danger")
    return redirect(url_for("admin_dashboard"))


def validate_registration(username, email, password, confirm):
    if not USERNAME_PATTERN.fullmatch(username):
        return "Username must be 3-50 letters, numbers or underscores."
    if not EMAIL_PATTERN.fullmatch(email) or len(email) > 120:
        return "Please enter a valid email address."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password is too long."
    if password != confirm:
        return "Password confirmation does not match."
    return None


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
