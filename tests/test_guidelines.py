import os
import unittest
from unittest.mock import patch

previous_app_env = os.environ.get("APP_ENV")
previous_secret = os.environ.get("FLASK_SECRET_KEY")
os.environ["APP_ENV"] = "development"
os.environ["FLASK_SECRET_KEY"] = "test-only-openshelf-guidelines-secret"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import app as openshelf
import create_admin
import db

if previous_app_env is None:
    os.environ.pop("APP_ENV", None)
else:
    os.environ["APP_ENV"] = previous_app_env
if previous_secret is None:
    os.environ.pop("FLASK_SECRET_KEY", None)
else:
    os.environ["FLASK_SECRET_KEY"] = previous_secret


class GuidelinesTests(unittest.TestCase):
    def setUp(self):
        self.client = openshelf.app.test_client()
        with self.client.session_transaction() as current_session:
            current_session.clear()
            current_session["csrf_token"] = "test-csrf-token"

    def test_guidelines_page_is_public_and_contains_core_rules(self):
        response = self.client.get("/guidelines")

        self.assertEqual(response.status_code, 200)
        for phrase in (
            b"Community Guidelines",
            b"Be respectful",
            b"genuine reviews",
            b"No spam",
            b"No harassment or abuse",
            b"fake, coordinated, or misleading ratings",
            b"account may be suspended",
        ):
            self.assertIn(phrase, response.data)

    def test_registration_without_agreement_is_rejected_server_side(self):
        with patch.object(openshelf, "get_current_user", return_value=None), patch.object(
            openshelf, "execute_query"
        ) as execute_query:
            response = self.client.post(
                "/register",
                data={
                    "csrf_token": "test-csrf-token",
                    "username": "new_reader",
                    "email": "new@example.test",
                    "password": "valid-password",
                    "confirm_password": "valid-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You must accept the Community Guidelines", response.data)
        execute_query.assert_not_called()

    def test_registration_with_agreement_records_acceptance(self):
        with patch.object(openshelf, "get_current_user", return_value=None), patch.object(
            openshelf, "execute_query", return_value=15
        ) as execute_query:
            response = self.client.post(
                "/register",
                data={
                    "csrf_token": "test-csrf-token",
                    "username": "new_reader",
                    "email": "new@example.test",
                    "password": "valid-password",
                    "confirm_password": "valid-password",
                    "guidelines_agreement": "1",
                },
            )

        self.assertEqual(response.status_code, 302)
        sql = execute_query.call_args.args[0]
        self.assertIn("guidelines_accepted_at", sql)
        self.assertIn("CURRENT_TIMESTAMP", sql)
        self.assertIn("'user'", sql)

    def test_existing_active_user_can_log_in_without_guidelines_acceptance(self):
        from werkzeug.security import generate_password_hash

        existing_user = {
            "id": 31,
            "username": "existing_reader",
            "email": "existing@example.test",
            "password_hash": generate_password_hash("valid-password"),
            "role": "user",
            "is_active": 1,
        }
        with patch.object(openshelf, "get_current_user", return_value=None), patch.object(
            openshelf, "fetch_one", return_value=existing_user
        ):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": "test-csrf-token",
                    "identifier": "existing_reader",
                    "password": "valid-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as current_session:
            self.assertEqual(current_session.get("user_id"), existing_user["id"])

    def test_admin_creation_does_not_require_regular_user_acceptance(self):
        with patch.object(create_admin, "fetch_one", return_value=None), patch.object(
            create_admin, "execute_query"
        ) as execute_query:
            create_admin.create_admin("new_admin", "admin@example.test", "valid-password")

        sql = execute_query.call_args.args[0]
        self.assertIn("'admin'", sql)
        self.assertNotIn("guidelines_accepted_at", sql)

    def test_guidelines_acceptance_migration_is_additive(self):
        def column_exists(_table_name, column_name):
            return column_name != "guidelines_accepted_at"

        with patch.object(db, "_column_exists", side_effect=column_exists), patch.object(
            db, "execute_query"
        ) as execute_query:
            db.apply_migrations()

        statements = [call.args[0] for call in execute_query.call_args_list]
        acceptance_migrations = [
            statement for statement in statements
            if "guidelines_accepted_at" in statement
        ]
        self.assertEqual(len(acceptance_migrations), 1)
        self.assertIn("ALTER TABLE users ADD COLUMN", acceptance_migrations[0])
        self.assertIn("TIMESTAMP NULL DEFAULT NULL", acceptance_migrations[0])
        self.assertFalse(any("DROP TABLE" in statement.upper() for statement in statements))
        self.assertFalse(any("TRUNCATE" in statement.upper() for statement in statements))


if __name__ == "__main__":
    unittest.main()
