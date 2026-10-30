"""Create the first administrator. Public registration cannot do this."""

import re
import sys

from mysql.connector import Error, IntegrityError
from werkzeug.security import generate_password_hash

from db import execute_query, fetch_one

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_admin(username, email, password):
    username = (username or "").strip()
    email = (email or "").strip().lower()

    if not re.fullmatch(r"[A-Za-z0-9_]{3,50}", username):
        raise ValueError("Username must be 3-50 letters, numbers or underscores.")
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Please enter a valid email address.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    existing = fetch_one(
        "SELECT id, username, email, role FROM users WHERE username = %s OR email = %s",
        (username, email),
    )
    if existing:
        raise ValueError(
            "That username or email is already registered. "
            "Choose a new admin username."
        )

    execute_query(
        """
        INSERT INTO users (username, email, password_hash, role)
        VALUES (%s, %s, %s, 'admin')
        """,
        (username, email, generate_password_hash(password)),
    )


def main():
    if len(sys.argv) != 4:
        print("Usage: python create_admin.py <username> <email> <password>")
        print("Example: python create_admin.py shelfadmin admin@example.com ChangeMe123")
        raise SystemExit(1)

    username, email, password = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        create_admin(username, email, password)
    except ValueError as error:
        print(error)
        raise SystemExit(1) from error
    except IntegrityError:
        print("That username or email is already registered.")
        raise SystemExit(1)
    except Error as error:
        print("Could not create the admin account. Check your .env database settings.")
        print(f"Details: {error}")
        raise SystemExit(1) from error

    print(f"Administrator '{username}' created. Use the Login page to sign in.")


if __name__ == "__main__":
    main()
