from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
from io import BytesIO
from xml.sax.saxutils import escape

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rental.db"
SESSION_DAYS = 14
SESSION_SECURE_COOKIE = os.getenv("SESSION_SECURE_COOKIE", "0") == "1"
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
VALID_ROLES = {"admin", "management", "warehouse"}
JOB_STATUSES = {"upcoming", "active", "done"}
QUOTE_STATUSES = {"pending", "approved", "rejected"}
logger = logging.getLogger("rental_saas")

app = FastAPI(title="Rental SaaS App")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_column(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    cols = [row["name"] for row in cursor.fetchall()]
    return column in cols


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def compute_financial_totals(subtotal, discount_percent, vat_enabled, vat_percent):
    discount_amount = round(subtotal * discount_percent / 100, 2)
    discounted = round(subtotal - discount_amount, 2)
    vat_amount = round(discounted * vat_percent / 100, 2) if vat_enabled else 0.0
    grand_total = round(discounted + vat_amount, 2)
    return {
        "subtotal": subtotal,
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "vat_enabled": vat_enabled,
        "vat_percent": vat_percent,
        "vat_amount": vat_amount,
        "grand_total": grand_total,
    }


def init_db() -> None:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
        """
    )
    if not has_column(cursor, "users", "role"):
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'admin'")
    if not has_column(cursor, "users", "must_change_password"):
        cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    cursor.execute("UPDATE users SET role='admin' WHERE role IS NULL OR role=''")
    cursor.execute("UPDATE users SET must_change_password=0 WHERE must_change_password IS NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            csrf_token TEXT,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    if not has_column(cursor, "sessions", "csrf_token"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN csrf_token TEXT")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            requested_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            status TEXT,
            rented_to TEXT,
            due_date TEXT,
            price INTEGER,
            prep_status TEXT DEFAULT 'pending'
        )
        """
    )
    if not has_column(cursor, "equipment", "prep_status"):
        cursor.execute("ALTER TABLE equipment ADD COLUMN prep_status TEXT DEFAULT 'pending'")
    if not has_column(cursor, "equipment", "quantity"):
        cursor.execute("ALTER TABLE equipment ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
    if not has_column(cursor, "equipment", "quantity_rented"):
        cursor.execute("ALTER TABLE equipment ADD COLUMN quantity_rented INTEGER NOT NULL DEFAULT 0")
    cursor.execute("UPDATE equipment SET quantity=1 WHERE quantity IS NULL OR quantity < 1")
    cursor.execute("UPDATE equipment SET quantity_rented=0 WHERE quantity_rented IS NULL")
    cursor.execute("UPDATE equipment SET quantity_rented=1 WHERE status='rented' AND quantity_rented=0")
    cursor.execute("UPDATE equipment SET quantity = MAX(quantity, quantity_rented) WHERE quantity < quantity_rented")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS rental_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equipment_name TEXT,
            client TEXT,
            date_rented TEXT,
            due_date TEXT,
            date_returned TEXT
        )
        """
    )

    for table in ["equipment", "clients", "rental_history"]:
        if not has_column(cursor, table, "company_id"):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN company_id INTEGER DEFAULT 1")
        cursor.execute(f"UPDATE {table} SET company_id=1 WHERE company_id IS NULL")

    for col, ddl in (
        ("contact_person", "ALTER TABLE clients ADD COLUMN contact_person TEXT"),
        ("phone", "ALTER TABLE clients ADD COLUMN phone TEXT"),
        ("email", "ALTER TABLE clients ADD COLUMN email TEXT"),
        ("address", "ALTER TABLE clients ADD COLUMN address TEXT"),
        ("vat_number", "ALTER TABLE clients ADD COLUMN vat_number TEXT"),
    ):
        if not has_column(cursor, "clients", col):
            cursor.execute(ddl)

    if not has_column(cursor, "rental_history", "units"):
        cursor.execute("ALTER TABLE rental_history ADD COLUMN units INTEGER NOT NULL DEFAULT 1")

    if table_exists(cursor, "company_settings") and not has_column(cursor, "company_settings", "company_id"):
        cursor.execute("ALTER TABLE company_settings RENAME TO company_settings_legacy")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS company_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL UNIQUE,
            company_name TEXT NOT NULL,
            tagline TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            vat_number TEXT,
            quote_footer TEXT,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
        """
    )
    if table_exists(cursor, "company_settings_legacy"):
        cursor.execute("SELECT * FROM company_settings_legacy LIMIT 1")
        legacy = cursor.fetchone()
        if legacy:
            cursor.execute(
                """
                INSERT OR IGNORE INTO company_settings
                (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    legacy["company_name"],
                    legacy["tagline"],
                    legacy["email"],
                    legacy["phone"],
                    legacy["address"],
                    legacy["vat_number"],
                    legacy["quote_footer"],
                ),
            )
        cursor.execute("DROP TABLE company_settings_legacy")

    cursor.execute("INSERT OR IGNORE INTO companies (id, name, created_at) VALUES (1, ?, ?)", ("Default Company", datetime.utcnow().isoformat()))
    cursor.execute(
        """
        INSERT OR IGNORE INTO company_settings
        (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
        VALUES
        (1, 'AVMAN RENTALS', 'Professional AV Equipment Rentals', 'info@avman.co.za', '+27 00 000 0000', '', '', 'Thank you for your business.')
        """
    )

    (BASE_DIR / "static" / "logos").mkdir(parents=True, exist_ok=True)

    for col, ddl in (
        ("tagline", "ALTER TABLE companies ADD COLUMN tagline TEXT"),
        ("email", "ALTER TABLE companies ADD COLUMN email TEXT"),
        ("phone", "ALTER TABLE companies ADD COLUMN phone TEXT"),
        ("address", "ALTER TABLE companies ADD COLUMN address TEXT"),
        ("vat_number", "ALTER TABLE companies ADD COLUMN vat_number TEXT"),
        ("vat_percent", "ALTER TABLE companies ADD COLUMN vat_percent REAL NOT NULL DEFAULT 15"),
        ("vat_enabled", "ALTER TABLE companies ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
        ("default_discount_percent", "ALTER TABLE companies ADD COLUMN default_discount_percent REAL NOT NULL DEFAULT 0"),
        ("bank_name", "ALTER TABLE companies ADD COLUMN bank_name TEXT"),
        ("bank_account_holder", "ALTER TABLE companies ADD COLUMN bank_account_holder TEXT"),
        ("bank_account_number", "ALTER TABLE companies ADD COLUMN bank_account_number TEXT"),
        ("bank_account_type", "ALTER TABLE companies ADD COLUMN bank_account_type TEXT"),
        ("bank_branch_code", "ALTER TABLE companies ADD COLUMN bank_branch_code TEXT"),
        ("bank_reference", "ALTER TABLE companies ADD COLUMN bank_reference TEXT"),
        ("terms_and_conditions", "ALTER TABLE companies ADD COLUMN terms_and_conditions TEXT"),
    ):
        if not has_column(cursor, "companies", col):
            cursor.execute(ddl)

    cursor.execute(
        """
        UPDATE companies SET
            tagline = COALESCE(tagline, (SELECT tagline FROM company_settings WHERE company_id = companies.id)),
            email = COALESCE(email, (SELECT email FROM company_settings WHERE company_id = companies.id)),
            phone = COALESCE(phone, (SELECT phone FROM company_settings WHERE company_id = companies.id)),
            address = COALESCE(address, (SELECT address FROM company_settings WHERE company_id = companies.id)),
            vat_number = COALESCE(vat_number, (SELECT vat_number FROM company_settings WHERE company_id = companies.id))
        WHERE EXISTS (SELECT 1 FROM company_settings cs WHERE cs.company_id = companies.id)
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            quote_number TEXT NOT NULL,
            client_name TEXT NOT NULL,
            quote_date TEXT NOT NULL,
            total INTEGER NOT NULL,
            line_items_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            UNIQUE(company_id, quote_number)
        )
        """
    )
    for qcol, qddl in (
        ("subtotal", "ALTER TABLE quotes ADD COLUMN subtotal INTEGER"),
        ("discount_percent", "ALTER TABLE quotes ADD COLUMN discount_percent REAL NOT NULL DEFAULT 0"),
        ("discount_amount", "ALTER TABLE quotes ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0"),
        ("vat_enabled", "ALTER TABLE quotes ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
        ("vat_percent", "ALTER TABLE quotes ADD COLUMN vat_percent REAL NOT NULL DEFAULT 15"),
        ("vat_amount", "ALTER TABLE quotes ADD COLUMN vat_amount INTEGER NOT NULL DEFAULT 0"),
        ("grand_total", "ALTER TABLE quotes ADD COLUMN grand_total INTEGER"),
        ("job_name", "ALTER TABLE quotes ADD COLUMN job_name TEXT"),
        ("site_location", "ALTER TABLE quotes ADD COLUMN site_location TEXT"),
        ("start_date", "ALTER TABLE quotes ADD COLUMN start_date TEXT"),
        ("end_date", "ALTER TABLE quotes ADD COLUMN end_date TEXT"),
        ("special_notes", "ALTER TABLE quotes ADD COLUMN special_notes TEXT"),
        ("quote_terms", "ALTER TABLE quotes ADD COLUMN quote_terms TEXT"),
    ):
        if not has_column(cursor, "quotes", qcol):
            cursor.execute(qddl)
    if has_column(cursor, "quotes", "subtotal"):
        cursor.execute(
            """
            UPDATE quotes
            SET subtotal = COALESCE(subtotal, total),
                discount_amount = COALESCE(discount_amount, 0),
                vat_amount = COALESCE(vat_amount, 0),
                grand_total = COALESCE(grand_total, total)
            WHERE grand_total IS NULL OR subtotal IS NULL
            """
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            invoice_number TEXT NOT NULL,
            quote_id INTEGER,
            client_name TEXT NOT NULL,
            line_items_json TEXT NOT NULL,
            total INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            due_date TEXT NOT NULL,
            payment_status TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(quote_id) REFERENCES quotes(id),
            UNIQUE(company_id, invoice_number)
        )
        """
    )
    for icol, iddl in (
        ("subtotal", "ALTER TABLE invoices ADD COLUMN subtotal INTEGER"),
        ("discount_percent", "ALTER TABLE invoices ADD COLUMN discount_percent REAL NOT NULL DEFAULT 0"),
        ("discount_amount", "ALTER TABLE invoices ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0"),
        ("vat_enabled", "ALTER TABLE invoices ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
        ("vat_percent", "ALTER TABLE invoices ADD COLUMN vat_percent REAL NOT NULL DEFAULT 15"),
        ("vat_amount", "ALTER TABLE invoices ADD COLUMN vat_amount INTEGER NOT NULL DEFAULT 0"),
        ("grand_total", "ALTER TABLE invoices ADD COLUMN grand_total INTEGER"),
    ):
        if not has_column(cursor, "invoices", icol):
            cursor.execute(iddl)
    if has_column(cursor, "invoices", "subtotal"):
        cursor.execute(
            """
            UPDATE invoices
            SET subtotal = COALESCE(subtotal, total),
                discount_amount = COALESCE(discount_amount, 0),
                vat_amount = COALESCE(vat_amount, 0),
                grand_total = COALESCE(grand_total, total)
            WHERE grand_total IS NULL OR subtotal IS NULL
            """
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            quote_id INTEGER NOT NULL,
            invoice_id INTEGER,
            client_name TEXT NOT NULL,
            job_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'upcoming',
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(quote_id) REFERENCES quotes(id),
            FOREIGN KEY(invoice_id) REFERENCES invoices(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_prep_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            equipment_id INTEGER,
            equipment_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            packed INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
        """
    )
    if not has_column(cursor, "job_prep_items", "equipment_id"):
        cursor.execute("ALTER TABLE job_prep_items ADD COLUMN equipment_id INTEGER")
    if not has_column(cursor, "job_prep_items", "line_type"):
        cursor.execute("ALTER TABLE job_prep_items ADD COLUMN line_type TEXT NOT NULL DEFAULT 'owned'")
    if not has_column(cursor, "job_prep_items", "sub_rental_id"):
        cursor.execute("ALTER TABLE job_prep_items ADD COLUMN sub_rental_id INTEGER")
    if not has_column(cursor, "job_prep_items", "supplier_name"):
        cursor.execute("ALTER TABLE job_prep_items ADD COLUMN supplier_name TEXT")
    if not has_column(cursor, "job_prep_items", "received_from_supplier"):
        cursor.execute("ALTER TABLE job_prep_items ADD COLUMN received_from_supplier INTEGER NOT NULL DEFAULT 0")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sub_rentals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            supplier_name TEXT NOT NULL,
            equipment_description TEXT NOT NULL,
            quantity_total INTEGER NOT NULL,
            quantity_available INTEGER NOT NULL,
            cost_per_unit INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sub_rental_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            sub_rental_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            units_used INTEGER NOT NULL,
            show_on_quote INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(sub_rental_id) REFERENCES sub_rentals(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            changed_by_user_id INTEGER NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(changed_by_user_id) REFERENCES users(id)
        )
        """
    )

    if not table_exists(cursor, "invoice_payments"):
        cursor.execute(
            """
            CREATE TABLE invoice_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                invoice_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                recorded_by_user_id INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id),
                FOREIGN KEY(invoice_id) REFERENCES invoices(id),
                FOREIGN KEY(recorded_by_user_id) REFERENCES users(id)
            )
            """
        )
    if table_exists(cursor, "invoices") and not has_column(cursor, "invoices", "amount_paid"):
        cursor.execute("ALTER TABLE invoices ADD COLUMN amount_paid INTEGER NOT NULL DEFAULT 0")
    if table_exists(cursor, "invoices") and has_column(cursor, "invoices", "amount_paid"):
        cursor.execute("UPDATE invoices SET amount_paid=0 WHERE amount_paid IS NULL")
        cursor.execute(
            """
            UPDATE invoices SET payment_status = CASE
                WHEN COALESCE(amount_paid, 0) >= total THEN 'paid'
                WHEN COALESCE(amount_paid, 0) > 0 THEN 'partial'
                ELSE 'unpaid'
            END
            """
        )
    if table_exists(cursor, "company_settings") and not has_column(cursor, "company_settings", "company_logo_path"):
        cursor.execute("ALTER TABLE company_settings ADD COLUMN company_logo_path TEXT")

    if table_exists(cursor, "jobs"):
        cursor.execute(
            "UPDATE jobs SET status='upcoming' WHERE status IN ('pending', 'confirmed', 'in_progress', 'completed')"
        )
    if table_exists(cursor, "job_status_log"):
        for old, new in (
            ("pending", "upcoming"),
            ("confirmed", "upcoming"),
            ("in_progress", "active"),
            ("completed", "done"),
        ):
            cursor.execute("UPDATE job_status_log SET old_status=? WHERE old_status=?", (new, old))
            cursor.execute("UPDATE job_status_log SET new_status=? WHERE new_status=?", (new, old))

    conn.commit()
    conn.close()


init_db()


def log_job_status_change(
    cursor: sqlite3.Cursor, company_id: int, job_id: int, old_status: str | None, new_status: str, user_id: int
) -> None:
    cursor.execute(
        """
        INSERT INTO job_status_log (company_id, job_id, old_status, new_status, changed_at, changed_by_user_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (company_id, job_id, old_status, new_status, datetime.utcnow().isoformat(), user_id),
    )


def warehouse_may_set_job_status(from_status: str, to_status: str) -> bool:
    return (from_status == "upcoming" and to_status == "active") or (from_status == "active" and to_status == "done")


def qty_total(row: sqlite3.Row | dict) -> int:
    v = row["quantity"] if "quantity" in row.keys() else None
    return max(1, int(v if v is not None else 1))


def qty_rented(row: sqlite3.Row | dict) -> int:
    v = row["quantity_rented"] if "quantity_rented" in row.keys() else None
    return max(0, int(v if v is not None else 0))


def qty_available(row: sqlite3.Row | dict) -> int:
    return max(0, qty_total(row) - qty_rented(row))


def sync_equipment_row(cursor: sqlite3.Cursor, equip_id: int, company_id: int) -> None:
    cursor.execute(
        "SELECT quantity, quantity_rented, rented_to, due_date FROM equipment WHERE id=? AND company_id=?",
        (equip_id, company_id),
    )
    row = cursor.fetchone()
    if not row:
        return
    qt = qty_total(row)
    qr = min(qty_rented(row), qt)
    if qr < 0:
        qr = 0
    if qr <= 0:
        cursor.execute(
            """
            UPDATE equipment
            SET quantity_rented=0, status='available', rented_to=NULL, due_date=NULL, prep_status='pending'
            WHERE id=? AND company_id=?
            """,
            (equip_id, company_id),
        )
    else:
        cursor.execute(
            "UPDATE equipment SET quantity_rented=?, status='rented' WHERE id=? AND company_id=?",
            (qr, equip_id, company_id),
        )


def process_job_status_stock_delta(cursor: sqlite3.Cursor, company_id: int, job_id: int, old_status: str, new_status: str) -> tuple[bool, str]:
    if old_status == new_status:
        return True, ""
    if new_status == "done" and old_status != "done":
        release_job_stock(cursor, company_id, job_id)
        restore_sub_rental_stock_after_job_done(cursor, company_id, job_id)
        return True, ""
    if old_status == "done" and new_status != "done":
        ok, err = reserve_job_stock_from_prep(cursor, company_id, job_id)
        if not ok:
            return ok, err
        return reserve_sub_rental_stock_when_job_reopened_from_done(cursor, company_id, job_id)
    return True, ""


def _resolve_prep_equipment_id(cursor: sqlite3.Cursor, company_id: int, row: sqlite3.Row) -> int | None:
    eid = row["equipment_id"]
    if eid is not None and eid != "":
        try:
            return int(eid)
        except (TypeError, ValueError):
            pass
    cursor.execute(
        "SELECT id FROM equipment WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
        (company_id, row["equipment_name"]),
    )
    found = cursor.fetchone()
    return int(found["id"]) if found else None


def _prep_row_is_sub_rental(row: sqlite3.Row) -> bool:
    lt = row["line_type"] if "line_type" in row.keys() else None
    if (lt or "owned") == "sub_rental":
        return True
    sid = row["sub_rental_id"] if "sub_rental_id" in row.keys() else None
    return sid is not None and sid != ""


def release_job_stock(cursor: sqlite3.Cursor, company_id: int, job_id: int) -> None:
    cursor.execute(
        "SELECT equipment_id, equipment_name, quantity, line_type, sub_rental_id FROM job_prep_items WHERE job_id=? AND company_id=?",
        (job_id, company_id),
    )
    for row in cursor.fetchall():
        if _prep_row_is_sub_rental(row):
            continue
        qty = max(1, int(row["quantity"] or 1))
        eid = _resolve_prep_equipment_id(cursor, company_id, row)
        if not eid:
            continue
        cursor.execute(
            "UPDATE equipment SET quantity_rented = MAX(0, COALESCE(quantity_rented,0) - ?) WHERE id=? AND company_id=?",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)


def reserve_job_stock_from_prep(cursor: sqlite3.Cursor, company_id: int, job_id: int) -> tuple[bool, str]:
    cursor.execute(
        "SELECT equipment_id, equipment_name, quantity, line_type, sub_rental_id FROM job_prep_items WHERE job_id=? AND company_id=?",
        (job_id, company_id),
    )
    rows = cursor.fetchall()
    checks: list[tuple[int, int]] = []
    for row in rows:
        if _prep_row_is_sub_rental(row):
            continue
        qty = max(1, int(row["quantity"] or 1))
        eid = _resolve_prep_equipment_id(cursor, company_id, row)
        if not eid:
            return False, "Could not match equipment for job line."
        cursor.execute("SELECT quantity, quantity_rented FROM equipment WHERE id=? AND company_id=?", (eid, company_id))
        er = cursor.fetchone()
        if not er:
            return False, "Equipment not found."
        if qty_available(er) < qty:
            return False, "Not enough units available"
        checks.append((eid, qty))
    for eid, qty in checks:
        cursor.execute(
            "UPDATE equipment SET quantity_rented = COALESCE(quantity_rented,0) + ? WHERE id=? AND company_id=?",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)
    return True, ""


def quote_line_is_sub_rental(line: dict) -> bool:
    return line.get("line_type") == "sub_rental"


def reserve_sub_rental_stock_for_quote_lines(cursor: sqlite3.Cursor, company_id: int, lines: list) -> tuple[bool, str]:
    sub_lines = [ln for ln in lines if quote_line_is_sub_rental(ln)]
    for line in sub_lines:
        qty = max(1, int(line.get("qty", 1) or 1))
        sid = line.get("sub_rental_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return False, "Invalid sub-rental line."
        cursor.execute(
            "SELECT quantity_available FROM sub_rentals WHERE id=? AND company_id=?",
            (sid, company_id),
        )
        sr = cursor.fetchone()
        if not sr:
            return False, "Sub-rental item not found."
        avail = max(0, int(sr["quantity_available"] or 0))
        if qty > avail:
            return False, "Not enough sub-rental units available"
    for line in sub_lines:
        qty = max(1, int(line.get("qty", 1) or 1))
        sid = int(line["sub_rental_id"])
        cursor.execute(
            """
            UPDATE sub_rentals
            SET quantity_available = quantity_available - ?
            WHERE id=? AND company_id=? AND quantity_available >= ?
            """,
            (qty, sid, company_id, qty),
        )
        if cursor.rowcount != 1:
            return False, "Could not reserve sub-rental stock"
    return True, ""


def restore_sub_rental_stock_after_job_done(cursor: sqlite3.Cursor, company_id: int, job_id: int) -> None:
    cursor.execute(
        "SELECT sub_rental_id, units_used FROM sub_rental_usage WHERE job_id=? AND company_id=?",
        (job_id, company_id),
    )
    for row in cursor.fetchall():
        u = max(1, int(row["units_used"] or 1))
        sid = int(row["sub_rental_id"])
        cursor.execute(
            """
            UPDATE sub_rentals
            SET quantity_available = MIN(quantity_total, quantity_available + ?)
            WHERE id=? AND company_id=?
            """,
            (u, sid, company_id),
        )


def reserve_sub_rental_stock_when_job_reopened_from_done(cursor: sqlite3.Cursor, company_id: int, job_id: int) -> tuple[bool, str]:
    cursor.execute(
        "SELECT sub_rental_id, units_used FROM sub_rental_usage WHERE job_id=? AND company_id=?",
        (job_id, company_id),
    )
    rows = cursor.fetchall()
    for row in rows:
        u = max(1, int(row["units_used"] or 1))
        sid = int(row["sub_rental_id"])
        cursor.execute(
            "SELECT quantity_available FROM sub_rentals WHERE id=? AND company_id=?",
            (sid, company_id),
        )
        sr = cursor.fetchone()
        if not sr:
            return False, "Sub-rental item not found."
        if int(sr["quantity_available"] or 0) < u:
            return False, "Not enough sub-rental units available"
    for row in rows:
        u = max(1, int(row["units_used"] or 1))
        sid = int(row["sub_rental_id"])
        cursor.execute(
            """
            UPDATE sub_rentals
            SET quantity_available = quantity_available - ?
            WHERE id=? AND company_id=? AND quantity_available >= ?
            """,
            (u, sid, company_id, u),
        )
        if cursor.rowcount != 1:
            return False, "Could not adjust sub-rental stock"
    return True, ""


def reserve_stock_for_quote_lines(cursor: sqlite3.Cursor, company_id: int, lines: list) -> tuple[bool, str]:
    for line in lines:
        if quote_line_is_sub_rental(line):
            continue
        qty = max(1, int(line.get("qty", 1) or 1))
        eid = line.get("equipment_id")
        if eid:
            try:
                eid = int(eid)
            except (TypeError, ValueError):
                eid = None
        if not eid:
            name = str(line.get("name", "")).strip()
            if not name:
                return False, "Invalid quote line."
            cursor.execute(
                "SELECT id FROM equipment WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
                (company_id, name),
            )
            found = cursor.fetchone()
            if not found:
                return False, f"No equipment named {name!r}."
            eid = int(found["id"])
        cursor.execute("SELECT quantity, quantity_rented FROM equipment WHERE id=? AND company_id=?", (eid, company_id))
        er = cursor.fetchone()
        if not er:
            return False, "Equipment not found."
        if qty_available(er) < qty:
            return False, "Not enough units available"
    for line in lines:
        if quote_line_is_sub_rental(line):
            continue
        qty = max(1, int(line.get("qty", 1) or 1))
        eid = line.get("equipment_id")
        if eid:
            try:
                eid = int(eid)
            except (TypeError, ValueError):
                eid = None
        if not eid:
            name = str(line.get("name", "")).strip()
            cursor.execute(
                "SELECT id FROM equipment WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
                (company_id, name),
            )
            eid = int(cursor.fetchone()["id"])
        cursor.execute(
            "UPDATE equipment SET quantity_rented = COALESCE(quantity_rented,0) + ? WHERE id=? AND company_id=?",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)
    return True, ""


def apply_rental_return_units(
    cursor: sqlite3.Cursor, company_id: int, equipment_name: str, return_units: int
) -> None:
    remaining = return_units
    while remaining > 0:
        cursor.execute(
            """
            SELECT id, COALESCE(units, 1) AS u FROM rental_history
            WHERE equipment_name=? AND company_id=? AND date_returned IS NULL
            ORDER BY id ASC LIMIT 1
            """,
            (equipment_name, company_id),
        )
        h = cursor.fetchone()
        if not h:
            break
        hu = max(1, int(h["u"]))
        if hu <= remaining:
            cursor.execute(
                "UPDATE rental_history SET date_returned=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d"), h["id"]),
            )
            remaining -= hu
        else:
            cursor.execute("UPDATE rental_history SET units=? WHERE id=?", (hu - remaining, h["id"]))
            remaining = 0


def next_invoice_number(cursor: sqlite3.Cursor, company_id: int) -> str:
    cursor.execute("SELECT COUNT(*) AS c FROM invoices WHERE company_id=?", (company_id,))
    n = cursor.fetchone()["c"] + 1
    return f"INV-{company_id}-{n:05d}"


def parse_invoice_line_items_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def company_logo_file_uri(settings: dict | None) -> str | None:
    if not settings:
        return None
    cid = settings.get("company_id")
    if cid is not None:
        try:
            p = company_static_logo_path(int(cid))
            if p.is_file():
                return p.resolve().as_uri()
        except (TypeError, ValueError, OSError):
            pass
    raw = settings.get("company_logo_path")
    if raw is None or str(raw).strip() == "":
        return None
    p = Path(str(raw).strip())
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        if p.is_file():
            return p.resolve().as_uri()
    except OSError:
        return None
    return None


def company_logo_web_path(settings: dict | None) -> str | None:
    """URL path under this app for logos inside the project (e.g. static/). file:// is not reliable in browsers."""
    if not settings:
        return None
    cid = settings.get("company_id")
    if cid is not None:
        p = company_static_logo_path(int(cid))
        try:
            if p.is_file():
                return f"/static/logos/company_{int(cid)}.png"
        except (TypeError, ValueError, OSError):
            pass
    raw = settings.get("company_logo_path")
    if raw is None or str(raw).strip() == "":
        return None
    p = Path(str(raw).strip())
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        p = p.resolve()
        base = BASE_DIR.resolve()
        if not p.is_file():
            return None
        rel = p.relative_to(base)
        s = str(rel).replace("\\", "/")
        if s.startswith("static/"):
            return "/" + s
    except (ValueError, OSError):
        return None
    return None


def refresh_invoice_payment_aggregate(cursor: sqlite3.Cursor, invoice_id: int, company_id: int) -> None:
    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS s FROM invoice_payments
        WHERE invoice_id=? AND company_id=?
        """,
        (invoice_id, company_id),
    )
    paid_sum = int(cursor.fetchone()["s"] or 0)
    cursor.execute(
        "SELECT total FROM invoices WHERE id=? AND company_id=?",
        (invoice_id, company_id),
    )
    row = cursor.fetchone()
    if not row:
        return
    total = int(row["total"] or 0)
    if paid_sum >= total and total >= 0:
        status = "paid"
    elif paid_sum > 0:
        status = "partial"
    else:
        status = "unpaid"
    cursor.execute(
        "UPDATE invoices SET amount_paid=?, payment_status=? WHERE id=? AND company_id=?",
        (paid_sum, status, invoice_id, company_id),
    )


def invoice_line_subtotal(lines: list[dict]) -> int:
    s = 0
    for line in lines:
        try:
            s += int(line.get("line_total", 0) or 0)
        except (TypeError, ValueError):
            continue
    return s


def build_invoice_pdf_bytes(
    settings: dict,
    inv: dict,
    line_items: list[dict],
    amount_paid: int,
    total: int,
    remaining: int,
    payment_status: str,
) -> bytes:
    """Build invoice PDF using ReportLab (no HTML / WeasyPrint)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    story: list = []
    navy = colors.HexColor("#1a1a2e")
    grid = colors.HexColor("#d9dce3")
    cid = int(inv.get("company_id") or 0)

    cn = escape(str(settings.get("company_name") or "Company"))
    left_parts = [f"<b><font size='14'>{cn}</font></b>"]
    if settings.get("address"):
        left_parts.append(escape(str(settings["address"]).strip()))
    if settings.get("email"):
        left_parts.append(escape(str(settings["email"]).strip()))
    if settings.get("phone"):
        left_parts.append(escape(str(settings["phone"]).strip()))
    left_html = "<br/>".join(left_parts)
    left_para = Paragraph(left_html, styles["Normal"])

    inv_no = escape(str(inv.get("invoice_number") or ""))
    created_raw = str(inv.get("created_at") or "")
    created = escape(created_raw[:10] if created_raw else "—")
    due = escape(str(inv.get("due_date") or "—"))
    right_html = (
        f'<para align="right"><b><font size="18" color="#1a1a2e">INVOICE</font></b><br/><br/>'
        f"<b>No.</b> {inv_no}<br/>"
        f"<b>Date created</b> {created}<br/>"
        f"<b>Due date</b> {due}</para>"
    )
    right_para = Paragraph(right_html, styles["Normal"])

    logo_flow = pdf_company_logo_flowable(cid) if cid else None
    if logo_flow:
        left_inner_rows: list[list] = [[logo_flow, left_para]]
        left_inner = Table(left_inner_rows, colWidths=[32 * mm, 68 * mm])
        left_inner.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0, 0), (0, 0), "LEFT"),
                    ("ALIGN", (1, 0), (1, 0), "LEFT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        left_cell = left_inner
    else:
        left_cell = left_para

    header_table = Table([[left_cell, right_para]], colWidths=[100 * mm, 69 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 14))

    client_name = str(inv.get("client_name") or "—")
    client_prof = client_profile_by_name(cid, client_name) if cid else None
    for flow in pdf_client_details_flowables(client_name, client_prof, styles):
        story.append(flow)
    quote_row = None
    qid = inv.get("quote_id")
    if cid and qid:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM quotes WHERE id=? AND company_id=?", (int(qid), cid))
        quote_row = cur.fetchone()
        conn.close()
    if quote_row:
        qd = dict(quote_row)
        for flow in pdf_job_details_flowables(
            qd.get("job_name"),
            qd.get("site_location"),
            qd.get("start_date"),
            qd.get("end_date"),
            qd.get("special_notes"),
            styles,
        ):
            story.append(flow)
    story.append(Spacer(1, 4))

    inv_equip_cell_style = ParagraphStyle(
        name="InvoicePdfEquipmentCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceBefore=0,
        spaceAfter=0,
    )
    table_rows: list[list] = [["Equipment", "Qty", "Unit price", "Line total"]]
    for line in line_items:
        nm = escape(str(line.get("name", "—")))
        try:
            q = int(line.get("qty", 1) or 1)
        except (TypeError, ValueError):
            q = 1
        try:
            up = int(line.get("unit_price", 0) or 0)
        except (TypeError, ValueError):
            up = 0
        try:
            lt = int(line.get("line_total", 0) or 0)
        except (TypeError, ValueError):
            lt = 0
        table_rows.append([Paragraph(nm, inv_equip_cell_style), str(q), f"R{up}", f"R{lt}"])

    inv_col_widths = [112 * mm, 14 * mm, 26 * mm, 26 * mm]
    items_table = Table(table_rows, colWidths=inv_col_widths, repeatRows=1)
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), navy),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, grid),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 14))

    fin = invoice_financials_from_row(inv, line_items)
    red_hex = "#b91c1c"
    discount_label_style = ParagraphStyle(
        name="InvoicePdfDiscountLbl",
        parent=styles["Normal"],
        textColor=colors.HexColor(red_hex),
        fontSize=10,
        alignment=TA_LEFT,
    )
    discount_amt_style = ParagraphStyle(
        name="InvoicePdfDiscountAmt",
        parent=styles["Normal"],
        textColor=colors.HexColor(red_hex),
        fontSize=10,
        alignment=TA_RIGHT,
    )
    inv_summary_amt_style = ParagraphStyle(
        name="InvoicePdfSummaryAmt",
        parent=styles["Normal"],
        fontSize=10,
        alignment=TA_RIGHT,
    )
    grand_label_style = ParagraphStyle(
        name="InvoicePdfGrandLbl",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        alignment=TA_LEFT,
    )
    grand_amt_style = ParagraphStyle(
        name="InvoicePdfGrandAmt",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        alignment=TA_RIGHT,
    )

    summary_rows: list[list] = [
        ["Subtotal", Paragraph(escape(f"R{int(fin['subtotal'])}"), inv_summary_amt_style)],
    ]
    if int(fin["discount_amount"] or 0) > 0:
        dp = fin["discount_percent"]
        summary_rows.append(
            [
                Paragraph(escape(f"Discount ({dp:g}%)"), discount_label_style),
                Paragraph(escape(f"R{int(fin['discount_amount'])}"), discount_amt_style),
            ]
        )
    if fin.get("vat_enabled") and int(fin.get("vat_amount") or 0) > 0:
        vp = fin["vat_percent"]
        summary_rows.append(
            [
                Paragraph(escape(f"VAT ({vp:g}%)"), styles["Normal"]),
                Paragraph(escape(f"R{int(fin['vat_amount'])}"), inv_summary_amt_style),
            ]
        )
    summary_rows.append(
        [
            Paragraph("Grand total", grand_label_style),
            Paragraph(escape(f"R{int(fin['grand_total'])}"), grand_amt_style),
        ]
    )
    summary_rows.append(["Amount paid", Paragraph(escape(f"R{amount_paid}"), inv_summary_amt_style)])
    summary_rows.append(["Remaining balance", Paragraph(escape(f"R{remaining}"), inv_summary_amt_style)])

    inv_line_items_width = sum(inv_col_widths)
    totals_amt_col_w = 52 * mm
    totals_label_col_w = inv_line_items_width - totals_amt_col_w
    summary_tbl = Table(summary_rows, colWidths=[totals_label_col_w, totals_amt_col_w])
    summary_tbl.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEABOVE", (0, 0), (-1, 0), 0.75, colors.HexColor("#c2c8d3")),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, grid),
            ]
        )
    )
    story.append(summary_tbl)
    story.append(Spacer(1, 16))
    inv_terms = ""
    if quote_row:
        inv_terms = str(dict(quote_row).get("quote_terms") or "").strip()
    for flow in pdf_terms_flowables(inv_terms, styles):
        story.append(flow)
    for flow in pdf_banking_detail_flowables(settings, styles):
        story.append(flow)
    story.append(Spacer(1, 8))

    ps = (payment_status or "unpaid").lower()
    if ps == "paid":
        status_label, color_hex = "PAID", "#166534"
    elif ps == "partial":
        status_label, color_hex = "PARTIAL", "#c2410c"
    else:
        status_label, color_hex = "UNPAID", "#b91c1c"
    story.append(
        Paragraph(
            f'<para align="center"><b><font size="16" color="{color_hex}">Payment status: {status_label}</font></b></para>',
            styles["Normal"],
        )
    )

    doc.build(story)
    return buf.getvalue()


def client_contact_from_directory(company_id: int, client_name: str | None) -> str:
    """Best-effort client contact from clients table; empty if not found or no extra columns."""
    name = (client_name or "").strip()
    if not name:
        return ""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM clients WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
        (company_id, name),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return ""
    d = dict(row)
    parts: list[str] = []
    for key in ("address", "email", "phone", "mobile", "contact_name"):
        v = d.get(key)
        if v is not None and str(v).strip():
            parts.append(str(v).strip())
    return " · ".join(parts)


def hash_password(password: str, salt: str) -> str:
    if salt == "bcrypt":
        import bcrypt

        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    if salt == "bcrypt":
        import bcrypt

        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    return hash_password(password, salt) == stored_hash


def sendgrid_configured() -> bool:
    return bool(os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDER_EMAIL"))


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    if not sendgrid_configured():
        return
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        body = (
            "A password reset was requested for your GearGrid account.\n\n"
            "If you did not request this, you can safely ignore this email.\n\n"
            f"Reset your password using this link (valid for 1 hour):\n{reset_link}\n"
        )
        message = Mail(
            from_email=os.environ.get("SENDER_EMAIL"),
            to_emails=to_email,
            subject="Password Reset Request - GearGrid",
            plain_text_content=body,
        )
        SendGridAPIClient(os.environ.get("SENDGRID_API_KEY")).send(message)
    except Exception:
        logger.exception("Failed to send password reset email to %s", to_email)


def fetch_reset_token_row(cursor: sqlite3.Cursor, token: str) -> sqlite3.Row | None:
    cursor.execute(
        "SELECT id, user_id, expires_at, used FROM password_reset_tokens WHERE token=?",
        (token,),
    )
    return cursor.fetchone()


def reset_token_error_message(row: sqlite3.Row | None) -> str | None:
    if not row:
        return "This reset link is invalid or has expired. Please request a new one."
    if row["used"]:
        return "This reset link has already been used. Please request a new one."
    if datetime.utcnow() >= datetime.fromisoformat(row["expires_at"]):
        return "This reset link has expired. Please request a new one."
    return None


def create_session(response: RedirectResponse, user_id: int) -> None:
    conn = get_db()
    cursor = conn.cursor()
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    expires_at = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    cursor.execute("INSERT INTO sessions (token, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)", (token, user_id, csrf_token, expires_at))
    conn.commit()
    conn.close()
    response.set_cookie("session_token", token, httponly=True, samesite="lax", secure=SESSION_SECURE_COOKIE, max_age=SESSION_DAYS * 24 * 3600)


def current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id as user_id, u.full_name, u.email, u.company_id, u.role, u.must_change_password, c.name as company_record_name, cs.company_name, s.csrf_token
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        JOIN companies c ON c.id = u.company_id
        LEFT JOIN company_settings cs ON cs.company_id = u.company_id
        WHERE s.token=?
        """,
        (token,),
    )
    user = cursor.fetchone()
    if not user:
        conn.close()
        return None
    cursor.execute("SELECT expires_at FROM sessions WHERE token=?", (token,))
    expiry = cursor.fetchone()
    if not expiry or datetime.fromisoformat(expiry["expires_at"]) < datetime.utcnow():
        cursor.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
        return None
    user_dict = dict(user)
    if not user_dict.get("csrf_token"):
        new_csrf = secrets.token_urlsafe(24)
        cursor.execute("UPDATE sessions SET csrf_token=? WHERE token=?", (new_csrf, token))
        conn.commit()
        user_dict["csrf_token"] = new_csrf
    try:
        cid_nav = int(user_dict["company_id"])
        user_dict["has_company_logo"] = company_static_logo_path(cid_nav).is_file()
    except (TypeError, ValueError, KeyError):
        user_dict["has_company_logo"] = False
    conn.close()
    return user_dict


def render_not_authorized(request: Request, user: dict | None):
    return templates.TemplateResponse(
        request=request,
        name="not_authorized.html",
        context={"title": "Not Authorized", "current_user": user},
        status_code=403,
    )


def get_current_user(request: Request, allowed_roles: set[str] | None = None, allow_password_change_page: bool = False):
    user = current_user(request)
    if not user:
        return None, RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_password") and not allow_password_change_page:
        return None, RedirectResponse(url="/change-password", status_code=303)
    if allowed_roles and user["role"] not in allowed_roles:
        return None, render_not_authorized(request, user)
    return user, None


async def validate_csrf(request: Request, user: dict) -> bool:
    form = await request.form()
    form_token = str(form.get("csrf_token", ""))
    return bool(user.get("csrf_token")) and secrets.compare_digest(form_token, user["csrf_token"])


def get_company_settings(company_id: int) -> dict:
    """Merged company profile: company_settings (quote footer) overlaid by companies table fields."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM companies WHERE id=?", (company_id,))
    co = cursor.fetchone()
    cursor.execute("SELECT * FROM company_settings WHERE company_id=?", (company_id,))
    cs = cursor.fetchone()
    conn.close()
    out: dict = {"company_id": company_id}
    if cs:
        csd = dict(cs)
        out["quote_footer"] = csd.get("quote_footer") or ""
        out["company_logo_path"] = csd.get("company_logo_path")
        out["company_name"] = (csd.get("company_name") or "").strip()
        out["tagline"] = csd.get("tagline") or ""
        out["email"] = csd.get("email") or ""
        out["phone"] = csd.get("phone") or ""
        out["address"] = csd.get("address") or ""
        out["vat_number"] = csd.get("vat_number") or ""
    else:
        out.setdefault("quote_footer", "")
        out["company_name"] = ""
        out["tagline"] = ""
        out["email"] = ""
        out["phone"] = ""
        out["address"] = ""
        out["vat_number"] = ""
    if co:
        cod = dict(co)
        nm = (cod.get("name") or "").strip()
        if nm:
            out["company_name"] = nm
        for fld in ("tagline", "email", "phone", "address", "vat_number"):
            v = cod.get(fld)
            if v is not None and str(v).strip() != "":
                out[fld] = str(v).strip()
        try:
            out["vat_percent"] = float(cod.get("vat_percent") if cod.get("vat_percent") is not None else 15)
        except (TypeError, ValueError):
            out["vat_percent"] = 15.0
        out["vat_enabled"] = int(cod.get("vat_enabled") or 0)
        try:
            out["default_discount_percent"] = float(cod.get("default_discount_percent") if cod.get("default_discount_percent") is not None else 0)
        except (TypeError, ValueError):
            out["default_discount_percent"] = 0.0
        for fld in (
            "bank_name",
            "bank_account_holder",
            "bank_account_number",
            "bank_account_type",
            "bank_branch_code",
            "bank_reference",
            "terms_and_conditions",
        ):
            out[fld] = (cod.get(fld) or "").strip() if cod.get(fld) is not None else ""
    out.setdefault("quote_footer", "")
    out.setdefault("vat_percent", 15.0)
    out.setdefault("vat_enabled", 0)
    out.setdefault("default_discount_percent", 0.0)
    for fld in (
        "bank_name",
        "bank_account_holder",
        "bank_account_number",
        "bank_account_type",
        "bank_branch_code",
        "bank_reference",
        "terms_and_conditions",
    ):
        out.setdefault(fld, "")
    return out


def company_banking_configured(settings: dict) -> bool:
    return bool((settings.get("bank_name") or "").strip() and (settings.get("bank_account_number") or "").strip())


def pdf_banking_detail_flowables(settings: dict, styles) -> list:
    """Banking block for quote/invoice PDFs when bank name and account number are set."""
    if not company_banking_configured(settings):
        return []
    parts = [
        "<b>Banking Details</b>",
        f"Bank: {escape(str(settings['bank_name']).strip())}",
        f"Account Holder: {escape(str(settings.get('bank_account_holder') or '').strip())}",
        f"Account Number: {escape(str(settings['bank_account_number']).strip())}",
        f"Account Type: {escape(str(settings.get('bank_account_type') or '').strip())}",
        f"Branch Code: {escape(str(settings.get('bank_branch_code') or '').strip())}",
    ]
    ref = str(settings.get("bank_reference") or "").strip()
    if ref:
        parts.append(f"Reference: {escape(ref)}")
    return [Spacer(1, 10), Paragraph("<br/>".join(parts), styles["Normal"])]


def client_row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    return {
        "id": d.get("id"),
        "name": (d.get("name") or "").strip(),
        "contact_person": (d.get("contact_person") or "").strip(),
        "phone": (d.get("phone") or "").strip(),
        "email": (d.get("email") or "").strip(),
        "address": (d.get("address") or "").strip(),
        "vat_number": (d.get("vat_number") or "").strip(),
    }


def client_profile_by_name(company_id: int, client_name: str | None) -> dict | None:
    name = (client_name or "").strip()
    if not name:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM clients WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
        (company_id, name),
    )
    row = cursor.fetchone()
    conn.close()
    return client_row_to_dict(row)


def client_profile_by_id(company_id: int, client_id: int) -> dict | None:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients WHERE id=? AND company_id=?", (client_id, company_id))
    row = cursor.fetchone()
    conn.close()
    return client_row_to_dict(row)


def pdf_client_details_flowables(client_name: str, profile: dict | None, styles) -> list:
    parts = ["<b>Client</b>", f"<b>{escape(str(client_name or '').strip() or '—')}</b>"]
    if profile:
        for label, key in (
            ("Contact Person", "contact_person"),
            ("Phone", "phone"),
            ("Email", "email"),
            ("Address", "address"),
            ("VAT Number", "vat_number"),
        ):
            val = str(profile.get(key) or "").strip()
            if val:
                parts.append(f"{label}: {escape(val)}")
    return [Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 8)]


def pdf_job_details_flowables(
    job_name: str | None,
    site_location: str | None,
    start_date: str | None,
    end_date: str | None,
    special_notes: str | None,
    styles,
) -> list:
    jn = str(job_name or "").strip()
    sl = str(site_location or "").strip()
    sd = str(start_date or "").strip()
    ed = str(end_date or "").strip()
    sn = str(special_notes or "").strip()
    if not any([jn, sl, sd, ed, sn]):
        return []
    parts = ["<b>Job Details</b>"]
    if jn:
        parts.append(f"Job Name: {escape(jn)}")
    if sl:
        parts.append(f"Site / Location: {escape(sl)}")
    if sd:
        parts.append(f"Start Date: {escape(sd)}")
    if ed:
        parts.append(f"End Date: {escape(ed)}")
    if sn:
        parts.append(f"Notes: {escape(sn)}")
    return [Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 8)]


def pdf_terms_flowables(terms: str | None, styles) -> list:
    text = str(terms or "").strip()
    if not text:
        return []
    body = escape(text).replace("\n", "<br/>")
    return [Spacer(1, 8), Paragraph(f"<b>Terms and Conditions</b><br/>{body}", styles["Normal"])]


def quote_meta_from_row(qdict: dict | None, company_id: int, client_name: str) -> dict:
    qd = qdict or {}
    return {
        "job_name": qd.get("job_name"),
        "site_location": qd.get("site_location"),
        "start_date": qd.get("start_date"),
        "end_date": qd.get("end_date"),
        "special_notes": qd.get("special_notes"),
        "quote_terms": qd.get("quote_terms"),
        "client_profile": client_profile_by_name(company_id, client_name),
    }


def company_static_logo_path(company_id: int) -> Path:
    return BASE_DIR / "static" / "logos" / f"company_{company_id}.png"


def quote_financials_from_saved_row(qdict: dict, lines: list[dict]) -> dict[str, int | float | bool]:
    if qdict.get("subtotal") is not None:
        return {
            "subtotal": int(qdict["subtotal"]),
            "discount_percent": float(qdict.get("discount_percent") or 0),
            "discount_amount": int(qdict.get("discount_amount") or 0),
            "vat_enabled": bool(int(qdict.get("vat_enabled") or 0)),
            "vat_percent": float(qdict.get("vat_percent") or 15),
            "vat_amount": int(qdict.get("vat_amount") or 0),
            "grand_total": int(qdict.get("grand_total") or qdict.get("total") or 0),
        }
    st = invoice_line_subtotal(lines)
    return compute_financial_totals(st, 0.0, False, 15.0)
    """Whole-currency totals: discount and VAT applied server-side."""
    st = max(0, int(subtotal))
    dp = max(0.0, min(100.0, float(discount_percent)))
    discount_amount = int(round(st * dp / 100.0))
    after_discount = max(0, st - discount_amount)
    vp = max(0.0, float(vat_percent))
    vat_amount = int(round(after_discount * vp / 100.0)) if vat_enabled else 0
    grand_total = after_discount + vat_amount
    return {
        "subtotal": st,
        "discount_percent": dp,
        "discount_amount": discount_amount,
        "vat_enabled": bool(vat_enabled),
        "vat_percent": vp,
        "vat_amount": vat_amount,
        "grand_total": grand_total,
    }


def invoice_financials_from_row(inv: dict, line_items: list[dict]) -> dict[str, int | float | bool]:
    """Totals for display/PDF; backfills from line items for legacy invoices."""
    sub = inv.get("subtotal")
    if sub is None:
        sub = invoice_line_subtotal(line_items)
    sub = int(sub or 0)
    d_pct = float(inv.get("discount_percent") or 0)
    d_amt = inv.get("discount_amount")
    if d_amt is None:
        d_amt = int(round(sub * max(0.0, min(100.0, d_pct)) / 100.0)) if d_pct else 0
    d_amt = int(d_amt or 0)
    v_en = bool(int(inv.get("vat_enabled") or 0))
    v_pct = float(inv.get("vat_percent") if inv.get("vat_percent") is not None else 15)
    v_amt = inv.get("vat_amount")
    if v_amt is None:
        after = max(0, sub - d_amt)
        v_amt = int(round(after * v_pct / 100.0)) if v_en else 0
    v_amt = int(v_amt or 0)
    grand = inv.get("grand_total")
    if grand is None:
        grand = int(inv.get("total") or 0)
    grand = int(grand or 0)
    return {
        "subtotal": sub,
        "discount_percent": d_pct,
        "discount_amount": d_amt,
        "vat_enabled": v_en,
        "vat_percent": v_pct,
        "vat_amount": v_amt,
        "grand_total": grand,
    }


def pdf_company_logo_flowable(company_id: int) -> RLImage | None:
    path = company_static_logo_path(company_id)
    if not path.is_file():
        return None
    try:
        ir = ImageReader(str(path))
        iw, ih = ir.getSize()
        if iw <= 0 or ih <= 0:
            return None
        max_h_pt = 80.0
        max_w_pt = 160.0
        scale = min(max_w_pt / float(iw), max_h_pt / float(ih), 1.0)
        dw = iw * scale
        dh = ih * scale
        return RLImage(str(path), width=dw, height=dh)
    except Exception:
        logger.exception("Could not load company logo for PDF company_id=%s", company_id)
        return None


def save_company_logo_upload(company_id: int, upload: UploadFile | None) -> str | None:
    """Save uploaded logo as static/logos/company_{id}.png. Returns error message or None."""
    if upload is None or not getattr(upload, "filename", None):
        return None
    fn = str(upload.filename or "")
    if not fn.strip():
        return None
    ct = (upload.content_type or "").lower()
    allowed_ct = ("image/jpeg", "image/jpg", "image/png", "image/pjpeg", "image/x-png", "application/octet-stream")
    if ct and ct not in allowed_ct:
        return "Logo must be a JPG or PNG image"
    raw = upload.file.read()
    if len(raw) > 5 * 1024 * 1024:
        return "Logo file is too large"
    (BASE_DIR / "static" / "logos").mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        img = Image.open(BytesIO(raw))
        img = img.convert("RGBA")
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img.thumbnail((300, 300), resample)
        dest = company_static_logo_path(company_id)
        img.save(dest, "PNG")
    except Exception:
        logger.exception("Logo save failed company_id=%s", company_id)
        return "Could not process logo image"
    return None


def render_message(request: Request, title: str, message: str, back_url: str, user: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="message.html",
        context={"title": title, "message": message, "back_url": back_url, "current_user": user},
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={"title": "Register Company"})


@app.post("/register")
def register_company(company_name: str = Form(...), full_name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    clean_company = company_name.strip()
    clean_full_name = full_name.strip()
    clean_email = email.strip().lower()
    if not clean_company or not clean_full_name or not clean_email or len(password) < 6:
        return RedirectResponse(url="/register?error=Please%20fill%20all%20fields%20and%20use%20a%206%2B%20char%20password", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO companies (name, created_at) VALUES (?, ?)", (clean_company, datetime.utcnow().isoformat()))
        company_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO users (company_id, full_name, email, password_hash, password_salt, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (company_id, clean_full_name, clean_email, hash_password(password, "bcrypt"), "bcrypt", "admin", datetime.utcnow().isoformat()),
        )
        cursor.execute(
            """
            INSERT INTO company_settings
            (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_id, clean_company, "Professional Equipment Rentals", clean_email, "", "", "", "Thank you for your business."),
        )
        cursor.execute(
            """
            UPDATE companies
            SET tagline=?, email=?, vat_percent=15, vat_enabled=0, default_discount_percent=0
            WHERE id=?
            """,
            ("Professional Equipment Rentals", clean_email, company_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return RedirectResponse(url="/register?error=Company%20or%20email%20already%20exists", status_code=303)
    cursor.execute("SELECT id FROM users WHERE email=?", (clean_email,))
    user = cursor.fetchone()
    conn.close()
    response = RedirectResponse(url="/", status_code=303)
    create_session(response, user["id"])
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"title": "Login"})


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"title": "Forgot Password"})


@app.post("/forgot-password")
def forgot_password_submit(email: str = Form(...)):
    clean_email = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, company_id, email FROM users WHERE email=?", (clean_email,))
    user = cursor.fetchone()
    if user:
        cursor.execute("SELECT id FROM password_reset_requests WHERE user_id=?", (user["id"],))
        existing = cursor.fetchone()
        if not existing:
            cursor.execute(
                "INSERT INTO password_reset_requests (user_id, company_id, requested_at) VALUES (?, ?, ?)",
                (user["id"], user["company_id"], datetime.utcnow().isoformat()),
            )
        if sendgrid_configured():
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            cursor.execute(
                "INSERT INTO password_reset_tokens (user_id, token, expires_at, used) VALUES (?, ?, ?, 0)",
                (user["id"], token, expires_at),
            )
            send_password_reset_email(user["email"], f"{BASE_URL}/reset-password/{token}")
        conn.commit()
    conn.close()
    return RedirectResponse(url="/forgot-password?submitted=1", status_code=303)


@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str):
    conn = get_db()
    cursor = conn.cursor()
    row = fetch_reset_token_row(cursor, token)
    conn.close()
    error = reset_token_error_message(row)
    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={"title": "Reset Password", "error": error, "token": None if error else token},
    )


@app.post("/reset-password/{token}")
def reset_password_submit(request: Request, token: str, password: str = Form(...), confirm_password: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    row = fetch_reset_token_row(cursor, token)
    error = reset_token_error_message(row)
    if error:
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"title": "Reset Password", "error": error, "token": None},
        )
    if len(password) < 8:
        conn.close()
        return RedirectResponse(url=f"/reset-password/{token}?error=Password%20must%20be%20at%20least%208%20characters", status_code=303)
    if password != confirm_password:
        conn.close()
        return RedirectResponse(url=f"/reset-password/{token}?error=Passwords%20do%20not%20match", status_code=303)
    cursor.execute(
        "UPDATE users SET password_hash=?, password_salt='bcrypt', must_change_password=0 WHERE id=?",
        (hash_password(password, "bcrypt"), row["user_id"]),
    )
    cursor.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))
    cursor.execute("DELETE FROM password_reset_requests WHERE user_id=?", (row["user_id"],))
    conn.commit()
    conn.close()
    return RedirectResponse(
        url="/login?success=Password%20reset%20successfully.%20Please%20log%20in.",
        status_code=303,
    )


@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    clean_email = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email=?", (clean_email,))
    user = cursor.fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
        return RedirectResponse(url="/login?error=Invalid%20credentials", status_code=303)
    if user["password_salt"] != "bcrypt":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (hash_password(password, "bcrypt"), "bcrypt", user["id"]))
        conn.commit()
        conn.close()
    response = RedirectResponse(url="/", status_code=303)
    create_session(response, user["id"])
    return response


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management", "warehouse"}, allow_password_change_page=True)
    if response:
        return response
    return templates.TemplateResponse(request=request, name="change_password.html", context={"title": "Change Password", "current_user": user})


@app.post("/change-password")
async def change_password_submit(request: Request, password: str = Form(...), confirm_password: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin", "management", "warehouse"}, allow_password_change_page=True)
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/change-password", user)
    if len(password) < 6:
        return RedirectResponse(url="/change-password?error=Password%20must%20be%20at%20least%206%20characters", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url="/change-password?error=Passwords%20do%20not%20match", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash=?, password_salt='bcrypt', must_change_password=0 WHERE id=?",
        (hash_password(password, "bcrypt"), user["user_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    user = current_user(request)
    if user and not await validate_csrf(request, user):
        return RedirectResponse(url="/?error=Invalid%20security%20token", status_code=303)
    token = request.cookies.get("session_token")
    if token:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management", "warehouse"})
    if response:
        return response
    if user["role"] == "warehouse":
        return RedirectResponse(url="/warehouse", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM equipment WHERE company_id=? ORDER BY id DESC", (user["company_id"],))
    all_items = cursor.fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    total_count = sum(qty_total(r) for r in all_items)
    available_count = sum(qty_available(r) for r in all_items)
    rented_count = sum(qty_rented(r) for r in all_items)
    overdue_count = sum(
        (qty_rented(r) if (r["due_date"] and r["due_date"] < today and qty_rented(r) > 0) else 0) for r in all_items
    )
    stock_filter = request.query_params.get("stock", "all")
    if stock_filter == "available":
        items = [r for r in all_items if qty_available(r) > 0]
    elif stock_filter == "rented":
        items = [r for r in all_items if qty_rented(r) > 0]
    elif stock_filter == "overdue":
        items = [r for r in all_items if qty_rented(r) > 0 and r["due_date"] and r["due_date"] < today]
    else:
        items = list(all_items)
    cursor.execute(
        "SELECT * FROM rental_history WHERE company_id=? ORDER BY id DESC",
        (user["company_id"],),
    )
    history_rows = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        "SELECT * FROM sub_rentals WHERE company_id=? ORDER BY id DESC",
        (user["company_id"],),
    )
    sub_rental_rows = cursor.fetchall()
    conn.close()
    settings = get_company_settings(user["company_id"])
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "title": f"{settings.get('company_name', 'Dashboard')} Dashboard",
            "items": items,
            "today": today,
            "current_user": user,
            "stock_filter": stock_filter,
            "total_count": total_count,
            "available_count": available_count,
            "rented_count": rented_count,
            "overdue_count": overdue_count,
            "history_rows": history_rows,
            "sub_rentals": sub_rental_rows,
        },
    )


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, full_name, email, role, created_at FROM users WHERE company_id=? ORDER BY id DESC", (user["company_id"],))
    users = cursor.fetchall()
    cursor.execute(
        """
        SELECT pr.id, pr.user_id, pr.requested_at, u.full_name, u.email
        FROM password_reset_requests pr
        JOIN users u ON u.id = pr.user_id
        WHERE pr.company_id=?
        ORDER BY pr.requested_at DESC
        """,
        (user["company_id"],),
    )
    reset_requests = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "title": "User Management",
            "users": users,
            "roles": sorted(VALID_ROLES),
            "reset_requests": reset_requests,
            "temp_password": request.query_params.get("temp_password", ""),
            "current_user": user,
        },
    )


@app.post("/users")
async def users_add(request: Request, full_name: str = Form(...), email: str = Form(...), password: str = Form(...), role: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/users", user)
    clean_name = full_name.strip()
    clean_email = email.strip().lower()
    if role not in VALID_ROLES:
        return RedirectResponse(url="/users?error=Invalid%20role", status_code=303)
    if not clean_name or not clean_email or len(password) < 6:
        return RedirectResponse(url="/users?error=Invalid%20input", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO users (company_id, full_name, email, password_hash, password_salt, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["company_id"], clean_name, clean_email, hash_password(password, "bcrypt"), "bcrypt", role, datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return RedirectResponse(url="/users?error=Email%20already%20exists", status_code=303)
    conn.close()
    return RedirectResponse(url="/users?saved=1", status_code=303)


@app.post("/users/{user_id}/role")
async def users_set_role(request: Request, user_id: int, role: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/users", user)
    if role not in VALID_ROLES:
        return RedirectResponse(url="/users?error=Invalid%20role", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET role=? WHERE id=? AND company_id=?", (role, user_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/users?saved=1", status_code=303)


@app.post("/users/{user_id}/delete")
async def users_delete(request: Request, user_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/users", user)
    if user["user_id"] == user_id:
        return RedirectResponse(url="/users?error=You%20cannot%20delete%20yourself", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE id=? AND company_id=?)", (user_id, user["company_id"]))
    cursor.execute("DELETE FROM users WHERE id=? AND company_id=?", (user_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/users?saved=1", status_code=303)


@app.post("/users/{user_id}/reset-password")
async def users_reset_password(request: Request, user_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/users", user)

    temp_password = secrets.token_urlsafe(8)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id=? AND company_id=?", (user_id, user["company_id"]))
    target = cursor.fetchone()
    if not target:
        conn.close()
        return RedirectResponse(url="/users?error=User%20not%20found", status_code=303)

    cursor.execute(
        "UPDATE users SET password_hash=?, password_salt='bcrypt', must_change_password=1 WHERE id=? AND company_id=?",
        (hash_password(temp_password, "bcrypt"), user_id, user["company_id"]),
    )
    cursor.execute("DELETE FROM password_reset_requests WHERE user_id=? AND company_id=?", (user_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/users?saved=1&temp_password={temp_password}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    settings = get_company_settings(user["company_id"])
    has_logo = company_static_logo_path(user["company_id"]).is_file()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "Company Settings",
            "settings": settings,
            "has_logo": has_logo,
            "company_id": user["company_id"],
            "current_user": user,
        },
    )


@app.post("/settings")
async def update_settings(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    form = await request.form(max_part_size=5 * 1024 * 1024)
    company_name = str(form.get("company_name", "")).strip()
    tagline = str(form.get("tagline", "")).strip()
    email = str(form.get("email", "")).strip()
    phone = str(form.get("phone", "")).strip()
    address = str(form.get("address", "")).strip()
    vat_number = str(form.get("vat_number", "")).strip()
    quote_footer = str(form.get("quote_footer", "")).strip()
    try:
        vat_percent = float(str(form.get("vat_percent", "15")).strip() or 15)
    except ValueError:
        vat_percent = 15.0
    vat_percent = max(0.0, min(100.0, vat_percent))
    vat_enabled = 1 if str(form.get("vat_enabled", "")).strip() in ("1", "on", "yes", "true", "True") else 0
    try:
        default_discount_percent = float(str(form.get("default_discount_percent", "0")).strip() or 0)
    except ValueError:
        default_discount_percent = 0.0
    default_discount_percent = max(0.0, min(100.0, default_discount_percent))
    bank_name = str(form.get("bank_name", "")).strip()
    bank_account_holder = str(form.get("bank_account_holder", "")).strip()
    bank_account_number = str(form.get("bank_account_number", "")).strip()
    bank_account_type = str(form.get("bank_account_type", "")).strip()
    bank_branch_code = str(form.get("bank_branch_code", "")).strip()
    bank_reference = str(form.get("bank_reference", "")).strip()
    terms_and_conditions = str(form.get("terms_and_conditions", "")).strip()
    if not company_name:
        return RedirectResponse(url="/settings?error=Company%20name%20is%20required", status_code=303)
    logo_upload = form.get("logo")
    if logo_upload is not None and hasattr(logo_upload, "filename") and getattr(logo_upload, "filename", None):
        err = save_company_logo_upload(user["company_id"], logo_upload)  # type: ignore[arg-type]
        if err:
            return RedirectResponse(url=f"/settings?{urlencode({'error': err})}", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE companies
        SET name=?, tagline=?, email=?, phone=?, address=?, vat_number=?, vat_percent=?, vat_enabled=?, default_discount_percent=?,
            bank_name=?, bank_account_holder=?, bank_account_number=?, bank_account_type=?, bank_branch_code=?, bank_reference=?,
            terms_and_conditions=?
        WHERE id=?
        """,
        (
            company_name,
            tagline,
            email,
            phone,
            address,
            vat_number,
            vat_percent,
            vat_enabled,
            default_discount_percent,
            bank_name,
            bank_account_holder,
            bank_account_number,
            bank_account_type,
            bank_branch_code,
            bank_reference,
            terms_and_conditions,
            user["company_id"],
        ),
    )
    cursor.execute(
        """
        UPDATE company_settings
        SET company_name=?, tagline=?, email=?, phone=?, address=?, vat_number=?, quote_footer=?
        WHERE company_id=?
        """,
        (company_name, tagline, email, phone, address, vat_number, quote_footer, user["company_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    return templates.TemplateResponse(request=request, name="billing.html", context={"title": "Billing", "current_user": user})


@app.get("/quotes/dashboard", response_class=HTMLResponse)
def quote_dashboard(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, quote_number, client_name, quote_date, total, status, created_at
        FROM quotes
        WHERE company_id=?
        ORDER BY id DESC
        """,
        (user["company_id"],),
    )
    quotes = cursor.fetchall()
    cursor.execute(
        """
        SELECT j.id, j.client_name, j.job_date, j.status, j.quote_id, q.quote_number
        FROM jobs j
        LEFT JOIN quotes q ON q.id = j.quote_id
        WHERE j.company_id=?
        ORDER BY j.id DESC
        """,
        (user["company_id"],),
    )
    jobs = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) AS c FROM quotes WHERE company_id=?", (user["company_id"],))
    quote_total = int(cursor.fetchone()["c"] or 0)
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END), 0) AS a,
            COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS p,
            COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END), 0) AS r
        FROM quotes
        WHERE company_id=?
        """,
        (user["company_id"],),
    )
    st = cursor.fetchone()
    approved_count = int(st["a"] or 0)
    pending_count = int(st["p"] or 0)
    rejected_count = int(st["r"] or 0)
    conversion_pct = round((approved_count / quote_total) * 100, 1) if quote_total else 0.0
    cursor.execute(
        "SELECT COALESCE(SUM(total), 0) AS t FROM quotes WHERE company_id=? AND status='approved'",
        (user["company_id"],),
    )
    approved_value = int(cursor.fetchone()["t"] or 0)
    if has_column(cursor, "invoices", "amount_paid"):
        cursor.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN payment_status IN ('unpaid', 'partial')
                THEN (total - COALESCE(amount_paid, 0)) ELSE 0 END), 0) AS o
            FROM invoices WHERE company_id=?
            """,
            (user["company_id"],),
        )
        outstanding_invoices = int(cursor.fetchone()["o"] or 0)
    else:
        outstanding_invoices = 0
    conn.close()
    err = request.query_params.get("error", "")
    return templates.TemplateResponse(
        request=request,
        name="quote_dashboard.html",
        context={
            "title": "Quote Dashboard",
            "quotes": quotes,
            "jobs": jobs,
            "current_user": user,
            "error": err,
            "quote_total": quote_total,
            "approved_count": approved_count,
            "pending_count": pending_count,
            "rejected_count": rejected_count,
            "conversion_pct": conversion_pct,
            "approved_value": approved_value,
            "outstanding_invoices": outstanding_invoices,
        },
    )


@app.get("/quotes/{quote_id}/pdf")
def quote_saved_pdf(request: Request, quote_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    settings = get_company_settings(user["company_id"])
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM quotes WHERE id=? AND company_id=?", (quote_id, user["company_id"]))
    qrow = cursor.fetchone()
    conn.close()
    if not qrow:
        return render_message(request, "Quote Error", "Quote not found.", "/quotes/dashboard", user)
    qdict = dict(qrow)
    try:
        lines = json.loads(qdict["line_items_json"])
    except json.JSONDecodeError:
        lines = []
    if not isinstance(lines, list):
        lines = []
    client = qdict["client_name"]
    date = qdict["quote_date"]
    qnum = qdict["quote_number"]
    fin = quote_financials_from_saved_row(qdict, lines)
    meta = quote_meta_from_row(qdict, user["company_id"], client)
    return quote_pdf_file_response(request, user, settings, client, date, qnum, lines, fin, "/quotes/dashboard", quote_meta=meta)


@app.post("/quotes/{quote_id}/approve")
async def quote_approve(request: Request, quote_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/quotes/dashboard", user)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT * FROM quotes WHERE id=? AND company_id=?",
            (quote_id, user["company_id"]),
        )
        quote_row = cursor.fetchone()
        if not quote_row or quote_row["status"] != "pending":
            conn.rollback()
            conn.close()
            return RedirectResponse(url="/quotes/dashboard?error=Quote%20not%20found%20or%20already%20processed", status_code=303)
        try:
            lines = json.loads(quote_row["line_items_json"])
        except json.JSONDecodeError:
            lines = []
        ok, err = reserve_stock_for_quote_lines(cursor, user["company_id"], lines)
        if not ok:
            conn.rollback()
            conn.close()
            qe = urlencode({"error": err or "Could not reserve stock"})
            return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
        ok2, err2 = reserve_sub_rental_stock_for_quote_lines(cursor, user["company_id"], lines)
        if not ok2:
            conn.rollback()
            conn.close()
            qe = urlencode({"error": err2 or "Could not reserve sub-rental stock"})
            return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
        inv_num = next_invoice_number(cursor, user["company_id"])
        due_date = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
        created = datetime.utcnow().isoformat()
        fin_inv = quote_financials_from_saved_row(dict(quote_row), lines)
        cursor.execute(
            """
            INSERT INTO invoices (
                company_id, invoice_number, quote_id, client_name, line_items_json, total, created_at, due_date, payment_status, amount_paid,
                subtotal, discount_percent, discount_amount, vat_enabled, vat_percent, vat_amount, grand_total
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["company_id"],
                inv_num,
                quote_id,
                quote_row["client_name"],
                quote_row["line_items_json"],
                int(fin_inv["grand_total"]),
                created,
                due_date,
                int(fin_inv["subtotal"]),
                float(fin_inv["discount_percent"]),
                int(fin_inv["discount_amount"]),
                1 if fin_inv["vat_enabled"] else 0,
                float(fin_inv["vat_percent"]),
                int(fin_inv["vat_amount"]),
                int(fin_inv["grand_total"]),
            ),
        )
        invoice_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO jobs (company_id, quote_id, invoice_id, client_name, job_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'upcoming', ?)
            """,
            (user["company_id"], quote_id, invoice_id, quote_row["client_name"], quote_row["quote_date"], created),
        )
        job_id = cursor.lastrowid
        log_job_status_change(cursor, user["company_id"], job_id, None, "upcoming", user["user_id"])
        for line in lines:
            if quote_line_is_sub_rental(line):
                qty = max(1, int(line.get("qty", 1) or 1))
                try:
                    sid = int(line.get("sub_rental_id"))
                except (TypeError, ValueError):
                    continue
                desc = str(line.get("name", "")).strip()
                supplier = str(line.get("supplier_name", "")).strip()
                show_raw = line.get("show_on_quote", 0)
                show_on = 1 if show_raw in (1, True, "1", "yes", "Yes", "true", "True") else 0
                cursor.execute(
                    """
                    INSERT INTO job_prep_items (company_id, job_id, equipment_id, equipment_name, quantity, packed, line_type, sub_rental_id, supplier_name, received_from_supplier)
                    VALUES (?, ?, NULL, ?, ?, 0, 'sub_rental', ?, ?, 0)
                    """,
                    (user["company_id"], job_id, desc or "Sub-rental", qty, sid, supplier),
                )
                cursor.execute(
                    """
                    INSERT INTO sub_rental_usage (company_id, sub_rental_id, job_id, units_used, show_on_quote)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user["company_id"], sid, job_id, qty, show_on),
                )
                continue
            name = str(line.get("name", "")).strip()
            qty = int(line.get("qty", 1) or 1)
            if not name:
                continue
            eid = line.get("equipment_id")
            if eid:
                try:
                    eid = int(eid)
                except (TypeError, ValueError):
                    eid = None
            if not eid:
                cursor.execute(
                    "SELECT id FROM equipment WHERE company_id=? AND name=? ORDER BY id ASC LIMIT 1",
                    (user["company_id"], name),
                )
                fr = cursor.fetchone()
                eid = int(fr["id"]) if fr else None
            cursor.execute(
                """
                INSERT INTO job_prep_items (company_id, job_id, equipment_id, equipment_name, quantity, packed, line_type, sub_rental_id, supplier_name, received_from_supplier)
                VALUES (?, ?, ?, ?, ?, 0, 'owned', NULL, NULL, 0)
                """,
                (user["company_id"], job_id, eid, name, max(1, qty)),
            )
        cursor.execute("UPDATE quotes SET status='approved' WHERE id=? AND company_id=?", (quote_id, user["company_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url="/quotes/dashboard", status_code=303)


@app.post("/quotes/{quote_id}/reject")
async def quote_reject(request: Request, quote_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/quotes/dashboard", user)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM quotes WHERE id=? AND company_id=? AND status='pending'",
        (quote_id, user["company_id"]),
    )
    if not cursor.fetchone():
        conn.close()
        return RedirectResponse(url="/quotes/dashboard?error=Quote%20not%20found%20or%20already%20processed", status_code=303)
    cursor.execute("UPDATE quotes SET status='rejected' WHERE id=? AND company_id=?", (quote_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/quotes/dashboard", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT j.id, j.client_name, j.job_date, j.status, j.quote_id, q.quote_number
        FROM jobs j
        LEFT JOIN quotes q ON q.id = j.quote_id
        WHERE j.company_id=?
        ORDER BY j.job_date DESC, j.id DESC
        """,
        (user["company_id"],),
    )
    jobs = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "title": "Jobs",
            "jobs": jobs,
            "current_user": user,
            "job_statuses": ["upcoming", "active", "done"],
        },
    )


@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_dashboard(request: Request):
    user, response = get_current_user(request, allowed_roles={"warehouse"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT j.id, j.client_name, j.job_date, j.status,
               (SELECT COUNT(*) FROM job_prep_items jpi WHERE jpi.job_id = j.id AND jpi.company_id = j.company_id) AS item_count
        FROM jobs j
        WHERE j.company_id=? AND j.status IN ('upcoming', 'active')
        ORDER BY j.job_date ASC, j.id ASC
        """,
        (user["company_id"],),
    )
    jobs = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="warehouse.html",
        context={"title": "Warehouse", "jobs": jobs, "current_user": user},
    )


@app.get("/warehouse/job/{job_id}", response_class=HTMLResponse)
def warehouse_job_prep(request: Request, job_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM jobs WHERE id=? AND company_id=?",
        (job_id, user["company_id"]),
    )
    job = cursor.fetchone()
    if not job:
        conn.close()
        return render_message(request, "Not found", "Job not found.", "/warehouse" if user["role"] == "warehouse" else "/jobs", user)
    if user["role"] == "warehouse" and job["status"] not in ("upcoming", "active"):
        conn.close()
        return render_message(request, "Not available", "This job is not available for prep.", "/warehouse", user)
    cursor.execute(
        "SELECT * FROM job_prep_items WHERE job_id=? AND company_id=? ORDER BY id ASC",
        (job_id, user["company_id"]),
    )
    prep_items = cursor.fetchall()
    conn.close()
    back_url = "/warehouse" if user["role"] == "warehouse" else "/jobs"
    return templates.TemplateResponse(
        request=request,
        name="warehouse_job.html",
        context={
            "title": f"Prep — Job #{job_id}",
            "job": job,
            "prep_items": prep_items,
            "current_user": user,
            "back_url": back_url,
        },
    )


@app.post("/warehouse/job/{job_id}/status")
async def warehouse_job_set_status(request: Request, job_id: int, job_status: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"warehouse"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    if job_status not in JOB_STATUSES:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20status", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT status FROM jobs WHERE id=? AND company_id=?", (job_id, user["company_id"]))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return RedirectResponse(url="/warehouse?error=Job%20not%20found", status_code=303)
        old = row["status"]
        if not warehouse_may_set_job_status(old, job_status):
            conn.rollback()
            conn.close()
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20status%20change", status_code=303)
        ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
        if not ok:
            conn.rollback()
            conn.close()
            qe = urlencode({"error": err or "Not enough units available"})
            return RedirectResponse(url=f"/warehouse/job/{job_id}?{qe}", status_code=303)
        cursor.execute("UPDATE jobs SET status=? WHERE id=? AND company_id=?", (job_status, job_id, user["company_id"]))
        log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/status")
async def management_job_set_status(request: Request, job_id: int, job_status: str = Form(...), redirect_to: str = Form(default="/jobs")):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/jobs", user)
    if job_status not in JOB_STATUSES:
        return RedirectResponse(url="/jobs?error=Invalid%20status", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT status FROM jobs WHERE id=? AND company_id=?", (job_id, user["company_id"]))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return RedirectResponse(url="/jobs?error=Job%20not%20found", status_code=303)
        old = row["status"]
        if old == job_status:
            conn.rollback()
            conn.close()
            safe_next = redirect_to if redirect_to.startswith("/") and not redirect_to.startswith("//") else "/jobs"
            return RedirectResponse(url=safe_next, status_code=303)
        ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
        if not ok:
            conn.rollback()
            conn.close()
            qe = urlencode({"error": err or "Not enough units available"})
            return RedirectResponse(url=f"/jobs?{qe}", status_code=303)
        cursor.execute("UPDATE jobs SET status=? WHERE id=? AND company_id=?", (job_status, job_id, user["company_id"]))
        log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    safe_next = redirect_to if redirect_to.startswith("/") and not redirect_to.startswith("//") else "/jobs"
    return RedirectResponse(url=safe_next, status_code=303)


@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/toggle")
async def warehouse_prep_toggle(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT packed FROM job_prep_items WHERE id=? AND job_id=? AND company_id=?",
        (prep_item_id, job_id, user["company_id"]),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
    new_packed = 0 if row["packed"] else 1
    cursor.execute(
        "UPDATE job_prep_items SET packed=? WHERE id=? AND job_id=? AND company_id=?",
        (new_packed, prep_item_id, job_id, user["company_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/received")
async def warehouse_prep_received_toggle(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT received_from_supplier, line_type FROM job_prep_items WHERE id=? AND job_id=? AND company_id=?",
        (prep_item_id, job_id, user["company_id"]),
    )
    row = cursor.fetchone()
    if not row or (row["line_type"] or "owned") != "sub_rental":
        conn.close()
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
    new_val = 0 if int(row["received_from_supplier"] or 0) else 1
    cursor.execute(
        "UPDATE job_prep_items SET received_from_supplier=? WHERE id=? AND job_id=? AND company_id=?",
        (new_val, prep_item_id, job_id, user["company_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/quotes/dashboard/job/{job_id}/status")
async def quote_dashboard_job_status(request: Request, job_id: int, job_status: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/quotes/dashboard", user)
    if job_status not in JOB_STATUSES:
        return RedirectResponse(url="/quotes/dashboard?error=Invalid%20status", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT status FROM jobs WHERE id=? AND company_id=?", (job_id, user["company_id"]))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return RedirectResponse(url="/quotes/dashboard?error=Job%20not%20found", status_code=303)
        old = row["status"]
        if old != job_status:
            ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
            if not ok:
                conn.rollback()
                conn.close()
                qe = urlencode({"error": err or "Not enough units available"})
                return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
            cursor.execute("UPDATE jobs SET status=? WHERE id=? AND company_id=?", (job_status, job_id, user["company_id"]))
            log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url="/quotes/dashboard", status_code=303)


@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    return templates.TemplateResponse(request=request, name="add.html", context={"title": "Add Equipment", "current_user": user})


@app.post("/add")
async def add_equipment(request: Request, name: str = Form(...), price: int = Form(...), quantity: int = Form(default=1)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/add", user)
    clean_name = name.strip()
    if not clean_name:
        return RedirectResponse(url="/add?error=Name%20is%20required", status_code=303)
    if price < 0:
        return RedirectResponse(url="/add?error=Price%20must%20be%200%20or%20more", status_code=303)
    if quantity < 1:
        return RedirectResponse(url="/add?error=Quantity%20must%20be%20at%20least%201", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO equipment (name, status, price, prep_status, company_id, quantity, quantity_rented) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (clean_name, "available", price, "pending", user["company_id"], quantity),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/equipment/{item_id}/edit", response_class=HTMLResponse)
def edit_equipment_page(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
    item = cursor.fetchone()
    conn.close()
    if not item:
        return render_message(request, "Error", "Equipment not found.", "/", user)
    return templates.TemplateResponse(request=request, name="equipment_edit.html", context={"title": "Edit Equipment", "item": item, "current_user": user})


@app.post("/equipment/{item_id}/edit")
async def edit_equipment(request: Request, item_id: int, name: str = Form(...), price: int = Form(...), quantity: int = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/equipment/{item_id}/edit", user)
    clean_name = name.strip()
    if not clean_name:
        return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Name%20is%20required", status_code=303)
    if price < 0:
        return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Price%20must%20be%200%20or%20more", status_code=303)
    if quantity < 1:
        return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Quantity%20must%20be%20at%20least%201", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT quantity_rented FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
    cur = cursor.fetchone()
    if not cur:
        conn.close()
        return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Not%20found", status_code=303)
    qr = int(cur["quantity_rented"] or 0)
    if quantity < qr:
        return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Quantity%20cannot%20be%20less%20than%20rented%20units", status_code=303)
    cursor.execute("UPDATE equipment SET name=?, price=?, quantity=? WHERE id=? AND company_id=?", (clean_name, price, quantity, item_id, user["company_id"]))
    sync_equipment_row(cursor, item_id, user["company_id"])
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/equipment/{item_id}/delete")
async def delete_equipment(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/", user)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients WHERE company_id=? ORDER BY name ASC", (user["company_id"],))
    clients = cursor.fetchall()
    conn.close()
    err = request.query_params.get("error", "")
    return templates.TemplateResponse(
        request=request,
        name="clients.html",
        context={"title": "Clients", "clients": clients, "current_user": user, "error": err},
    )


@app.post("/clients")
async def add_client(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/clients", user)
    form = await request.form()
    clean_name = str(form.get("name", "")).strip()
    if not clean_name:
        return RedirectResponse(url="/clients?error=Client%20name%20is%20required", status_code=303)
    contact_person = str(form.get("contact_person", "")).strip()
    phone = str(form.get("phone", "")).strip()
    email = str(form.get("email", "")).strip()
    address = str(form.get("address", "")).strip()
    vat_number = str(form.get("vat_number", "")).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO clients (name, company_id, contact_person, phone, email, address, vat_number)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (clean_name, user["company_id"], contact_person, phone, email, address, vat_number),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/clients", status_code=303)


@app.get("/clients/{client_id}/details")
def client_details_json(request: Request, client_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    profile = client_profile_by_id(user["company_id"], client_id)
    if not profile:
        return JSONResponse({"error": "Client not found"}, status_code=404)
    return JSONResponse(profile)


@app.get("/clients/{client_id}/edit", response_class=HTMLResponse)
def client_edit_page(request: Request, client_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients WHERE id=? AND company_id=?", (client_id, user["company_id"]))
    client = cursor.fetchone()
    conn.close()
    if not client:
        return render_message(request, "Error", "Client not found.", "/clients", user)
    err = request.query_params.get("error", "")
    return templates.TemplateResponse(
        request=request,
        name="client_edit.html",
        context={"title": "Edit Client", "client": client, "current_user": user, "error": err},
    )


@app.post("/clients/{client_id}/edit")
async def client_edit_save(request: Request, client_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/clients/{client_id}/edit", user)
    form = await request.form()
    clean_name = str(form.get("name", "")).strip()
    if not clean_name:
        return RedirectResponse(url=f"/clients/{client_id}/edit?error=Client%20name%20is%20required", status_code=303)
    contact_person = str(form.get("contact_person", "")).strip()
    phone = str(form.get("phone", "")).strip()
    email = str(form.get("email", "")).strip()
    address = str(form.get("address", "")).strip()
    vat_number = str(form.get("vat_number", "")).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM clients WHERE id=? AND company_id=?", (client_id, user["company_id"]))
    if not cursor.fetchone():
        conn.close()
        return RedirectResponse(url="/clients?error=Not%20found", status_code=303)
    cursor.execute(
        """
        UPDATE clients
        SET name=?, contact_person=?, phone=?, email=?, address=?, vat_number=?
        WHERE id=? AND company_id=?
        """,
        (clean_name, contact_person, phone, email, address, vat_number, client_id, user["company_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/clients", status_code=303)


@app.get("/sub-rentals", response_class=HTMLResponse)
def sub_rentals_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM sub_rentals WHERE company_id=? ORDER BY id DESC",
        (user["company_id"],),
    )
    rows = cursor.fetchall()
    conn.close()
    err = request.query_params.get("error", "")
    return templates.TemplateResponse(
        request=request,
        name="sub_rentals.html",
        context={"title": "Sub-Rental Management", "rows": rows, "current_user": user, "error": err},
    )


@app.post("/sub-rentals")
async def sub_rentals_add(
    request: Request,
    supplier_name: str = Form(...),
    equipment_description: str = Form(...),
    quantity_total: int = Form(...),
    cost_per_unit: int = Form(default=0),
    notes: str = Form(default=""),
):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/sub-rentals", user)
    sup = supplier_name.strip()
    desc = equipment_description.strip()
    if not sup or not desc:
        return RedirectResponse(url="/sub-rentals?error=Supplier%20and%20description%20are%20required", status_code=303)
    if quantity_total < 1:
        return RedirectResponse(url="/sub-rentals?error=Total%20units%20must%20be%20at%20least%201", status_code=303)
    if cost_per_unit < 0:
        return RedirectResponse(url="/sub-rentals?error=Cost%20per%20unit%20must%20be%200%20or%20more", status_code=303)
    created = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO sub_rentals (company_id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user["company_id"], sup, desc, quantity_total, quantity_total, cost_per_unit, notes.strip(), created),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/sub-rentals", status_code=303)


@app.get("/sub-rentals/{sub_id}/edit", response_class=HTMLResponse)
def sub_rental_edit_page(request: Request, sub_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sub_rentals WHERE id=? AND company_id=?", (sub_id, user["company_id"]))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return render_message(request, "Error", "Sub-rental item not found.", "/sub-rentals", user)
    return templates.TemplateResponse(
        request=request,
        name="sub_rental_edit.html",
        context={"title": "Edit Sub-Rental", "item": row, "current_user": user},
    )


@app.post("/sub-rentals/{sub_id}/edit")
async def sub_rental_edit_save(
    request: Request,
    sub_id: int,
    supplier_name: str = Form(...),
    equipment_description: str = Form(...),
    quantity_total: int = Form(...),
    cost_per_unit: int = Form(default=0),
    notes: str = Form(default=""),
):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/sub-rentals/{sub_id}/edit", user)
    sup = supplier_name.strip()
    desc = equipment_description.strip()
    if not sup or not desc:
        return RedirectResponse(url=f"/sub-rentals/{sub_id}/edit?error=Supplier%20and%20description%20are%20required", status_code=303)
    if quantity_total < 1:
        return RedirectResponse(url=f"/sub-rentals/{sub_id}/edit?error=Total%20units%20must%20be%20at%20least%201", status_code=303)
    if cost_per_unit < 0:
        return RedirectResponse(url=f"/sub-rentals/{sub_id}/edit?error=Cost%20invalid", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sub_rentals WHERE id=? AND company_id=?", (sub_id, user["company_id"]))
    cur = cursor.fetchone()
    if not cur:
        conn.close()
        return RedirectResponse(url="/sub-rentals?error=Not%20found", status_code=303)
    old_total = int(cur["quantity_total"] or 0)
    old_avail = int(cur["quantity_available"] or 0)
    in_use = max(0, old_total - old_avail)
    new_total = quantity_total
    new_avail = new_total - in_use
    if new_avail < 0:
        conn.close()
        return RedirectResponse(
            url=f"/sub-rentals/{sub_id}/edit?error=Total%20units%20cannot%20be%20less%20than%20units%20already%20allocated",
            status_code=303,
        )
    cursor.execute(
        """
        UPDATE sub_rentals
        SET supplier_name=?, equipment_description=?, quantity_total=?, quantity_available=?, cost_per_unit=?, notes=?
        WHERE id=? AND company_id=?
        """,
        (sup, desc, new_total, new_avail, cost_per_unit, notes.strip(), sub_id, user["company_id"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/sub-rentals", status_code=303)


@app.post("/sub-rentals/{sub_id}/delete")
async def sub_rental_delete(request: Request, sub_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/sub-rentals", user)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM sub_rentals WHERE id=? AND company_id=?", (sub_id, user["company_id"]))
    if not cursor.fetchone():
        conn.close()
        return RedirectResponse(url="/sub-rentals?error=Not%20found", status_code=303)
    cursor.execute(
        """
        SELECT 1 FROM sub_rental_usage u
        JOIN jobs j ON j.id = u.job_id AND j.company_id = u.company_id
        WHERE u.sub_rental_id = ? AND u.company_id = ?
        AND j.status IN ('upcoming', 'active')
        LIMIT 1
        """,
        (sub_id, user["company_id"]),
    )
    if cursor.fetchone():
        conn.close()
        return RedirectResponse(
            url="/sub-rentals?error=Cannot%20delete%20while%20units%20are%20on%20an%20open%20job",
            status_code=303,
        )
    cursor.execute("DELETE FROM sub_rental_usage WHERE sub_rental_id=? AND company_id=?", (sub_id, user["company_id"]))
    cursor.execute("DELETE FROM sub_rentals WHERE id=? AND company_id=?", (sub_id, user["company_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/sub-rentals", status_code=303)


@app.get("/quote", response_class=HTMLResponse)
def quote_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, name, price, quantity, quantity_rented,
               (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) AS qty_available
        FROM equipment
        WHERE company_id=? AND (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) > 0
        ORDER BY name ASC
        """,
        (user["company_id"],),
    )
    items = cursor.fetchall()
    cursor.execute(
        """
        SELECT id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit, notes
        FROM sub_rentals
        WHERE company_id=? AND quantity_available > 0
        ORDER BY supplier_name ASC, id ASC
        """,
        (user["company_id"],),
    )
    sub_rentals = cursor.fetchall()
    cursor.execute("SELECT * FROM clients WHERE company_id=? ORDER BY name ASC", (user["company_id"],))
    clients = cursor.fetchall()
    conn.close()
    company = get_company_settings(user["company_id"])
    return templates.TemplateResponse(
        request=request,
        name="quote.html",
        context={
            "title": "Create Quote",
            "items": items,
            "sub_rentals": sub_rentals,
            "clients": clients,
            "company": company,
            "current_user": user,
        },
    )


@app.post("/quote", response_class=HTMLResponse)
async def generate_quote(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/quote", user)
    form_data = await request.form()
    try:
        client_id = int(str(form_data.get("client_id", "")).strip())
    except ValueError:
        return render_message(request, "Create Quote", "Please select a client.", "/quote", user)
    profile = client_profile_by_id(user["company_id"], client_id)
    if not profile:
        return render_message(request, "Create Quote", "Client not found.", "/quote", user)
    clean_client_name = profile["name"]
    job_name = str(form_data.get("job_name", "")).strip()
    if not job_name:
        return render_message(request, "Create Quote", "Job name is required.", "/quote", user)
    site_location = str(form_data.get("site_location", "")).strip()
    start_date = str(form_data.get("start_date", "")).strip()
    end_date = str(form_data.get("end_date", "")).strip()
    special_notes = str(form_data.get("special_notes", "")).strip()
    quote_terms = str(form_data.get("quote_terms", "")).strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, name, price, quantity, quantity_rented
        FROM equipment
        WHERE company_id=? AND (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) > 0
        ORDER BY name ASC
        """,
        (user["company_id"],),
    )
    items = cursor.fetchall()
    cursor.execute(
        """
        SELECT id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit
        FROM sub_rentals
        WHERE company_id=? AND quantity_available > 0
        ORDER BY supplier_name ASC, id ASC
        """,
        (user["company_id"],),
    )
    sub_stock = cursor.fetchall()
    lines = []
    quote_date = datetime.now().strftime("%Y-%m-%d")
    quote_number = datetime.now().strftime("%Y%m%d%H%M")
    for item in items:
        item_id = item["id"]
        qty_raw = str(form_data.get(f"qty_{item_id}", "0")).strip()
        days_raw = str(form_data.get(f"days_{item_id}", "1")).strip()
        try:
            qty = int(qty_raw)
            days = int(days_raw)
        except ValueError:
            conn.close()
            return render_message(request, "Create Quote", "Quantity and days must be whole numbers.", "/quote", user)
        if qty < 0 or days < 1:
            conn.close()
            return render_message(request, "Create Quote", "Quantity must be 0+ and days at least 1.", "/quote", user)
        if qty == 0:
            continue
        avail = qty_available(item)
        if qty > avail:
            conn.close()
            return render_message(request, "Create Quote", "Not enough units available", "/quote", user)
        unit_price = int(item["price"] or 0)
        line_total = qty * unit_price * days
        lines.append(
            {
                "line_type": "owned",
                "equipment_id": item_id,
                "name": item["name"],
                "qty": qty,
                "unit_price": unit_price,
                "days": days,
                "line_total": line_total,
            }
        )
    for sr in sub_stock:
        sid = int(sr["id"])
        qty_raw = str(form_data.get(f"sub_qty_{sid}", "0")).strip()
        days_raw = str(form_data.get(f"sub_days_{sid}", "1")).strip()
        show_raw = str(form_data.get(f"sub_show_{sid}", "0")).strip()
        try:
            qty = int(qty_raw)
            days = int(days_raw)
        except ValueError:
            conn.close()
            return render_message(request, "Create Quote", "Sub-rental quantity and days must be whole numbers.", "/quote", user)
        if qty < 0 or days < 1:
            conn.close()
            return render_message(request, "Create Quote", "Sub-rental quantity must be 0+ and days at least 1.", "/quote", user)
        if qty == 0:
            continue
        avail = max(0, int(sr["quantity_available"] or 0))
        if qty > avail:
            conn.close()
            return render_message(request, "Create Quote", "Not enough sub-rental units available", "/quote", user)
        unit_price = int(sr["cost_per_unit"] or 0)
        line_total = qty * unit_price * days
        desc = str(sr["equipment_description"] or "").strip()
        supplier = str(sr["supplier_name"] or "").strip()
        show_on_quote = 1 if show_raw in ("1", "yes", "Yes", "true", "True") else 0
        lines.append(
            {
                "line_type": "sub_rental",
                "sub_rental_id": sid,
                "supplier_name": supplier,
                "name": desc,
                "qty": qty,
                "unit_price": unit_price,
                "days": days,
                "line_total": line_total,
                "show_on_quote": show_on_quote,
            }
        )
    if not lines:
        conn.close()
        return render_message(request, "Create Quote", "Please add quantity for at least one owned or sub-rental item.", "/quote", user)
    subtotal = sum(int(l.get("line_total", 0) or 0) for l in lines)
    com = get_company_settings(user["company_id"])
    try:
        discount_percent_in = float(str(form_data.get("discount_percent", com.get("default_discount_percent", 0))).strip())
    except ValueError:
        discount_percent_in = 0.0
    discount_percent_in = max(0.0, min(100.0, discount_percent_in))
    vat_on_raw = form_data.get("vat_on")
    if vat_on_raw is None:
        vat_enabled = bool(int(com.get("vat_enabled") or 0))
    else:
        vat_enabled = str(vat_on_raw).strip() in ("1", "on", "yes", "true", "True")
    try:
        vat_pct_use = float(com.get("vat_percent") or 15)
    except (TypeError, ValueError):
        vat_pct_use = 15.0
    vat_pct_use = max(0.0, min(100.0, vat_pct_use))
    totals = compute_financial_totals(subtotal, discount_percent_in, vat_enabled, vat_pct_use)
    grand_total = int(totals["grand_total"])
    line_items_json = json.dumps(lines)
    created_at = datetime.utcnow().isoformat()
    for _attempt in range(5):
        try:
            cursor.execute(
                """
                INSERT INTO quotes (
                    company_id, quote_number, client_name, quote_date, total, line_items_json, status, created_at,
                    subtotal, discount_percent, discount_amount, vat_enabled, vat_percent, vat_amount, grand_total,
                    job_name, site_location, start_date, end_date, special_notes, quote_terms
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["company_id"],
                    quote_number,
                    clean_client_name,
                    quote_date,
                    grand_total,
                    line_items_json,
                    created_at,
                    int(totals["subtotal"]),
                    totals["discount_percent"],
                    int(totals["discount_amount"]),
                    1 if totals["vat_enabled"] else 0,
                    totals["vat_percent"],
                    int(totals["vat_amount"]),
                    grand_total,
                    job_name,
                    site_location or None,
                    start_date or None,
                    end_date or None,
                    special_notes or None,
                    quote_terms or None,
                ),
            )
            break
        except sqlite3.IntegrityError:
            conn.rollback()
            quote_number = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    else:
        conn.close()
        return render_message(request, "Create Quote", "Could not save quote. Please try again.", "/quote", user)
    quote_id = cursor.lastrowid
    conn.commit()
    conn.close()
    download_url = f"/download?{urlencode({'quote_id': str(quote_id)})}"
    return templates.TemplateResponse(
        request=request,
        name="quote_result.html",
        context={
            "title": "Quote",
            "client_name": clean_client_name,
            "quote_date": quote_date,
            "quote_number": quote_number,
            "lines": lines,
            "totals": totals,
            "total": grand_total,
            "download_url": download_url,
            "quote_id": quote_id,
            "current_user": user,
        },
    )


def show_sub_rental_on_client_pdf(line: dict) -> bool:
    v = line.get("show_on_quote", 0)
    return v in (1, True, "1", "yes", "Yes", "true", "True")


def quote_lines_for_client_pdf(lines: list) -> tuple[list[list], int]:
    """Rows for client PDF (sub-rental lines omitted when show_on_quote is false)."""
    table_rows: list[list] = []
    pdf_total = 0
    for line in lines:
        if line.get("equipment_id") is None and quote_line_is_sub_rental(line) and not show_sub_rental_on_client_pdf(line):
            continue
        try:
            qty = int(line.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        try:
            days = int(line.get("days", 1) or 1)
        except (TypeError, ValueError):
            days = 1
        name = str(line.get("name", "")).strip() or "—"
        try:
            unit_price = int(line.get("unit_price", 0) or 0)
        except (TypeError, ValueError):
            unit_price = 0
        try:
            line_total = int(line.get("line_total", 0) or 0)
        except (TypeError, ValueError):
            line_total = 0
        table_rows.append([str(qty), name, f"R{unit_price}", str(days), f"R{line_total}"])
        pdf_total += line_total
    return table_rows, pdf_total


def quote_pdf_file_response(
    request: Request,
    user: dict,
    settings: dict,
    client: str,
    date: str,
    qnum: str,
    lines: list,
    fin: dict[str, int | float | bool],
    error_back_url: str,
    quote_meta: dict | None = None,
) -> FileResponse | HTMLResponse:
    """Build quote PDF from in-memory line items and totals; return FileResponse or error page."""
    body_rows, _pdf_vis = quote_lines_for_client_pdf(lines)
    with tempfile.NamedTemporaryFile(prefix=f"quote_{qnum}_", suffix=".pdf", delete=False) as tmp:
        temp_pdf_path = Path(tmp.name)
    doc = SimpleDocTemplate(str(temp_pdf_path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    content = []
    cid = int(user["company_id"])
    cn = escape(str(settings.get("company_name") or "Rental Company"))
    header_parts = [f"<b><font size='14'>{cn}</font></b>"]
    if settings.get("tagline"):
        header_parts.append(escape(str(settings["tagline"]).strip()))
    if settings.get("email"):
        header_parts.append(f"Email: {escape(str(settings['email']).strip())}")
    if settings.get("phone"):
        header_parts.append(f"Phone: {escape(str(settings['phone']).strip())}")
    if settings.get("address"):
        header_parts.append(f"Address: {escape(str(settings['address']).strip())}")
    if settings.get("vat_number"):
        header_parts.append(f"VAT/Reg: {escape(str(settings['vat_number']).strip())}")
    company_blk = Paragraph("<br/>".join(header_parts), styles["Normal"])
    logo_flow = pdf_company_logo_flowable(cid)
    if logo_flow:
        quote_header = Table([[logo_flow, company_blk]], colWidths=[35 * mm, 143 * mm])
        quote_header.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0, 0), (0, 0), "LEFT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        content.append(quote_header)
    else:
        content.append(company_blk)
    content.append(Spacer(1, 12))
    meta = quote_meta or {}
    client_prof = meta.get("client_profile")
    if client_prof is None:
        client_prof = client_profile_by_name(int(user["company_id"]), client)
    for flow in pdf_client_details_flowables(client, client_prof, styles):
        content.append(flow)
    for flow in pdf_job_details_flowables(
        meta.get("job_name"),
        meta.get("site_location"),
        meta.get("start_date"),
        meta.get("end_date"),
        meta.get("special_notes"),
        styles,
    ):
        content.append(flow)
    content.append(Paragraph(f"Date: {date}", styles["Normal"]))
    content.append(Paragraph(f"Quote #: {qnum}", styles["Normal"]))
    content.append(Spacer(1, 12))
    quote_desc_cell_style = ParagraphStyle(
        name="QuotePdfEquipmentDescCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceBefore=0,
        spaceAfter=0,
    )
    table_rows: list[list] = [["Qty", "Equipment / Description", "Unit Price", "Days", "Line Total"]]
    if not body_rows:
        table_rows.append(
            [
                "—",
                Paragraph(escape("No line items included on this client quote."), quote_desc_cell_style),
                "",
                "",
                "",
            ]
        )
    else:
        for br in body_rows:
            table_rows.append(
                [
                    br[0],
                    Paragraph(escape(str(br[1])), quote_desc_cell_style),
                    br[2],
                    br[3],
                    br[4],
                ]
            )
    quote_col_widths = [12 * mm, 104 * mm, 26 * mm, 12 * mm, 24 * mm]
    quote_table = Table(table_rows, colWidths=quote_col_widths, repeatRows=1)
    quote_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f3b52")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 1), (4, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9dce3")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
            ]
        )
    )
    content.append(quote_table)
    content.append(Spacer(1, 12))
    red_hex = "#b91c1c"
    discount_label_style = ParagraphStyle(
        name="QuotePdfDiscountLbl",
        parent=styles["Normal"],
        textColor=colors.HexColor(red_hex),
        fontSize=10,
        alignment=TA_LEFT,
    )
    discount_amt_style = ParagraphStyle(
        name="QuotePdfDiscountAmt",
        parent=styles["Normal"],
        textColor=colors.HexColor(red_hex),
        fontSize=10,
        alignment=TA_RIGHT,
    )
    quote_summary_amt_style = ParagraphStyle(
        name="QuotePdfSummaryAmt",
        parent=styles["Normal"],
        fontSize=10,
        alignment=TA_RIGHT,
    )
    grand_label_style = ParagraphStyle(
        name="QuotePdfGrandLbl",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        alignment=TA_LEFT,
    )
    grand_amt_style = ParagraphStyle(
        name="QuotePdfGrandAmt",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        alignment=TA_RIGHT,
    )
    tot_rows: list[list] = [
        ["Subtotal", Paragraph(escape(f"R{int(round(float(fin['subtotal'])))}"), quote_summary_amt_style)],
    ]
    if int(fin.get("discount_amount") or 0) > 0:
        dp = fin["discount_percent"]
        tot_rows.append(
            [
                Paragraph(escape(f"Discount ({dp:g}%)"), discount_label_style),
                Paragraph(escape(f"R{int(round(float(fin['discount_amount'])))}"), discount_amt_style),
            ]
        )
    if fin.get("vat_enabled") and int(fin.get("vat_amount") or 0) > 0:
        vp = fin["vat_percent"]
        tot_rows.append(
            [
                Paragraph(escape(f"VAT ({vp:g}%)"), styles["Normal"]),
                Paragraph(escape(f"R{int(round(float(fin['vat_amount'])))}"), quote_summary_amt_style),
            ]
        )
    tot_rows.append(
        [
            Paragraph("Grand total", grand_label_style),
            Paragraph(escape(f"R{int(round(float(fin['grand_total'])))}"), grand_amt_style),
        ]
    )
    quote_line_items_width = sum(quote_col_widths)
    q_totals_amt_col_w = 52 * mm
    q_totals_label_col_w = quote_line_items_width - q_totals_amt_col_w
    total_table = Table(tot_rows, colWidths=[q_totals_label_col_w, q_totals_amt_col_w])
    total_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef2f8")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#c2c8d3")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    content.append(total_table)
    content.append(Spacer(1, 10))
    for flow in pdf_terms_flowables(meta.get("quote_terms"), styles):
        content.append(flow)
    for flow in pdf_banking_detail_flowables(settings, styles):
        content.append(flow)
    if settings.get("quote_footer"):
        content.append(Spacer(1, 6))
        content.append(Paragraph(settings["quote_footer"], styles["Italic"]))
    try:
        doc.build(content)
    except Exception:
        logger.exception("PDF generation failed for company_id=%s quote=%s", user["company_id"], qnum)
        temp_pdf_path.unlink(missing_ok=True)
        return render_message(request, "Quote Error", "Could not generate PDF. Please try again.", error_back_url, user)
    return FileResponse(str(temp_pdf_path), filename=f"quote-{qnum}.pdf", background=BackgroundTask(lambda p: Path(p).unlink(missing_ok=True), str(temp_pdf_path)))


@app.get("/download")
def download(
    request: Request,
    quote_id: int | None = None,
    data: str | None = None,
    total: int | None = None,
    client: str | None = None,
    qnum: str | None = None,
    date: str | None = None,
):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    settings = get_company_settings(user["company_id"])
    lines: list[dict] = []
    fin: dict[str, int | float | bool] = {}
    if quote_id is not None:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM quotes WHERE id=? AND company_id=?", (quote_id, user["company_id"]))
        qrow = cursor.fetchone()
        conn.close()
        if not qrow:
            return render_message(request, "Quote Error", "Quote not found.", "/quote", user)
        qdict = dict(qrow)
        try:
            lines = json.loads(qdict["line_items_json"])
        except json.JSONDecodeError:
            lines = []
        if not isinstance(lines, list):
            lines = []
        client = qdict["client_name"]
        date = qdict["quote_date"]
        qnum = qdict["quote_number"]
        fin = quote_financials_from_saved_row(qdict, lines)
        meta = quote_meta_from_row(qdict, user["company_id"], client)
        return quote_pdf_file_response(request, user, settings, client, date, qnum, lines, fin, "/quote", quote_meta=meta)
    elif data:
        client = client or ""
        date = date or ""
        qnum = qnum or "quote"
        for item in data.split(";;"):
            if not item.strip():
                continue
            parts = item.split("~", 4)
            if len(parts) < 5:
                continue
            name, qty_s, unit_price_s, days_s, line_total_s = parts
            lines.append(
                {
                    "name": name,
                    "qty": int(qty_s) if qty_s.isdigit() else 1,
                    "unit_price": int(unit_price_s) if str(unit_price_s).isdigit() else 0,
                    "days": int(days_s) if str(days_s).isdigit() else 1,
                    "line_total": int(line_total_s) if str(line_total_s).isdigit() else 0,
                }
            )
        st = invoice_line_subtotal(lines)
        fin = compute_financial_totals(st, 0.0, False, 15.0)
        if total is not None:
            fin = {**fin, "grand_total": int(total)}
        return quote_pdf_file_response(request, user, settings, client, date, qnum, lines, fin, "/quote")
    else:
        return render_message(request, "Quote Error", "Missing quote reference.", "/quote", user)


@app.get("/rent/{item_id}", response_class=HTMLResponse)
def rent_page(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients WHERE company_id=? ORDER BY name ASC", (user["company_id"],))
    clients = cursor.fetchall()
    cursor.execute("SELECT * FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
    equipment = cursor.fetchone()
    conn.close()
    if not equipment:
        return render_message(request, "Error", "Equipment not found.", "/", user)
    if not clients:
        return render_message(request, "Rent Equipment", "Add at least one client before renting.", "/clients", user)
    qty_avail = qty_available(equipment)
    if qty_avail < 1:
        return render_message(request, "Rent Equipment", "Not enough units available", "/", user)
    return templates.TemplateResponse(
        request=request,
        name="rent.html",
        context={
            "title": "Rent Equipment",
            "clients": clients,
            "equipment": equipment,
            "qty_available": qty_avail,
            "current_user": user,
        },
    )


@app.post("/rent/{item_id}")
async def rent_item(request: Request, item_id: int, client: str = Form(...), due_date: str = Form(...), units: int = Form(default=1)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/rent/{item_id}", user)
    clean_client = client.strip()
    if not clean_client:
        return render_message(request, "Rent Equipment", "Client is required.", "/", user)
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return render_message(request, "Rent Equipment", "Due date must be YYYY-MM-DD.", "/", user)
    if units < 1:
        return render_message(request, "Rent Equipment", "Units must be at least 1.", f"/rent/{item_id}", user)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
        equipment = cursor.fetchone()
        if not equipment:
            conn.rollback()
            conn.close()
            return render_message(request, "Error", "Equipment not found.", "/", user)
        if units > qty_available(equipment):
            conn.rollback()
            conn.close()
            return render_message(request, "Rent Equipment", "Not enough units available", f"/rent/{item_id}", user)
        cursor.execute(
            """
            UPDATE equipment
            SET quantity_rented = COALESCE(quantity_rented, 0) + ?,
                rented_to = ?,
                due_date = ?,
                prep_status = 'pending'
            WHERE id=? AND company_id=?
            """,
            (units, clean_client, due_date, item_id, user["company_id"]),
        )
        sync_equipment_row(cursor, item_id, user["company_id"])
        cursor.execute(
            """
            INSERT INTO rental_history (equipment_name, client, date_rented, due_date, company_id, units)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (equipment["name"], clean_client, datetime.now().strftime("%Y-%m-%d"), due_date, user["company_id"], units),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/return/{item_id}", response_class=HTMLResponse)
def return_item_page(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
    equipment = cursor.fetchone()
    conn.close()
    if not equipment:
        return render_message(request, "Error", "Equipment not found.", "/", user)
    if qty_rented(equipment) < 1:
        return render_message(request, "Return", "Nothing to return for this item.", "/", user)
    return templates.TemplateResponse(
        request=request,
        name="return.html",
        context={
            "title": "Return Equipment",
            "equipment": equipment,
            "max_units": qty_rented(equipment),
            "current_user": user,
        },
    )


@app.post("/return/{item_id}")
async def return_item_submit(request: Request, item_id: int, units: int = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/return/{item_id}", user)
    if units < 1:
        return RedirectResponse(url=f"/return/{item_id}?error=Units%20must%20be%20at%20least%201", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT name, rented_to, quantity_rented FROM equipment WHERE id=? AND company_id=?", (item_id, user["company_id"]))
        equipment = cursor.fetchone()
        if not equipment:
            conn.rollback()
            conn.close()
            return render_message(request, "Error", "Equipment not found.", "/", user)
        qr = qty_rented(equipment)
        if units > qr:
            conn.rollback()
            conn.close()
            return RedirectResponse(url=f"/return/{item_id}?error=Not%20enough%20units%20available", status_code=303)
        apply_rental_return_units(cursor, user["company_id"], equipment["name"], units)
        cursor.execute(
            "UPDATE equipment SET quantity_rented = MAX(0, COALESCE(quantity_rented,0) - ?) WHERE id=? AND company_id=?",
            (units, item_id, user["company_id"]),
        )
        sync_equipment_row(cursor, item_id, user["company_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, invoice_number, client_name, created_at, due_date, total,
               COALESCE(amount_paid, 0) AS amount_paid, payment_status
        FROM invoices
        WHERE company_id=?
        ORDER BY id DESC
        """,
        (user["company_id"],),
    )
    rows = cursor.fetchall()
    conn.close()
    today = datetime.now().strftime("%Y-%m-%d")
    invoices_out = []
    for r in rows:
        d = dict(r)
        tot = int(d.get("total") or 0)
        ap = int(d.get("amount_paid") or 0)
        d["remaining"] = max(0, tot - ap)
        ps = str(d.get("payment_status") or "unpaid").lower()
        d["payment_status_norm"] = ps
        dd = d.get("due_date") or ""
        d["overdue"] = ps in ("unpaid", "partial") and bool(dd) and dd < today
        invoices_out.append(d)
    return templates.TemplateResponse(
        request=request,
        name="invoices_list.html",
        context={"title": "Invoices", "invoices": invoices_out, "current_user": user},
    )


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail_page(request: Request, invoice_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM invoices WHERE id=? AND company_id=?", (invoice_id, user["company_id"]))
    inv_row = cursor.fetchone()
    if not inv_row:
        conn.close()
        return render_message(request, "Not found", "Invoice not found.", "/invoices", user)
    inv = dict(inv_row)
    quote_terms = ""
    if inv.get("quote_id"):
        cursor.execute(
            "SELECT quote_terms FROM quotes WHERE id=? AND company_id=?",
            (inv["quote_id"], user["company_id"]),
        )
        qt_row = cursor.fetchone()
        if qt_row:
            quote_terms = str(qt_row["quote_terms"] or "").strip()
    cursor.execute(
        """
        SELECT ip.amount, ip.recorded_at, u.full_name AS recorded_by_name
        FROM invoice_payments ip
        JOIN users u ON u.id = ip.recorded_by_user_id AND u.company_id = ip.company_id
        WHERE ip.invoice_id=? AND ip.company_id=?
        ORDER BY ip.recorded_at ASC, ip.id ASC
        """,
        (invoice_id, user["company_id"]),
    )
    payments = [dict(p) for p in cursor.fetchall()]
    conn.close()
    settings = get_company_settings(user["company_id"])
    lines = parse_invoice_line_items_json(inv.get("line_items_json"))
    fin = invoice_financials_from_row(inv, lines)
    total = int(fin["grand_total"])
    amount_paid = int(inv.get("amount_paid") or 0)
    remaining = max(0, total - amount_paid)
    today = datetime.now().strftime("%Y-%m-%d")
    dd = inv.get("due_date") or ""
    ps = str(inv.get("payment_status") or "unpaid").lower()
    overdue = ps in ("unpaid", "partial") and bool(dd) and dd < today
    logo_uri = company_logo_file_uri(settings)
    logo_href = company_logo_web_path(settings)
    err = request.query_params.get("error", "")
    client_contact = client_contact_from_directory(user["company_id"], inv.get("client_name"))
    return templates.TemplateResponse(
        request=request,
        name="invoice_detail.html",
        context={
            "title": f"Invoice {inv.get('invoice_number', '')}",
            "invoice": inv,
            "settings": settings,
            "line_items": lines,
            "payments": payments,
            "fin": fin,
            "subtotal": int(fin["subtotal"]),
            "total": total,
            "amount_paid": amount_paid,
            "remaining": remaining,
            "overdue": overdue,
            "logo_uri": logo_uri,
            "logo_href": logo_href,
            "client_contact": client_contact,
            "quote_terms": quote_terms,
            "current_user": user,
            "error": err,
        },
    )


@app.post("/invoices/{invoice_id}/payment")
async def invoice_record_payment(request: Request, invoice_id: int, payment_amount: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/invoices/{invoice_id}", user)
    raw = str(payment_amount).strip()
    try:
        amt = int(raw)
    except ValueError:
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Invalid%20amount", status_code=303)
    if amt < 1:
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Amount%20must%20be%20at%20least%201", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM invoices WHERE id=? AND company_id=?",
            (invoice_id, user["company_id"]),
        )
        if not cursor.fetchone():
            conn.rollback()
            conn.close()
            return RedirectResponse(url="/invoices?error=Invoice%20not%20found", status_code=303)
        cursor.execute(
            """
            INSERT INTO invoice_payments (company_id, invoice_id, amount, recorded_by_user_id, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["company_id"], invoice_id, amt, user["user_id"], datetime.utcnow().isoformat()),
        )
        refresh_invoice_payment_aggregate(cursor, invoice_id, user["company_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf_download(request: Request, invoice_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM invoices WHERE id=? AND company_id=?", (invoice_id, user["company_id"]))
    inv_row = cursor.fetchone()
    if not inv_row:
        conn.close()
        return render_message(request, "Not found", "Invoice not found.", "/invoices", user)
    inv = dict(inv_row)
    conn.close()
    settings = get_company_settings(user["company_id"])
    line_items = parse_invoice_line_items_json(inv.get("line_items_json"))
    amount_paid = int(inv.get("amount_paid") or 0)
    total = int(inv.get("total") or 0)
    remaining = max(0, total - amount_paid)
    payment_status = str(inv.get("payment_status") or "unpaid").lower()
    try:
        pdf_bytes = build_invoice_pdf_bytes(
            settings,
            inv,
            line_items,
            amount_paid,
            total,
            remaining,
            payment_status,
        )
    except Exception:
        logger.exception("ReportLab invoice PDF failed invoice_id=%s company_id=%s", invoice_id, user["company_id"])
        return render_message(request, "PDF Error", "Could not generate invoice PDF. Please try again.", f"/invoices/{invoice_id}", user)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(inv.get("invoice_number") or ""))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice-{safe_name}.pdf"'},
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)

