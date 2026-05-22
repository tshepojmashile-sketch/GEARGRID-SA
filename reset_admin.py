#!/usr/bin/env python3
"""Standalone utility: reset a user's password via PostgreSQL (bcrypt). Does not start the web server."""

import os
import sys

import bcrypt
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)
    email = input("Enter the email address to reset: ").strip().lower()
    password = input("Enter the new password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
        row = cursor.fetchone()
        if not row:
            print("No user found with that email")
            return
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        cursor.execute(
            "UPDATE users SET password_hash=%s, password_salt='bcrypt', must_change_password=0 WHERE id=%s",
            (hashed, row["id"]),
        )
        conn.commit()
        print(f"Password successfully reset for {email}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
