import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

previous_app_env = os.environ.get("APP_ENV")
previous_secret = os.environ.get("FLASK_SECRET_KEY")
os.environ["APP_ENV"] = "development"
os.environ["FLASK_SECRET_KEY"] = "test-only-openshelf-secret"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import app as openshelf

if previous_app_env is None:
    os.environ.pop("APP_ENV", None)
else:
    os.environ["APP_ENV"] = previous_app_env
if previous_secret is None:
    os.environ.pop("FLASK_SECRET_KEY", None)
else:
    os.environ["FLASK_SECRET_KEY"] = previous_secret


ADMIN = {
    "id": 1,
    "username": "shelfadmin",
    "role": "admin",
    "is_active": 1,
    "created_at": datetime(2026, 1, 1),
}
BANNED_USER = {
    "id": 22,
    "username": "reader22",
    "email": "private@example.test",
    "password_hash": "",
    "role": "user",
    "is_active": 0,
    "created_at": datetime(2026, 2, 1),
    "display_name": "Reader Twenty Two",
    "bio": "A private profile bio.",
    "favourite_genres": "Mystery",
    "favourite_book": "Sample Book",
    "currently_reading": "Another Book",
    "annual_reading_goal": 12,
    "profile_filename": None,
}


class BanVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.client = openshelf.app.test_client()

    def set_session(self, **values):
        with self.client.session_transaction() as current_session:
            current_session.clear()
            current_session.update(values)

    def test_banned_login_shows_suspension_without_private_details(self):
        from werkzeug.security import generate_password_hash

        banned = dict(BANNED_USER)
        banned["password_hash"] = generate_password_hash("correct-password")
        self.set_session(csrf_token="login-csrf")

        with patch.object(openshelf, "fetch_one", return_value=banned):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": "login-csrf",
                    "identifier": banned["username"],
                    "password": "correct-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This account is suspended.", response.data)
        self.assertNotIn(b"private@example.test", response.data)
        with self.client.session_transaction() as current_session:
            self.assertNotIn("user_id", current_session)

    def test_unbanned_user_can_log_in_again(self):
        from werkzeug.security import generate_password_hash

        restored = dict(BANNED_USER)
        restored["is_active"] = 1
        restored["password_hash"] = generate_password_hash("correct-password")
        self.set_session(csrf_token="login-csrf")

        with patch.object(openshelf, "fetch_one", return_value=restored):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": "login-csrf",
                    "identifier": restored["username"],
                    "password": "correct-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as current_session:
            self.assertEqual(current_session.get("user_id"), restored["id"])

    def test_banned_existing_session_is_cleared_on_protected_request(self):
        self.set_session(user_id=BANNED_USER["id"], csrf_token="old-csrf")

        with patch.object(openshelf, "fetch_one", return_value=dict(BANNED_USER)):
            response = self.client.get("/my-reviews")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        with self.client.session_transaction() as current_session:
            self.assertNotIn("user_id", current_session)
            self.assertNotIn("csrf_token", current_session)

    def test_profiles_are_not_public_and_owner_profile_requires_active_login(self):
        self.assertEqual(self.client.get("/users/22").status_code, 404)
        self.set_session(user_id=BANNED_USER["id"])

        with patch.object(openshelf, "fetch_one", return_value=dict(BANNED_USER)):
            response = self.client.get("/profile")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_public_book_reviews_filter_banned_authors(self):
        book = {
            "id": 5,
            "title": "Sample Book",
            "author": "Sample Author",
            "genre": "Mystery",
            "description": "A sample description.",
            "publisher": None,
            "publication_year": None,
            "isbn": None,
            "cover_filename": None,
            "cover_url": None,
            "avg_rating": Decimal("4.0"),
            "review_count": 1,
        }
        review_sql = []

        def fetch_one(sql, params=None):
            if "FROM books b" in sql and "stats.avg_rating" in sql:
                return dict(book)
            return None

        def fetch_all(sql, params=None):
            if "FROM reviews r" in sql and "JOIN users u" in sql:
                review_sql.append(sql)
                return [{
                    "id": 3,
                    "user_id": 8,
                    "rating": Decimal("4.0"),
                    "review_text": "Visible active-user review.",
                    "is_spoiler": 0,
                    "created_at": datetime(2026, 3, 1),
                    "username": "active_reader",
                    "helpful_count": 0,
                }]
            return []

        with patch.object(openshelf, "fetch_one", side_effect=fetch_one), patch.object(
            openshelf, "fetch_all", side_effect=fetch_all
        ):
            response = self.client.get("/books/5")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Visible active-user review.", response.data)
        self.assertTrue(review_sql)
        self.assertIn("r.status = 'approved'", review_sql[0])
        self.assertIn("u.is_active = 1", review_sql[0])

    def test_public_rating_aggregates_exclude_banned_users(self):
        self.assertIn("u.is_active = 1", openshelf.BOOKS_WITH_STATS)
        self.assertIn("r.status = 'approved'", openshelf.BOOKS_WITH_STATS)

        captured_sql = []

        def capture_fetch_all(sql, params=None):
            captured_sql.append(sql)
            return []

        with patch.object(openshelf, "fetch_all", side_effect=capture_fetch_all):
            openshelf.trending_books()
        trending_sql = next(sql for sql in captured_sql if "recent.recent_count" in sql)
        self.assertGreaterEqual(trending_sql.count("u.is_active = 1"), 2)

        active_reader = {**ADMIN, "id": 22, "role": "user", "username": "reader22"}
        self.set_session(user_id=22)
        captured_sql.clear()
        with patch.object(openshelf, "get_current_user", return_value=active_reader), patch.object(
            openshelf, "fetch_all", side_effect=capture_fetch_all
        ):
            response = self.client.get("/reading-list")
        self.assertEqual(response.status_code, 200)
        reading_sql = next(sql for sql in captured_sql if "rl.status AS list_status" in sql)
        self.assertIn("u.is_active = 1", reading_sql)
        self.assertIn("r.status = 'approved'", reading_sql)

    def test_admin_can_inspect_banned_profile_and_all_review_statuses(self):
        reviews_sql = []
        reviews = [
            {"id": 1, "rating": Decimal("4.0"), "review_text": "Approved review",
             "is_spoiler": 0, "status": "approved", "updated_at": datetime(2026, 1, 1),
             "book_id": 4, "title": "Approved Book", "author": "Author A"},
            {"id": 2, "rating": Decimal("3.0"), "review_text": "Pending review",
             "is_spoiler": 0, "status": "pending", "updated_at": datetime(2026, 1, 2),
             "book_id": 5, "title": "Pending Book", "author": "Author B"},
            {"id": 3, "rating": Decimal("2.0"), "review_text": "Rejected review",
             "is_spoiler": 0, "status": "rejected", "updated_at": datetime(2026, 1, 3),
             "book_id": 6, "title": "Rejected Book", "author": "Author C"},
        ]

        def fetch_one(sql, params=None):
            return dict(BANNED_USER)

        def fetch_all(sql, params=None):
            if "FROM reviews r" in sql and "WHERE r.user_id = %s" in sql:
                reviews_sql.append(sql)
                return reviews
            return []

        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "fetch_one", side_effect=fetch_one
        ), patch.object(openshelf, "fetch_all", side_effect=fetch_all):
            response = self.client.get("/admin/users/22")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reader Twenty Two", response.data)
        self.assertIn(b"A private profile bio.", response.data)
        self.assertIn(b"Approved review", response.data)
        self.assertIn(b"Pending review", response.data)
        self.assertIn(b"Rejected review", response.data)
        self.assertIn(b"Banned", response.data)
        self.assertTrue(reviews_sql)
        self.assertNotIn("u.is_active = 1", reviews_sql[0])
        self.assertNotIn("WHERE r.user_id = %s AND r.status", reviews_sql[0])

    def test_unban_only_changes_active_flag_and_does_not_approve_reviews(self):
        target = {"id": 22, "role": "user", "is_active": 0}
        updates = []
        self.set_session(user_id=ADMIN["id"], csrf_token="admin-csrf")

        def fetch_one(sql, params=None):
            if "SELECT id, role, is_active FROM users" in sql:
                return dict(target)
            if "SELECT role, is_active FROM users" in sql:
                return {"role": target["role"], "is_active": target["is_active"]}
            return None

        def execute_query(sql, params=None):
            updates.append(sql)
            target["is_active"] = params[0]
            return 0

        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=True
        ), patch.object(openshelf, "fetch_one", side_effect=fetch_one), patch.object(
            openshelf, "execute_query", side_effect=execute_query
        ):
            response = self.client.post("/admin/users/22/unban", data={"csrf_token": "admin-csrf"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(target["is_active"], 1)
        self.assertEqual(len(updates), 1)
        self.assertIn("SET is_active = %s", updates[0])
        self.assertNotIn("reviews", updates[0].lower())
        self.assertNotIn("status", updates[0].lower())

    def test_ban_only_changes_active_flag(self):
        target = {"id": 22, "role": "user", "is_active": 1}
        updates = []
        self.set_session(user_id=ADMIN["id"], csrf_token="admin-csrf")

        def fetch_one(sql, params=None):
            if "SELECT id, role, is_active FROM users" in sql:
                return dict(target)
            if "SELECT role, is_active FROM users" in sql:
                return {"role": target["role"], "is_active": target["is_active"]}
            return None

        def execute_query(sql, params=None):
            updates.append(sql)
            target["is_active"] = params[0]
            return 0

        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=True
        ), patch.object(openshelf, "fetch_one", side_effect=fetch_one), patch.object(
            openshelf, "execute_query", side_effect=execute_query
        ):
            response = self.client.post("/admin/users/22/ban", data={"csrf_token": "admin-csrf"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(target["is_active"], 0)
        self.assertEqual(len(updates), 1)
        self.assertIn("SET is_active = %s", updates[0])
        self.assertNotIn("reviews", updates[0].lower())
        self.assertNotIn("status", updates[0].lower())

    def test_ban_route_rejects_bad_csrf_missing_target_and_admin_target(self):
        self.set_session(user_id=ADMIN["id"], csrf_token="admin-csrf")
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=False
        ), patch.object(openshelf, "fetch_one") as fetch_one:
            response = self.client.post("/admin/users/22/ban")
        self.assertEqual(response.status_code, 302)
        fetch_one.assert_not_called()

        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=True
        ), patch.object(openshelf, "fetch_one", return_value=None):
            missing = self.client.post("/admin/users/22/ban")
        self.assertEqual(missing.status_code, 404)

        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=True
        ), patch.object(
            openshelf,
            "fetch_one",
            return_value={"id": 22, "role": "admin", "is_active": 1},
        ), patch.object(openshelf, "execute_query") as execute_query:
            admin_target = self.client.post("/admin/users/22/ban")
        self.assertEqual(admin_target.status_code, 302)
        execute_query.assert_not_called()

    def test_ban_route_prevents_self_ban_and_requires_admin(self):
        self.set_session(user_id=22, csrf_token="admin-csrf")
        with patch.object(openshelf, "get_current_user", return_value=ADMIN), patch.object(
            openshelf, "csrf_ok", return_value=True
        ), patch.object(openshelf, "fetch_one") as fetch_one, patch.object(
            openshelf, "execute_query"
        ) as execute_query:
            self_ban = self.client.post("/admin/users/22/ban")
        self.assertEqual(self_ban.status_code, 302)
        fetch_one.assert_not_called()
        execute_query.assert_not_called()

        ordinary_user = {**ADMIN, "id": 22, "role": "user"}
        self.set_session(user_id=22, csrf_token="user-csrf")
        with patch.object(openshelf, "get_current_user", return_value=ordinary_user):
            forbidden = self.client.post("/admin/users/23/unban")
        self.assertEqual(forbidden.status_code, 403)

        self.set_session()
        with patch.object(openshelf, "get_current_user", return_value=None):
            unauthenticated = self.client.post("/admin/users/23/unban")
        self.assertEqual(unauthenticated.status_code, 302)
        self.assertIn("/login", unauthenticated.location)

    def test_admin_user_detail_is_not_available_to_ordinary_users(self):
        ordinary_user = {**ADMIN, "id": 30, "role": "user"}
        self.set_session(user_id=ordinary_user["id"])

        with patch.object(openshelf, "get_current_user", return_value=ordinary_user):
            response = self.client.get("/admin/users/22")

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
