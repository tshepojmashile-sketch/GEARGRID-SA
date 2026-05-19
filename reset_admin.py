#!/usr/bin/env python3
"""Standalone utility: reset a user's password in rental.db (bcrypt). Does not start the web server."""

from pathlib import Path
import sqlite3
import sys

import bcrypt

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rental.db"


def main() -> None:
    email = input("Enter the email address to reset: ").strip().lower()
    password = input("Enter the new password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        print("No user found with that email")
        sys.exit(0)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cursor.execute(
        "UPDATE users SET password_hash=?, password_salt='bcrypt', must_change_password=0 WHERE id=?",
        (hashed, row[0]),
    )
    conn.commit()
    conn.close()
    print(f"Password successfully reset for {email}")


if __name__ == "__main__":
    main()
