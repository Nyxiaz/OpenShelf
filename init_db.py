"""Create missing OpenShelf tables in the configured database."""

from mysql.connector import Error

from db import apply_migrations, apply_schema, list_tables


def main():
    try:
        apply_schema()
        apply_migrations()
        tables = list_tables()
    except Error as error:
        print("Could not set up the database.")
        print("Check DB_HOST, DB_USER and DB_PASSWORD in your .env file.")
        print(f"Details: {error}")
        raise SystemExit(1) from error

    print("Database is ready.")
    print("Tables:", ", ".join(tables) if tables else "(none)")


if __name__ == "__main__":
    main()
