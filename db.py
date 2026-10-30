"""Simple MySQL helpers used by the whole application."""

import os
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error

load_dotenv()

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _database_name():
    database = os.getenv("DB_NAME")
    if not database or not database.strip():
        raise RuntimeError("DB_NAME must be configured before connecting to MySQL.")
    return database.strip()


def _base_config():
    user = os.getenv("DB_USER")
    if not user or not user.strip():
        raise RuntimeError("DB_USER must be configured before connecting to MySQL.")
    if "DB_PASSWORD" not in os.environ:
        raise RuntimeError("DB_PASSWORD must be configured before connecting to MySQL.")

    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": user.strip(),
        "password": os.environ["DB_PASSWORD"],
    }


def get_connection():
    """Open a new MySQL connection using values from the environment."""
    config = _base_config()
    config["database"] = _database_name()
    return mysql.connector.connect(**config)


def fetch_all(sql, params=None):
    """Run a SELECT query and return a list of rows as dictionaries."""
    connection = get_connection()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        connection.close()


def fetch_one(sql, params=None):
    """Run a SELECT query and return one row, or None."""
    connection = get_connection()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params or ())
        row = cursor.fetchone()
        cursor.close()
        return row
    finally:
        connection.close()


def execute_query(sql, params=None):
    """Run INSERT/UPDATE/DELETE and commit the change."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(sql, params or ())
        connection.commit()
        last_id = cursor.lastrowid
        cursor.close()
        return last_id
    except Error:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_schema():
    """Create missing tables in the configured database without dropping data."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = get_connection()
    try:
        cursor = connection.cursor()
        for _result in cursor.execute(sql, multi=True):
            pass
        connection.commit()
        cursor.close()
    except Error:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_tables():
    """Return table names in the application database."""
    rows = fetch_all("SHOW TABLES")
    names = []
    for row in rows:
        names.extend(row.values())
    return names


def _column_exists(table_name, column_name):
    database = _database_name()
    row = fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (database, table_name, column_name),
    )
    return bool(row and row["total"])


def apply_migrations():
    """Add new columns/tables without dropping existing data."""
    if not _column_exists("users", "is_active"):
        execute_query(
            "ALTER TABLE users ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1"
        )
    if not _column_exists("reading_list", "status"):
        execute_query(
            """
            ALTER TABLE reading_list
            ADD COLUMN status ENUM('want_to_read', 'currently_reading', 'finished')
            NOT NULL DEFAULT 'want_to_read'
            """
        )
    profile_columns = (
        ("display_name", "VARCHAR(80) NULL"),
        ("bio", "VARCHAR(500) NULL"),
        ("favourite_genres", "VARCHAR(255) NULL"),
        ("favourite_book", "VARCHAR(120) NULL"),
        ("currently_reading", "VARCHAR(120) NULL"),
        ("annual_reading_goal", "SMALLINT UNSIGNED NULL"),
        ("profile_filename", "VARCHAR(255) NULL"),
        ("guidelines_accepted_at", "TIMESTAMP NULL DEFAULT NULL"),
    )
    for column_name, column_definition in profile_columns:
        if not _column_exists("users", column_name):
            execute_query(
                f"ALTER TABLE users ADD COLUMN {column_name} {column_definition}"
            )
    execute_query(
        """
        CREATE TABLE IF NOT EXISTS review_votes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            review_id INT NOT NULL,
            user_id INT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_user_review_vote (review_id, user_id),
            CONSTRAINT fk_votes_review
                FOREIGN KEY (review_id) REFERENCES reviews(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_votes_user
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    execute_query(
        """
        CREATE TABLE IF NOT EXISTS review_comments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            review_id INT NOT NULL,
            user_id INT NOT NULL,
            comment_text VARCHAR(1000) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_comments_review
                FOREIGN KEY (review_id) REFERENCES reviews(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_comments_user
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
