"""Validation, staging, and safe external image helpers for admin book imports."""

import csv
import ipaddress
import io
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import uuid
import zipfile
from http.client import HTTPSConnection, HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_CSV_BYTES + MAX_ZIP_BYTES + 1024 * 1024
MAX_IMPORT_ROWS = 50
MAX_BATCH_IMPORT = 50
MAX_ZIP_FILES = 500
MAX_ZIP_FILE_BYTES = 2 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024
MAX_COVER_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_GOOGLE_RESULTS = 10
MAX_GOOGLE_START_INDEX = 1000
IMPORT_BATCH_TTL_SECONDS = 30 * 60
IMPORT_ROOT = Path(tempfile.gettempdir()) / "openshelf-book-imports"
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
BATCH_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")

CSV_HEADER_ALIASES = {
    "title": "title",
    "book title": "title",
    "author": "author",
    "authors": "author",
    "isbn": "isbn",
    "isbn 10": "isbn",
    "isbn 13": "isbn",
    "description": "description",
    "genre": "genre",
    "category": "genre",
    "categories": "genre",
    "publisher": "publisher",
    "publication year": "publication_year",
    "year": "publication_year",
    "cover filename": "cover_filename",
    "cover image filename": "cover_filename",
    "cover url": "cover_url",
    "cover image url": "cover_url",
}


class ImportValidationError(ValueError):
    """Raised when an uploaded import file is invalid as a whole."""


class GoogleBooksError(ValueError):
    """Raised when the Google Books API cannot provide a usable response."""


def image_extension(data):
    """Return a verified image extension from its file signature."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _header_key(value):
    return " ".join(re.sub(r"[_-]+", " ", (value or "").strip().lower()).split())


def parse_csv(csv_bytes):
    if len(csv_bytes) > MAX_CSV_BYTES:
        raise ImportValidationError("CSV file exceeds the 2 MB upload limit.")
    try:
        text = csv_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ImportValidationError("CSV must use UTF-8 encoding.") from error
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if not reader.fieldnames:
            raise ImportValidationError("CSV is empty or has no header row.")
        mapped_headers = []
        seen_headers = set()
        for header in reader.fieldnames:
            key = CSV_HEADER_ALIASES.get(_header_key(header))
            if key is None:
                continue
            if key in seen_headers:
                raise ImportValidationError(f"CSV has duplicate columns for {key.replace('_', ' ')}.")
            mapped_headers.append((header, key))
            seen_headers.add(key)
        if not {"title", "author", "genre"}.issubset(seen_headers):
            raise ImportValidationError("CSV must include Title, Author, and Genre columns.")

        rows = []
        for row_number, raw_row in enumerate(reader, start=2):
            if row_number > MAX_IMPORT_ROWS + 1:
                raise ImportValidationError(f"CSV cannot contain more than {MAX_IMPORT_ROWS} book rows.")
            if None in raw_row:
                rows.append({
                    "row_number": row_number,
                    "data": {},
                    "errors": ["Row has more values than the header."],
                    "warnings": [],
                    "cover_stage": None,
                })
                continue
            data = {
                mapped: (raw_row.get(header) or "").strip()
                for header, mapped in mapped_headers
            }
            if not any(data.values()):
                continue
            rows.append({
                "row_number": row_number,
                "data": data,
                "errors": [],
                "warnings": [],
                "cover_stage": None,
            })
        if not rows:
            raise ImportValidationError("CSV contains no book rows.")
        return rows
    except csv.Error as error:
        raise ImportValidationError(f"Malformed CSV: {error}") from error


def validate_zip_archive(zip_bytes):
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise ImportValidationError("ZIP file exceeds the 50 MB upload limit.")
    if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        raise ImportValidationError("The uploaded cover archive is not a valid ZIP file.")
    images = {}
    seen_basenames = set()
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_FILES:
                raise ImportValidationError(f"ZIP cannot contain more than {MAX_ZIP_FILES} files.")
            for entry in entries:
                name = entry.filename
                if "\\" in name or name.startswith("/"):
                    raise ImportValidationError("ZIP contains an unsafe archive path.")
                raw_name = name[:-1] if entry.is_dir() and name.endswith("/") else name
                raw_parts = raw_name.split("/")
                if not raw_name or any(part in {"", ".", ".."} for part in raw_parts):
                    raise ImportValidationError("ZIP contains an unsafe archive path.")
                path = PurePosixPath(name)
                if path.is_absolute():
                    raise ImportValidationError("ZIP contains an unsafe archive path.")
                if re.match(r"^[A-Za-z]:", name) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ImportValidationError("ZIP contains an unsafe archive entry.")
                if entry.is_dir():
                    continue
                total_uncompressed += entry.file_size
                if entry.file_size > MAX_ZIP_FILE_BYTES:
                    raise ImportValidationError("A ZIP image exceeds the 2 MB per-file limit.")
                if total_uncompressed > MAX_ZIP_TOTAL_BYTES:
                    raise ImportValidationError("ZIP uncompressed content exceeds 100 MB.")
                extension = Path(path.name).suffix.lower()
                if extension not in ALLOWED_IMAGE_EXTENSIONS:
                    continue
                basename = path.name.casefold()
                if basename in seen_basenames:
                    raise ImportValidationError("ZIP has multiple files with the same image filename.")
                seen_basenames.add(basename)
                with archive.open(entry) as image_stream:
                    data = image_stream.read(MAX_ZIP_FILE_BYTES + 1)
                    if len(data) <= MAX_ZIP_FILE_BYTES and image_stream.read(1):
                        raise ImportValidationError("A ZIP image exceeds the 2 MB per-file limit.")
                if len(data) > MAX_ZIP_FILE_BYTES or len(data) != entry.file_size:
                    raise ImportValidationError("ZIP contains an image with an invalid declared size.")
                verified_extension = image_extension(data)
                matching_jpeg_extensions = {verified_extension, extension} <= {".jpg", ".jpeg"}
                if not verified_extension or (
                    verified_extension != extension and not matching_jpeg_extensions
                ):
                    continue
                images[basename] = data
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ImportValidationError("Could not safely read the cover archive.") from error
    return images


def normalized_isbn(value):
    return re.sub(r"[^0-9X]", "", (value or "").upper())


def normalized_text(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def validate_book_record(data):
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    genre = (data.get("genre") or "").strip()
    isbn = (data.get("isbn") or "").strip()
    publisher = (data.get("publisher") or "").strip()
    description = (data.get("description") or "").strip()
    year_raw = (data.get("publication_year") or "").strip()

    errors = []
    if not title or len(title) > 200:
        errors.append("Title is required and must be 200 characters or fewer.")
    elif not normalized_text(title):
        errors.append("Title must contain at least one letter or number.")
    if not author or len(author) > 200:
        errors.append("Author is required and must be 200 characters or fewer.")
    elif not normalized_text(author):
        errors.append("Author must contain at least one letter or number.")
    if not genre or len(genre) > 80:
        errors.append("Genre is required and must be 80 characters or fewer.")
    if len(isbn) > 20:
        errors.append("ISBN must be 20 characters or fewer.")
    if len(publisher) > 200:
        errors.append("Publisher must be 200 characters or fewer.")
    if len(description.encode("utf-8")) > 65535:
        errors.append("Description exceeds the database TEXT column limit.")
    year = None
    if year_raw:
        if not year_raw.isdigit() or not (1000 <= int(year_raw) <= time.gmtime().tm_year + 1):
            errors.append("Publication year must be a valid four-digit year.")
        else:
            year = int(year_raw)
    if errors:
        return None, errors
    return {
        "title": title,
        "author": author,
        "genre": genre,
        "isbn": isbn or None,
        "description": description or None,
        "publisher": publisher or None,
        "publication_year": year,
    }, []


def validate_https_url(url):
    parsed = urlsplit((url or "").strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Cover URL must be a public HTTPS URL.")
    hostname = parsed.hostname.rstrip(".")
    if hostname.casefold() == "localhost" or hostname.casefold().endswith(".localhost"):
        raise ValueError("Cover URL points to a local host.")
    addresses = _resolve_public_addresses(hostname)
    if not addresses:
        raise ValueError("Cover URL host could not be safely resolved.")
    return parsed.geturl()


def _resolve_public_addresses(hostname):
    try:
        address_strings = {
            result[4][0].split("%", 1)[0]
            for result in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
        addresses = {ipaddress.ip_address(address) for address in address_strings}
    except (OSError, ValueError) as error:
        raise ValueError("Cover URL host could not be safely resolved.") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Cover URL must resolve only to public internet addresses.")
    return address_strings


class _PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        parsed = urlsplit(request.full_url)
        validate_https_url(request.full_url)
        addresses = sorted(_resolve_public_addresses(parsed.hostname))
        if not addresses:
            raise ValueError("Cover URL host could not be safely resolved.")

        class PinnedHTTPSConnection(HTTPSConnection):
            def connect(self):
                last_error = None
                for address in addresses:
                    try:
                        sock = socket.create_connection(
                            (address, self.port),
                            self.timeout,
                            self.source_address,
                        )
                        self.sock = self._context.wrap_socket(
                            sock,
                            server_hostname=self.host,
                        )
                        return
                    except OSError as error:
                        last_error = error
                raise last_error or OSError("Could not connect to the validated cover host.")

        def connection_factory(host, **kwargs):
            return PinnedHTTPSConnection(
                host,
                timeout=kwargs.get("timeout"),
                context=kwargs.get("context") or ssl.create_default_context(),
            )

        return self.do_open(connection_factory, request)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects=3):
        super().__init__()
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.redirects += 1
        if self.redirects > self.max_redirects:
            raise ValueError("Cover URL exceeded the redirect limit.")
        redirect_url = urljoin(request.full_url, new_url)
        validate_https_url(redirect_url)
        return super().redirect_request(request, response, code, message, headers, redirect_url)


def download_cover(url, opener_factory=None):
    """Download a verified public HTTPS image, returning (bytes, extension, error)."""
    try:
        safe_url = validate_https_url(url)
        opener = (opener_factory or build_opener)(
            ProxyHandler({}), _SafeRedirectHandler(), _PinnedHTTPSHandler()
        )
        response = opener.open(Request(safe_url, headers={"User-Agent": "OpenShelf/1.0"}), timeout=5)
        with response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_COVER_DOWNLOAD_BYTES:
                return None, None, "Cover image exceeds the 2 MB download limit."
            data = response.read(MAX_COVER_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_COVER_DOWNLOAD_BYTES:
            return None, None, "Cover image exceeds the 2 MB download limit."
        extension = image_extension(data)
        if not extension:
            return None, None, "Downloaded cover is not a supported image."
        return data, extension, None
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as error:
        if isinstance(error, HTTPError) and error.code == 429:
            message = "Cover image host rate-limited the download."
        else:
            message = "Cover image could not be safely downloaded."
        return None, None, message


def _google_volume_to_record(volume):
    info = volume.get("volumeInfo") or {}
    if not isinstance(info, dict):
        info = {}
    identifiers = info.get("industryIdentifiers") or []
    if not isinstance(identifiers, list):
        identifiers = []
    isbn = next(
        (identifier.get("identifier") for identifier in identifiers
         if isinstance(identifier, dict) and identifier.get("type") == "ISBN_13"),
        None,
    ) or next(
        (identifier.get("identifier") for identifier in identifiers
         if isinstance(identifier, dict) and identifier.get("type") == "ISBN_10"),
        None,
    )
    if not isinstance(isbn, str):
        isbn = ""
    categories = info.get("categories") or []
    if not isinstance(categories, list):
        categories = []
    authors = info.get("authors") or []
    if not isinstance(authors, list):
        authors = []
    published_value = info.get("publishedDate") or ""
    published = published_value[:4] if isinstance(published_value, str) else ""
    year = published if published.isdigit() else ""
    images = info.get("imageLinks") or {}
    if not isinstance(images, dict):
        images = {}
    title = info.get("title")
    description = info.get("description")
    publisher = info.get("publisher")
    volume_id = volume.get("id")
    cover_url = images.get("thumbnail") or images.get("smallThumbnail") or ""
    if not isinstance(cover_url, str):
        cover_url = ""
    if cover_url.startswith("http://"):
        cover_url = "https://" + cover_url[len("http://"):]
    return {
        "google_volume_id": volume_id if isinstance(volume_id, str) else "",
        "title": title.strip() if isinstance(title, str) else "",
        "author": ", ".join(author for author in authors if isinstance(author, str)).strip(),
        "isbn": isbn or "",
        "description": description.strip() if isinstance(description, str) else "",
        "genre": ", ".join(category for category in categories if isinstance(category, str)).strip()
        or "Uncategorized",
        "publisher": publisher.strip() if isinstance(publisher, str) else "",
        "publication_year": year,
        "cover_url": cover_url,
    }


def search_google_books(query, start_index=0, api_key=None, opener_factory=None):
    query = (query or "").strip()
    if not query or len(query) > 200:
        raise GoogleBooksError("Enter a search of 1 to 200 characters.")
    if not isinstance(start_index, int) or start_index < 0 or start_index > MAX_GOOGLE_START_INDEX:
        raise GoogleBooksError("Requested Google Books page is outside the allowed range.")
    parameters = {
        "q": query,
        "startIndex": start_index,
        "maxResults": MAX_GOOGLE_RESULTS,
        "printType": "books",
    }
    if api_key:
        parameters["key"] = api_key
    url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(parameters)
    opener = (opener_factory or build_opener)(ProxyHandler({}), _PinnedHTTPSHandler())
    try:
        response = opener.open(Request(url, headers={"User-Agent": "OpenShelf/1.0"}), timeout=5)
        with response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise GoogleBooksError("Google Books returned an oversized response.")
        decoded = json.loads(payload.decode("utf-8"))
    except HTTPError as error:
        if error.code == 429:
            raise GoogleBooksError("Google Books search quota was reached. Try again later.") from error
        raise GoogleBooksError(f"Google Books returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError, HTTPException, ValueError) as error:
        raise GoogleBooksError("Google Books could not be reached. Try again later.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoogleBooksError("Google Books returned an invalid response.") from error
    if not isinstance(decoded, dict):
        raise GoogleBooksError("Google Books returned an invalid response.")
    items = decoded.get("items") or []
    if not isinstance(items, list):
        raise GoogleBooksError("Google Books returned an invalid results list.")
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result = _google_volume_to_record(item)
        if result["google_volume_id"]:
            results.append(result)
    try:
        total_items = int(decoded.get("totalItems") or 0)
    except (ValueError, TypeError) as error:
        raise GoogleBooksError("Google Books returned an invalid result count.") from error
    return {
        "total_items": max(total_items, 0),
        "results": results,
        "start_index": start_index,
    }


def create_batch(owner_id, kind, records):
    IMPORT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    cleanup_expired_batches()
    token = uuid.uuid4().hex
    batch_dir = IMPORT_ROOT / token
    batch_dir.mkdir(mode=0o700)
    manifest = {
        "owner_id": int(owner_id),
        "kind": kind,
        "created_at": time.time(),
        "records": records,
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return token


def store_staged_cover(token, data, extension):
    if not BATCH_TOKEN_PATTERN.fullmatch(token) or extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Invalid import staging target.")
    if len(data) > MAX_COVER_DOWNLOAD_BYTES or image_extension(data) != extension:
        raise ValueError("Invalid staged cover image.")
    batch_dir = IMPORT_ROOT / token
    if not batch_dir.is_dir():
        raise ValueError("Import preview has expired.")
    filename = f"{uuid.uuid4().hex}{extension}"
    (batch_dir / filename).write_bytes(data)
    return filename


def load_batch(token, owner_id):
    if not BATCH_TOKEN_PATTERN.fullmatch(token):
        return None
    batch_dir = IMPORT_ROOT / token
    manifest_path = batch_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - manifest.get("created_at", 0) > IMPORT_BATCH_TTL_SECONDS:
        remove_batch(token)
        return None
    if manifest.get("owner_id") != int(owner_id):
        return None
    return manifest


def save_batch(token, manifest):
    if not BATCH_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid import staging target.")
    batch_dir = IMPORT_ROOT / token
    temporary_path = batch_dir / "manifest.tmp"
    temporary_path.write_text(json.dumps(manifest), encoding="utf-8")
    os.replace(temporary_path, batch_dir / "manifest.json")


def staged_cover_path(token, filename):
    if (
        not BATCH_TOKEN_PATTERN.fullmatch(token)
        or not re.fullmatch(r"[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|gif)", filename or "")
    ):
        return None
    path = IMPORT_ROOT / token / filename
    return path if path.is_file() else None


def remove_batch(token):
    if BATCH_TOKEN_PATTERN.fullmatch(token):
        shutil.rmtree(IMPORT_ROOT / token, ignore_errors=True)


def cleanup_expired_batches():
    if not IMPORT_ROOT.exists():
        return
    cutoff = time.time() - IMPORT_BATCH_TTL_SECONDS
    for batch_dir in IMPORT_ROOT.iterdir():
        if not batch_dir.is_dir() or not BATCH_TOKEN_PATTERN.fullmatch(batch_dir.name):
            continue
        try:
            manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("created_at", 0) < cutoff:
                remove_batch(batch_dir.name)
        except (OSError, json.JSONDecodeError):
            if time.time() - batch_dir.stat().st_mtime > IMPORT_BATCH_TTL_SECONDS:
                remove_batch(batch_dir.name)

