import io
import os
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

previous_app_env = os.environ.get("APP_ENV")
previous_secret = os.environ.get("FLASK_SECRET_KEY")
os.environ["APP_ENV"] = "development"
os.environ["FLASK_SECRET_KEY"] = "test-only-openshelf-bulk-import-secret"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import app as openshelf
import bulk_import
from mysql.connector import Error

if previous_app_env is None:
    os.environ.pop("APP_ENV", None)
else:
    os.environ["APP_ENV"] = previous_app_env
if previous_secret is None:
    os.environ.pop("FLASK_SECRET_KEY", None)
else:
    os.environ["FLASK_SECRET_KEY"] = previous_secret


ADMIN = {"id": 1, "username": "shelfadmin", "role": "admin", "is_active": 1}
REGULAR_USER = {"id": 2, "username": "reader", "role": "user", "is_active": 1}
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
SAMPLE_CSV = (
    b"Title,Author,Genre,ISBN,Cover Filename\n"
    b"Sample Book,Sample Author,Mystery,9781234567890,sample.png\n"
)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


class BulkImportHelperTests(unittest.TestCase):
    def test_csv_parses_supported_headers_and_rejects_bad_encoding(self):
        rows = bulk_import.parse_csv(SAMPLE_CSV)
        self.assertEqual(rows[0]["data"]["title"], "Sample Book")
        self.assertEqual(rows[0]["data"]["cover_filename"], "sample.png")
        with self.assertRaises(bulk_import.ImportValidationError):
            bulk_import.parse_csv(b"\xff\xfe")

    def test_csv_requires_schema_required_columns_and_row_values(self):
        with self.assertRaisesRegex(bulk_import.ImportValidationError, "Title, Author, and Genre"):
            bulk_import.parse_csv(b"Title,Author\nA,B\n")
        with self.assertRaisesRegex(bulk_import.ImportValidationError, "Malformed CSV"):
            bulk_import.parse_csv(b'Title,Author,Genre\n"A,B,Mystery\n')
        data, errors = bulk_import.validate_book_record(
            {"title": "", "author": "A", "genre": ""}
        )
        self.assertIsNone(data)
        self.assertTrue(any("Title is required" in error for error in errors))
        self.assertTrue(any("Genre is required" in error for error in errors))

    def test_csv_row_limit_is_enforced(self):
        csv_bytes = b"Title,Author,Genre\n" + b"A,B,Mystery\n" * (
            bulk_import.MAX_IMPORT_ROWS + 1
        )
        with self.assertRaisesRegex(bulk_import.ImportValidationError, "cannot contain more"):
            bulk_import.parse_csv(csv_bytes)

    def test_zip_path_traversal_and_oversized_archive_are_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../sample.png", PNG)
        with self.assertRaisesRegex(bulk_import.ImportValidationError, "unsafe"):
            bulk_import.validate_zip_archive(output.getvalue())

        with patch.object(bulk_import, "MAX_ZIP_BYTES", 4):
            with self.assertRaisesRegex(bulk_import.ImportValidationError, "50 MB"):
                bulk_import.validate_zip_archive(b"12345")

    def test_zip_decompressed_file_limit_is_enforced(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("cover.png", PNG)
        with patch.object(bulk_import, "MAX_ZIP_FILE_BYTES", 8):
            with self.assertRaisesRegex(bulk_import.ImportValidationError, "per-file limit"):
                bulk_import.validate_zip_archive(output.getvalue())

    def test_zip_accepts_jpeg_extension_variants_only_when_signature_matches(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("covers/book.jpeg", b"\xff\xd8\xff" + b"jpeg")
        images = bulk_import.validate_zip_archive(output.getvalue())
        self.assertIn("book.jpeg", images)

    def test_google_books_success_empty_and_malformed_responses(self):
        payload = {
            "totalItems": 1,
            "items": [{
                "id": "volume-1",
                "volumeInfo": {
                    "title": "Sample Book",
                    "authors": ["A Writer"],
                    "categories": ["Mystery"],
                    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781234567890"}],
                    "imageLinks": {"thumbnail": "https://books.example/cover"},
                },
            }],
        }

        def factory_for(body):
            opener = Mock()
            opener.open.return_value = FakeResponse(body)
            return lambda *_handlers: opener

        success = bulk_import.search_google_books(
            "Sample", opener_factory=factory_for(bulk_import.json.dumps(payload).encode())
        )
        self.assertEqual(success["results"][0]["title"], "Sample Book")
        self.assertEqual(success["results"][0]["isbn"], "9781234567890")
        empty = bulk_import.search_google_books(
            "none", opener_factory=factory_for(b'{"totalItems":0,"items":[]}')
        )
        self.assertEqual(empty["results"], [])
        with self.assertRaises(bulk_import.GoogleBooksError):
            bulk_import.search_google_books(
                "bad", opener_factory=factory_for(b"not-json")
            )

    def test_google_books_timeout_http_error_and_malformed_items(self):
        def opener_raising(error):
            opener = Mock()
            opener.open.side_effect = error
            return lambda *_handlers: opener

        with self.assertRaises(bulk_import.GoogleBooksError):
            bulk_import.search_google_books(
                "book", opener_factory=opener_raising(TimeoutError())
            )
        http_error = HTTPError("https://example.test", 429, "quota", {}, io.BytesIO())
        with self.assertRaisesRegex(bulk_import.GoogleBooksError, "quota"):
            bulk_import.search_google_books(
                "book", opener_factory=opener_raising(http_error)
            )
        malformed = Mock()
        malformed.open.return_value = FakeResponse(b'{"totalItems":"bad","items":[]}')
        with self.assertRaises(bulk_import.GoogleBooksError):
            bulk_import.search_google_books("book", opener_factory=lambda *_: malformed)

        payload = b'{"totalItems":1,"items":[{"id":"one","volumeInfo":{"authors":"bad","imageLinks":"bad"}}]}'
        malformed.open.return_value = FakeResponse(payload)
        result = bulk_import.search_google_books("book", opener_factory=lambda *_: malformed)
        self.assertEqual(result["results"][0]["author"], "")

    def test_cover_download_rejects_private_hosts_and_unsafe_redirects(self):
        private_result = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(bulk_import.socket, "getaddrinfo", return_value=private_result):
            with self.assertRaisesRegex(ValueError, "public internet"):
                bulk_import.validate_https_url("https://private.example/cover.png")

        public_result = [(2, 1, 6, "", ("8.8.8.8", 443))]
        handler = bulk_import._SafeRedirectHandler()
        request = Request("https://books.example/cover.png")
        with patch.object(bulk_import.socket, "getaddrinfo", return_value=public_result):
            with self.assertRaisesRegex(ValueError, "public HTTPS"):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "http://127.0.0.1/internal"
                )
            private_redirect = [(2, 1, 6, "", ("10.0.0.1", 443))]
            with patch.object(
                bulk_import.socket, "getaddrinfo", return_value=private_redirect
            ), self.assertRaisesRegex(ValueError, "public internet"):
                handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://internal.example/private",
                )
            redirect_handler = bulk_import._SafeRedirectHandler(max_redirects=1)
            redirect_handler.redirect_request(
                request, None, 302, "Found", {}, "https://books.example/first"
            )
            with self.assertRaisesRegex(ValueError, "redirect limit"):
                redirect_handler.redirect_request(
                    request, None, 302, "Found", {}, "https://books.example/second"
                )

    def test_cover_download_validates_image_and_limits_download_size(self):
        public_result = [(2, 1, 6, "", ("8.8.8.8", 443))]

        class FakeOpener:
            def __init__(self, response):
                self.response = response

            def open(self, *_args, **_kwargs):
                return self.response

        with patch.object(bulk_import.socket, "getaddrinfo", return_value=public_result):
            data, extension, error = bulk_import.download_cover(
                "https://books.example/cover.png",
                opener_factory=lambda *_handlers: FakeOpener(FakeResponse(PNG)),
            )
            self.assertEqual(extension, ".png")
            self.assertIsNone(error)
            self.assertEqual(data, PNG)
            data, extension, error = bulk_import.download_cover(
                "https://books.example/cover.png",
                opener_factory=lambda *_handlers: FakeOpener(FakeResponse(b"not an image")),
            )
            self.assertIsNone(data)
            self.assertIsNotNone(error)
            with patch.object(bulk_import, "MAX_COVER_DOWNLOAD_BYTES", 4):
                data, extension, error = bulk_import.download_cover(
                    "https://books.example/cover.png",
                    opener_factory=lambda *_handlers: FakeOpener(FakeResponse(PNG)),
                )
            self.assertIsNone(data)
            self.assertIn("2 MB", error)
            failing_opener = Mock()
            failing_opener.open.side_effect = TimeoutError()
            data, extension, error = bulk_import.download_cover(
                "https://books.example/cover.png",
                opener_factory=lambda *_handlers: failing_opener,
            )
            self.assertIsNone(data)
            self.assertIn("safely downloaded", error)

    def test_duplicate_detection_uses_parameterized_normalized_identifiers(self):
        duplicate = {"id": 11, "title": "Sample Book", "author": "Sample Author", "isbn": "9781234567890"}
        with patch.object(openshelf, "fetch_one", return_value=duplicate) as fetch_one:
            reason = openshelf.find_duplicate_book({
                "title": "Sample Book",
                "author": "Sample Author",
                "isbn": "978-1-234567-89-0",
            })
        self.assertIn("ISBN already exists", reason)
        self.assertIn("%s", fetch_one.call_args.args[0])
        self.assertEqual(fetch_one.call_args.args[1][0], "9781234567890")

    def test_normalized_title_author_duplicates_are_detected(self):
        duplicate = {"id": 13, "title": "The Book", "author": "A Writer", "isbn": None}
        with patch.object(openshelf, "fetch_one", return_value=duplicate) as fetch_one:
            reason = openshelf.find_duplicate_book({
                "title": "The-Book!",
                "author": "A Writer",
                "isbn": "",
            })
        self.assertIn("Matching title and author", reason)
        self.assertEqual(fetch_one.call_args.args[1][2:], ("thebook", "awriter"))

    def test_duplicates_inside_one_preview_are_explained(self):
        rows = [
            {"row_number": 2, "data": {"title": "A Book", "author": "An Author", "isbn": "", "genre": "Mystery"}, "errors": []},
            {"row_number": 3, "data": {"title": "A-Book!", "author": "An Author", "isbn": "", "genre": "Mystery"}, "errors": []},
        ]
        openshelf._mark_import_batch_duplicates(rows)
        self.assertIn("row 2", rows[1]["duplicate_reason"])


class BulkImportRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = openshelf.app.test_client()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.import_root_patch = patch.object(
            bulk_import, "IMPORT_ROOT", bulk_import.Path(self.temp_dir.name) / "staging"
        )
        self.import_root_patch.start()
        self.addCleanup(self.import_root_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.set_session(user_id=ADMIN["id"], csrf_token="valid-csrf")

    def set_session(self, **values):
        with self.client.session_transaction() as current_session:
            current_session.clear()
            current_session.update(values)

    def test_import_page_requires_admin_and_sample_is_admin_only(self):
        with patch.object(openshelf, "get_current_user", return_value=None):
            response = self.client.get("/admin/books/import")
        self.assertEqual(response.status_code, 302)

        self.set_session(user_id=REGULAR_USER["id"])
        with patch.object(openshelf, "get_current_user", return_value=REGULAR_USER):
            self.assertEqual(self.client.get("/admin/books/import").status_code, 403)
            self.assertEqual(self.client.get("/admin/books/import/sample.csv").status_code, 403)

    def test_all_import_endpoints_reject_regular_users(self):
        token = "0" * 32
        paths = [
            ("GET", "/admin/books/import"),
            ("GET", "/admin/books/import/sample.csv"),
            ("POST", "/admin/books/import/csv/preview"),
            ("POST", "/admin/books/import/google/search"),
            ("POST", "/admin/books/import/google/preview"),
            ("GET", f"/admin/books/import/{token}/cover/{'a' * 32}.png"),
            ("POST", f"/admin/books/import/{token}/confirm"),
        ]
        self.set_session(user_id=REGULAR_USER["id"], csrf_token="valid-csrf")
        with patch.object(openshelf, "get_current_user", return_value=REGULAR_USER):
            for method, path in paths:
                response = getattr(self.client, method.lower())(path)
                self.assertEqual(response.status_code, 403, path)

    def test_csv_preview_rejects_csrf_and_malformed_csv(self):
        with patch.object(openshelf, "get_current_user", return_value=ADMIN):
            denied = self.client.post(
                "/admin/books/import/csv/preview",
                data={"csrf_token": "wrong", "csv_file": (io.BytesIO(SAMPLE_CSV), "books.csv")},
                content_type="multipart/form-data",
            )
            self.assertEqual(denied.status_code, 302)
            response = self.client.post(
                "/admin/books/import/csv/preview",
                data={
                    "csrf_token": "valid-csrf",
                    "csv_file": (io.BytesIO(b"Title,Author\nA,B\n"), "books.csv"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Title, Author, and Genre", response.data)

    def test_google_preview_and_confirm_reject_invalid_csrf(self):
        token = bulk_import.create_batch(ADMIN["id"], "csv", [])
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "search_google_books"
        ) as google_search, patch.object(openshelf, "_insert_import_record") as insert_record:
            search_response = self.client.post(
                "/admin/books/import/google/search",
                data={"csrf_token": "wrong", "query": "book"},
            )
            preview_response = self.client.post(
                "/admin/books/import/google/preview",
                data={"csrf_token": "wrong", "search_token": token},
            )
            confirm_response = self.client.post(
                f"/admin/books/import/{token}/confirm",
                data={"csrf_token": "wrong"},
            )
        self.assertEqual(search_response.status_code, 302)
        self.assertEqual(preview_response.status_code, 302)
        self.assertEqual(confirm_response.status_code, 302)
        google_search.assert_not_called()
        insert_record.assert_not_called()
        bulk_import.remove_batch(token)

    def test_csv_preview_matches_zip_cover_and_never_extracts_archive(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("covers/sample.png", PNG)
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "find_duplicate_book", return_value=None
        ):
            response = self.client.post(
                "/admin/books/import/csv/preview",
                data={
                    "csrf_token": "valid-csrf",
                    "csv_file": (io.BytesIO(SAMPLE_CSV), "books.csv"),
                    "covers_zip": (io.BytesIO(archive.getvalue()), "covers.zip"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cover preview for Sample Book", response.data)
        staged_root = bulk_import.IMPORT_ROOT
        self.assertTrue(staged_root.exists())
        self.assertEqual(list(staged_root.glob("*/covers")), [])

    def test_google_search_route_is_admin_only_and_handles_api_failure(self):
        self.set_session(user_id=REGULAR_USER["id"], csrf_token="valid-csrf")
        with patch.object(openshelf, "get_current_user", return_value=REGULAR_USER):
            self.assertEqual(
                self.client.post("/admin/books/import/google/search", data={
                    "csrf_token": "valid-csrf",
                    "query": "book",
                }).status_code,
                403,
            )

        self.set_session(user_id=ADMIN["id"], csrf_token="valid-csrf")
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf,
            "search_google_books",
            side_effect=bulk_import.GoogleBooksError("Google Books returned HTTP 503."),
        ):
            response = self.client.post(
                "/admin/books/import/google/search",
                data={"csrf_token": "valid-csrf", "query": "book"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Google Books returned HTTP 503", response.data)

    def test_google_search_success_and_selected_preview_are_staged(self):
        book = {
            "google_volume_id": "volume-1",
            "title": "Google Book",
            "author": "A Writer",
            "isbn": "9781234567890",
            "description": "A short description.",
            "genre": "Mystery",
            "publisher": "Example Press",
            "publication_year": "2022",
            "cover_url": "",
        }
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "search_google_books", return_value={
                "total_items": 25,
                "results": [book],
                "start_index": 0,
            }
        ):
            search_response = self.client.post(
                "/admin/books/import/google/search",
                data={"csrf_token": "valid-csrf", "query": "Google Book"},
            )
        self.assertEqual(search_response.status_code, 200)
        self.assertIn(b"Google Book", search_response.data)
        with self.client.session_transaction() as current_session:
            search_token = current_session.get("csrf_token")
        # Locate the owner-bound batch created by the search response.
        search_batches = list(bulk_import.IMPORT_ROOT.glob("*/manifest.json"))
        self.assertEqual(len(search_batches), 1)
        manifest = bulk_import.json.loads(search_batches[0].read_text(encoding="utf-8"))
        batch_token = search_batches[0].parent.name
        self.assertEqual(manifest["owner_id"], ADMIN["id"])
        self.set_session(user_id=ADMIN["id"], csrf_token=search_token)
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "find_duplicate_book", return_value=None
        ):
            preview_response = self.client.post(
                "/admin/books/import/google/preview",
                data={
                    "csrf_token": search_token,
                    "search_token": batch_token,
                    "selected_volume_ids": ["volume-1"],
                },
            )
        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"A short description.", preview_response.data)
        self.assertIsNone(bulk_import.load_batch(batch_token, ADMIN["id"]))

    def test_database_failure_rolls_back_and_removes_new_cover(self):
        token = bulk_import.create_batch(ADMIN["id"], "csv", [])
        cover_name = bulk_import.store_staged_cover(token, PNG, ".png")
        staged_path = bulk_import.staged_cover_path(token, cover_name)
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = Error("insert failed")
        with tempfile.TemporaryDirectory() as upload_dir, patch.object(
            openshelf, "UPLOAD_FOLDER", bulk_import.Path(upload_dir)
        ), patch.object(openshelf, "get_connection", return_value=connection):
            with self.assertRaises(Error):
                openshelf._insert_import_record(
                    {
                        "data": {
                            "title": "Sample Book",
                            "author": "Sample Author",
                            "genre": "Mystery",
                            "isbn": None,
                            "description": None,
                            "publisher": None,
                            "publication_year": None,
                        },
                        "cover_stage": cover_name,
                    },
                    token,
                )
            connection.rollback.assert_called_once()
            self.assertEqual(list(bulk_import.Path(upload_dir).iterdir()), [])
        self.assertTrue(staged_path.exists())
        bulk_import.remove_batch(token)

    def test_database_success_commits_and_saves_generated_cover_name(self):
        token = bulk_import.create_batch(ADMIN["id"], "csv", [])
        cover_name = bulk_import.store_staged_cover(token, PNG, ".png")
        connection = Mock()
        connection.cursor.return_value.lastrowid = 42
        with tempfile.TemporaryDirectory() as upload_dir, patch.object(
            openshelf, "UPLOAD_FOLDER", bulk_import.Path(upload_dir)
        ), patch.object(openshelf, "get_connection", return_value=connection):
            book_id = openshelf._insert_import_record(
                {
                    "data": {
                        "title": "Sample Book",
                        "author": "Sample Author",
                        "genre": "Mystery",
                        "isbn": None,
                        "description": None,
                        "publisher": None,
                        "publication_year": None,
                    },
                    "cover_stage": cover_name,
                },
                token,
            )
            self.assertEqual(book_id, 42)
            connection.commit.assert_called_once()
            connection.rollback.assert_not_called()
            execute_params = connection.cursor.return_value.execute.call_args.args[1]
            self.assertRegex(execute_params[4], r"^[0-9a-f]{32}\.png$")
            self.assertTrue((bulk_import.Path(upload_dir) / execute_params[4]).exists())
        bulk_import.remove_batch(token)

    def test_confirm_reports_partial_success_failure_and_duplicate_skip(self):
        records = [
            {"row_number": 2, "data": {"title": "One", "author": "A", "genre": "Mystery"}, "errors": []},
            {"row_number": 3, "data": {"title": "Two", "author": "B", "genre": "Fantasy"}, "errors": []},
            {"row_number": 4, "data": {"title": "Three", "author": "C", "genre": "Poetry"}, "errors": [], "duplicate_reason": "already exists"},
        ]
        token = bulk_import.create_batch(ADMIN["id"], "csv", records)
        self.set_session(user_id=ADMIN["id"], csrf_token="valid-csrf")
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "find_duplicate_book", return_value=None
        ), patch.object(
            openshelf, "_insert_import_record", side_effect=[1, Error("insert failed")]
        ):
            response = self.client.post(
                f"/admin/books/import/{token}/confirm",
                data={"csrf_token": "valid-csrf"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.data, rb"Imported</p><strong>1</strong>")
        self.assertRegex(response.data, rb"Skipped</p><strong>1</strong>")
        self.assertRegex(response.data, rb"Failed</p><strong>1</strong>")
        self.assertIsNone(bulk_import.load_batch(token, ADMIN["id"]))


if __name__ == "__main__":
    unittest.main()
