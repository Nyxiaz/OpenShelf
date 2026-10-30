import os
import unittest
from unittest.mock import patch

import db


class DatabaseConfigurationTests(unittest.TestCase):
    def test_connection_uses_explicit_environment_configuration(self):
        environment = {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3307",
            "DB_USER": "local_reader",
            "DB_PASSWORD": "local-only-password",
            "DB_NAME": "openshelf_local",
        }

        with patch.dict(os.environ, environment, clear=True), patch.object(
            db.mysql.connector, "connect"
        ) as connect:
            db.get_connection()

        connect.assert_called_once_with(
            host="127.0.0.1",
            port=3307,
            user="local_reader",
            password="local-only-password",
            database="openshelf_local",
        )

    def test_missing_database_credentials_fail_before_connecting(self):
        complete_environment = {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3306",
            "DB_USER": "local_reader",
            "DB_PASSWORD": "local-only-password",
            "DB_NAME": "openshelf_local",
        }

        for missing_setting in ("DB_USER", "DB_PASSWORD", "DB_NAME"):
            with self.subTest(setting=missing_setting):
                environment = dict(complete_environment)
                environment.pop(missing_setting)
                with patch.dict(os.environ, environment, clear=True), patch.object(
                    db.mysql.connector, "connect"
                ) as connect:
                    with self.assertRaisesRegex(RuntimeError, missing_setting):
                        db.get_connection()

                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
