from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
import hashlib
import json
import logging
import os
import requests
import secrets
import tempfile
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
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
DATABASE_URL = os.environ.get("DATABASE_URL")
SESSION_DAYS = 14
SESSION_SECURE_COOKIE = os.getenv("SESSION_SECURE_COOKIE", "0") == "1"
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
VALID_ROLES = {"admin", "management", "warehouse"}
JOB_STATUSES = {"upcoming", "active", "done"}
QUOTE_STATUSES = {"pending", "approved", "rejected"}
DEFAULT_EQUIPMENT_CATEGORIES = ("General", "Tools", "Vehicles", "Electronics", "Furniture", "Other")
DEFAULT_TECHNICIAN_FUNCTIONS = (
    ("Audio Technician", 0),
    ("Video Technician", 0),
    ("Lighting Technician", 0),
    ("Camera Operator", 0),
    ("Stage Manager", 0),
    ("Driver", 0),
    ("General Assistant", 0),
)
EQUIPMENT_CONDITIONS_OUT = frozenset({"Good", "Minor wear", "Damaged"})
EQUIPMENT_CONDITIONS_IN = frozenset({"Good", "Minor wear", "Damaged", "Missing items"})
LEGACY_EQUIPMENT_CATEGORIES = (
    "Audio",
    "Video",
    "Lighting",
    "Staging",
    "Power",
    "Rigging",
    "Backline",
    "Transport",
    "Other",
)
PDF_NAVY_HEX = "#0f1923"
TRANSPORT_TYPES = frozenset({"none", "fixed_fee", "per_km", "vehicle_hire"})
logger = logging.getLogger("rental_saas")

app = FastAPI(title="Rental SaaS App")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_column(cursor, table: str, column: str) -> bool:
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cursor.fetchone() is not None


def table_exists(cursor, table: str) -> bool:
    cursor.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
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


def seed_default_equipment_categories(cursor, company_id: int) -> None:
    created = datetime.utcnow().isoformat()
    for name in DEFAULT_EQUIPMENT_CATEGORIES:
        cursor.execute(
            """
            INSERT INTO equipment_categories (company_id, name, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (company_id, name) DO NOTHING
            """,
            (company_id, name, created),
        )


def seed_default_technician_functions(cursor, company_id: int) -> None:
    created = datetime.utcnow().isoformat()
    for name, day_rate in DEFAULT_TECHNICIAN_FUNCTIONS:
        cursor.execute(
            """
            INSERT INTO technician_functions (company_id, function_name, day_rate, created_at)
            SELECT %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM technician_functions
                WHERE company_id=%s AND function_name=%s
            )
            """,
            (company_id, name, day_rate, created, company_id, name),
        )


def list_technician_functions(company_id: int) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT id, function_name, day_rate, created_at
            FROM technician_functions
            WHERE company_id=%s
            ORDER BY function_name ASC
            """,
            (company_id,),
        )
        return [dict(r) for r in cursor.fetchall()]


def technician_function_by_id(cursor, company_id: int, function_id: int) -> dict | None:
    cursor.execute(
        "SELECT id, function_name, day_rate FROM technician_functions WHERE id=%s AND company_id=%s",
        (function_id, company_id),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def parse_personnel_lines_from_form(form_data, company_id: int, cursor) -> tuple[list[dict], str | None]:
    raw = str(form_data.get("personnel_json", "") or "").strip()
    if not raw:
        return [], None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return [], "Invalid personnel line data."
    if not isinstance(entries, list):
        return [], "Invalid personnel line data."
    lines: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            function_id = int(entry.get("function_id") or 0)
            qty = int(entry.get("qty") or 0)
            days = int(entry.get("days") or 0)
            day_rate = int(entry.get("day_rate") or 0)
        except (TypeError, ValueError):
            return [], "Personnel quantity, days, and rates must be whole numbers."
        if qty < 1 or days < 1:
            continue
        if day_rate < 0:
            return [], "Personnel day rate cannot be negative."
        fn = technician_function_by_id(cursor, company_id, function_id)
        if not fn:
            return [], "Selected technician function not found."
        line_total = qty * days * day_rate
        lines.append(
            {
                "line_type": "personnel",
                "technician_function_id": function_id,
                "name": fn["function_name"],
                "qty": qty,
                "days": days,
                "unit_price": day_rate,
                "line_total": line_total,
            }
        )
    return lines, None


def list_equipment_movements_for_job(cursor, company_id: int, job_id: int) -> list[dict]:
    cursor.execute(
        """
        SELECT em.*, u.full_name AS processed_by_name, jpi.equipment_name
        FROM equipment_movements em
        LEFT JOIN users u ON u.id = em.processed_by_user_id
        LEFT JOIN job_prep_items jpi ON jpi.id = em.prep_item_id
        WHERE em.company_id=%s AND em.job_id=%s
        ORDER BY em.processed_at ASC, em.id ASC
        """,
        (company_id, job_id),
    )
    return [dict(r) for r in cursor.fetchall()]


def prep_item_collection_state(movements: list[dict], prep_item_id: int) -> str:
    """Return 'out' if last movement is collected, 'in' if returned or none."""
    relevant = [m for m in movements if int(m.get("prep_item_id") or 0) == prep_item_id]
    if not relevant:
        return "none"
    last = relevant[-1]
    if str(last.get("movement_type") or "").lower() == "collected":
        return "out"
    return "in"


def movements_by_job_for_company(cursor, company_id: int) -> dict[int, list[dict]]:
    cursor.execute(
        """
        SELECT em.*, u.full_name AS processed_by_name, jpi.equipment_name
        FROM equipment_movements em
        LEFT JOIN users u ON u.id = em.processed_by_user_id
        LEFT JOIN job_prep_items jpi ON jpi.id = em.prep_item_id
        WHERE em.company_id=%s
        ORDER BY em.job_id ASC, em.processed_at ASC, em.id ASC
        """,
        (company_id,),
    )
    out: dict[int, list[dict]] = {}
    for row in cursor.fetchall():
        d = dict(row)
        jid = int(d["job_id"])
        out.setdefault(jid, []).append(d)
    return out


def migrate_equipment_categories_for_company(cursor, company_id: int) -> None:
    seed_default_equipment_categories(cursor, company_id)
    created = datetime.utcnow().isoformat()
    cursor.execute(
        """
        SELECT DISTINCT TRIM(category) AS cat FROM equipment
        WHERE company_id=%s AND category IS NOT NULL AND TRIM(category) <> ''
        """,
        (company_id,),
    )
    for row in cursor.fetchall():
        cat = str(row["cat"] if isinstance(row, dict) else row[0]).strip()
        if not cat:
            continue
        cursor.execute(
            """
            INSERT INTO equipment_categories (company_id, name, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (company_id, name) DO NOTHING
            """,
            (company_id, cat, created),
        )


def list_equipment_categories(company_id: int) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT id, name, created_at FROM equipment_categories WHERE company_id=%s ORDER BY name ASC",
            (company_id,),
        )
        return [dict(r) for r in cursor.fetchall()]


def equipment_category_names(company_id: int) -> set[str]:
    return {c["name"] for c in list_equipment_categories(company_id)}


def normalize_equipment_category(company_id: int, category: str) -> str:
    clean = category.strip()
    if not clean:
        return ""
    if clean in equipment_category_names(company_id):
        return clean
    return ""


def parse_transport_from_form(form_data) -> dict:
    ttype = str(form_data.get("transport_type", "none") or "none").strip().lower()
    if ttype not in TRANSPORT_TYPES:
        ttype = "none"

    def _int_field(key: str, default: int = 0) -> int:
        try:
            return max(0, int(str(form_data.get(key, default)).strip() or default))
        except (TypeError, ValueError):
            return default

    amount = 0
    description = ""
    if ttype == "fixed_fee":
        amount = _int_field("transport_fixed_amount")
        description = "Fixed delivery fee"
    elif ttype == "per_km":
        distance = _int_field("transport_distance_km")
        rate = _int_field("transport_rate_per_km")
        amount = distance * rate
        description = f"{distance} km @ R{rate}/km"
    elif ttype == "vehicle_hire":
        amount = _int_field("transport_day_rate")
        desc = str(form_data.get("transport_vehicle_description", "")).strip()
        description = desc or "Vehicle hire"
        if desc and amount:
            description = f"{desc} (day rate)"
    return {
        "transport_type": ttype,
        "transport_description": description,
        "transport_amount": int(amount),
    }


def transport_display_from_row(row: dict | None) -> dict:
    if not row:
        return {"transport_type": "none", "transport_description": "", "transport_amount": 0}
    ttype = str(row.get("transport_type") or "none").strip().lower()
    if ttype not in TRANSPORT_TYPES:
        ttype = "none"
    try:
        amount = int(row.get("transport_amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    return {
        "transport_type": ttype,
        "transport_description": str(row.get("transport_description") or "").strip(),
        "transport_amount": max(0, amount),
    }


def quote_financials_with_breakdown(qdict: dict, lines: list[dict]) -> dict:
    fin = quote_financials_from_saved_row(qdict, lines)
    transport = transport_display_from_row(qdict)
    equipment_subtotal = sum(int(l.get("line_total", 0) or 0) for l in lines if str(l.get("line_type") or "") not in ("personnel", "function", "technician"))
    fin["equipment_subtotal"] = equipment_subtotal
    fin["transport_amount"] = transport["transport_amount"]
    fin["transport_type"] = transport["transport_type"]
    fin["transport_description"] = transport["transport_description"]
    return fin


def init_db() -> None:
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
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
                expires_at TEXT NOT NULL
            )
            """
        )
        if not has_column(cursor, "sessions", "csrf_token"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN csrf_token TEXT")
        if table_exists(cursor, "sessions"):
            cursor.execute(
                """
                DO $$
                DECLARE
                    r RECORD;
                BEGIN
                    FOR r IN SELECT conname FROM pg_constraint
                              WHERE conrelid = 'sessions'::regclass
                              AND contype = 'f'
                    LOOP
                        EXECUTE 'ALTER TABLE sessions DROP CONSTRAINT ' || quote_ident(r.conname);
                    END LOOP;
                END $$;
                """
            )
            cursor.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_requests (
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
        if not has_column(cursor, "equipment", "category"):
            cursor.execute("ALTER TABLE equipment ADD COLUMN category TEXT NOT NULL DEFAULT ''")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS equipment_categories (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id),
                UNIQUE(company_id, name)
            )
            """
        )
        cursor.execute("UPDATE equipment SET quantity=1 WHERE quantity IS NULL OR quantity < 1")
        cursor.execute("UPDATE equipment SET quantity_rented=0 WHERE quantity_rented IS NULL")
        cursor.execute("UPDATE equipment SET quantity_rented=1 WHERE status='rented' AND quantity_rented=0")
        cursor.execute(
            "UPDATE equipment SET quantity = GREATEST(quantity::integer, quantity_rented::integer) WHERE quantity < quantity_rented"
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                name TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rental_history (
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                    INSERT INTO company_settings
                    (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (company_id) DO NOTHING
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

        cursor.execute("INSERT INTO companies (id, name, created_at) VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING", ("Default Company", datetime.utcnow().isoformat()))
        cursor.execute(
            """
            INSERT INTO company_settings
            (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
            VALUES
            (1, 'AVMAN RENTALS', 'Professional AV Equipment Rentals', 'info@avman.co.za', '+27 00 000 0000', '', '', 'Thank you for your business.')
            ON CONFLICT (company_id) DO NOTHING
            """
        )

        (BASE_DIR / "static" / "logos").mkdir(parents=True, exist_ok=True)

        for col, ddl in (
            ("tagline", "ALTER TABLE companies ADD COLUMN tagline TEXT"),
            ("email", "ALTER TABLE companies ADD COLUMN email TEXT"),
            ("phone", "ALTER TABLE companies ADD COLUMN phone TEXT"),
            ("address", "ALTER TABLE companies ADD COLUMN address TEXT"),
            ("vat_number", "ALTER TABLE companies ADD COLUMN vat_number TEXT"),
            ("vat_percent", "ALTER TABLE companies ADD COLUMN vat_percent NUMERIC NOT NULL DEFAULT 15"),
            ("vat_enabled", "ALTER TABLE companies ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
            ("default_discount_percent", "ALTER TABLE companies ADD COLUMN default_discount_percent NUMERIC NOT NULL DEFAULT 0"),
            ("bank_name", "ALTER TABLE companies ADD COLUMN bank_name TEXT"),
            ("bank_account_holder", "ALTER TABLE companies ADD COLUMN bank_account_holder TEXT"),
            ("bank_account_number", "ALTER TABLE companies ADD COLUMN bank_account_number TEXT"),
            ("bank_account_type", "ALTER TABLE companies ADD COLUMN bank_account_type TEXT"),
            ("bank_branch_code", "ALTER TABLE companies ADD COLUMN bank_branch_code TEXT"),
            ("bank_reference", "ALTER TABLE companies ADD COLUMN bank_reference TEXT"),
            ("terms_and_conditions", "ALTER TABLE companies ADD COLUMN terms_and_conditions TEXT"),
            ("logo_url", "ALTER TABLE companies ADD COLUMN logo_url TEXT DEFAULT NULL"),
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
                id SERIAL PRIMARY KEY,
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
            ("discount_percent", "ALTER TABLE quotes ADD COLUMN discount_percent NUMERIC NOT NULL DEFAULT 0"),
            ("discount_amount", "ALTER TABLE quotes ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0"),
            ("vat_enabled", "ALTER TABLE quotes ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
            ("vat_percent", "ALTER TABLE quotes ADD COLUMN vat_percent NUMERIC NOT NULL DEFAULT 15"),
            ("vat_amount", "ALTER TABLE quotes ADD COLUMN vat_amount INTEGER NOT NULL DEFAULT 0"),
            ("grand_total", "ALTER TABLE quotes ADD COLUMN grand_total INTEGER"),
            ("job_name", "ALTER TABLE quotes ADD COLUMN job_name TEXT"),
            ("site_location", "ALTER TABLE quotes ADD COLUMN site_location TEXT"),
            ("start_date", "ALTER TABLE quotes ADD COLUMN start_date TEXT"),
            ("end_date", "ALTER TABLE quotes ADD COLUMN end_date TEXT"),
            ("special_notes", "ALTER TABLE quotes ADD COLUMN special_notes TEXT"),
            ("quote_terms", "ALTER TABLE quotes ADD COLUMN quote_terms TEXT"),
            ("transport_type", "ALTER TABLE quotes ADD COLUMN transport_type TEXT NOT NULL DEFAULT 'none'"),
            ("transport_description", "ALTER TABLE quotes ADD COLUMN transport_description TEXT"),
            ("transport_amount", "ALTER TABLE quotes ADD COLUMN transport_amount INTEGER NOT NULL DEFAULT 0"),
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
                id SERIAL PRIMARY KEY,
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
            ("discount_percent", "ALTER TABLE invoices ADD COLUMN discount_percent NUMERIC NOT NULL DEFAULT 0"),
            ("discount_amount", "ALTER TABLE invoices ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0"),
            ("vat_enabled", "ALTER TABLE invoices ADD COLUMN vat_enabled INTEGER NOT NULL DEFAULT 0"),
            ("vat_percent", "ALTER TABLE invoices ADD COLUMN vat_percent NUMERIC NOT NULL DEFAULT 15"),
            ("vat_amount", "ALTER TABLE invoices ADD COLUMN vat_amount INTEGER NOT NULL DEFAULT 0"),
            ("grand_total", "ALTER TABLE invoices ADD COLUMN grand_total INTEGER"),
            ("transport_type", "ALTER TABLE invoices ADD COLUMN transport_type TEXT NOT NULL DEFAULT 'none'"),
            ("transport_description", "ALTER TABLE invoices ADD COLUMN transport_description TEXT"),
            ("transport_amount", "ALTER TABLE invoices ADD COLUMN transport_amount INTEGER NOT NULL DEFAULT 0"),
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                    id SERIAL PRIMARY KEY,
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

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS technician_functions (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL,
                function_name TEXT NOT NULL,
                day_rate INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id),
                UNIQUE(company_id, function_name)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS equipment_movements (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                prep_item_id INTEGER,
                equipment_id INTEGER,
                movement_type TEXT NOT NULL,
                collected_by_name TEXT,
                collected_by_contact TEXT,
                condition_out TEXT,
                condition_in TEXT,
                notes TEXT,
                processed_by_user_id INTEGER NOT NULL,
                processed_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id),
                FOREIGN KEY(job_id) REFERENCES jobs(id),
                FOREIGN KEY(prep_item_id) REFERENCES job_prep_items(id),
                FOREIGN KEY(processed_by_user_id) REFERENCES users(id)
            )
            """
        )

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
                cursor.execute("UPDATE job_status_log SET old_status=%s WHERE old_status=%s", (new, old))
                cursor.execute("UPDATE job_status_log SET new_status=%s WHERE new_status=%s", (new, old))

        cursor.execute("SELECT id FROM companies")
        for co in cursor.fetchall():
            cid = int(co["id"])
            migrate_equipment_categories_for_company(cursor, cid)
            if table_exists(cursor, "technician_functions"):
                seed_default_technician_functions(cursor, cid)


init_db()


def log_job_status_change(
    cursor: object, company_id: int, job_id: int, old_status: str | None, new_status: str, user_id: int
) -> None:
    cursor.execute(
        """
        INSERT INTO job_status_log (company_id, job_id, old_status, new_status, changed_at, changed_by_user_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (company_id, job_id, old_status, new_status, datetime.utcnow().isoformat(), user_id),
    )


def warehouse_may_set_job_status(from_status: str, to_status: str) -> bool:
    return (from_status == "upcoming" and to_status == "active") or (from_status == "active" and to_status == "done")


def qty_total(row: dict) -> int:
    v = row["quantity"] if "quantity" in row.keys() else None
    return max(1, int(v if v is not None else 1))


def qty_rented(row: dict) -> int:
    v = row["quantity_rented"] if "quantity_rented" in row.keys() else None
    return max(0, int(v if v is not None else 0))


def qty_available(row: dict) -> int:
    return max(0, qty_total(row) - qty_rented(row))


def sync_equipment_row(cursor: object, equip_id: int, company_id: int) -> None:
    cursor.execute(
        "SELECT quantity, quantity_rented, rented_to, due_date FROM equipment WHERE id=%s AND company_id=%s",
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
            WHERE id=%s AND company_id=%s
            """,
            (equip_id, company_id),
        )
    else:
        cursor.execute(
            "UPDATE equipment SET quantity_rented=%s, status='rented' WHERE id=%s AND company_id=%s",
            (qr, equip_id, company_id),
        )


def process_job_status_stock_delta(cursor: object, company_id: int, job_id: int, old_status: str, new_status: str) -> tuple[bool, str]:
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


def _resolve_prep_equipment_id(cursor: object, company_id: int, row: dict) -> int | None:
    eid = row["equipment_id"]
    if eid is not None and eid != "":
        try:
            return int(eid)
        except (TypeError, ValueError):
            pass
    cursor.execute(
        "SELECT id FROM equipment WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
        (company_id, row["equipment_name"]),
    )
    found = cursor.fetchone()
    return int(found["id"]) if found else None


def _prep_row_is_sub_rental(row: dict) -> bool:
    lt = row["line_type"] if "line_type" in row.keys() else None
    if (lt or "owned") == "sub_rental":
        return True
    sid = row["sub_rental_id"] if "sub_rental_id" in row.keys() else None
    return sid is not None and sid != ""


def release_job_stock(cursor: object, company_id: int, job_id: int) -> None:
    cursor.execute(
        "SELECT equipment_id, equipment_name, quantity, line_type, sub_rental_id FROM job_prep_items WHERE job_id=%s AND company_id=%s",
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
            "UPDATE equipment SET quantity_rented = GREATEST(0::integer, (COALESCE(quantity_rented, 0) - %s)::integer) WHERE id=%s AND company_id=%s",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)


def reserve_job_stock_from_prep(cursor: object, company_id: int, job_id: int) -> tuple[bool, str]:
    cursor.execute(
        "SELECT equipment_id, equipment_name, quantity, line_type, sub_rental_id FROM job_prep_items WHERE job_id=%s AND company_id=%s",
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
        cursor.execute("SELECT quantity, quantity_rented FROM equipment WHERE id=%s AND company_id=%s", (eid, company_id))
        er = cursor.fetchone()
        if not er:
            return False, "Equipment not found."
        if qty_available(er) < qty:
            return False, "Not enough units available"
        checks.append((eid, qty))
    for eid, qty in checks:
        cursor.execute(
            "UPDATE equipment SET quantity_rented = COALESCE(quantity_rented,0) + %s WHERE id=%s AND company_id=%s",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)
    return True, ""


def quote_line_is_sub_rental(line: dict) -> bool:
    return line.get("line_type") == "sub_rental"


def reserve_sub_rental_stock_for_quote_lines(cursor: object, company_id: int, lines: list) -> tuple[bool, str]:
    sub_lines = [ln for ln in lines if quote_line_is_sub_rental(ln)]
    for line in sub_lines:
        qty = max(1, int(line.get("qty", 1) or 1))
        sid = line.get("sub_rental_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return False, "Invalid sub-rental line."
        cursor.execute(
            "SELECT quantity_available FROM sub_rentals WHERE id=%s AND company_id=%s",
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
            SET quantity_available = quantity_available - %s
            WHERE id=%s AND company_id=%s AND quantity_available >= %s
            """,
            (qty, sid, company_id, qty),
        )
        if cursor.rowcount != 1:
            return False, "Could not reserve sub-rental stock"
    return True, ""


def restore_sub_rental_stock_after_job_done(cursor: object, company_id: int, job_id: int) -> None:
    cursor.execute(
        "SELECT sub_rental_id, units_used FROM sub_rental_usage WHERE job_id=%s AND company_id=%s",
        (job_id, company_id),
    )
    for row in cursor.fetchall():
        u = max(1, int(row["units_used"] or 1))
        sid = int(row["sub_rental_id"])
        cursor.execute(
            """
            UPDATE sub_rentals
            SET quantity_available = LEAST(quantity_total::integer, (quantity_available + %s)::integer)
            WHERE id=%s AND company_id=%s
            """,
            (u, sid, company_id),
        )


def reserve_sub_rental_stock_when_job_reopened_from_done(cursor: object, company_id: int, job_id: int) -> tuple[bool, str]:
    cursor.execute(
        "SELECT sub_rental_id, units_used FROM sub_rental_usage WHERE job_id=%s AND company_id=%s",
        (job_id, company_id),
    )
    rows = cursor.fetchall()
    for row in rows:
        u = max(1, int(row["units_used"] or 1))
        sid = int(row["sub_rental_id"])
        cursor.execute(
            "SELECT quantity_available FROM sub_rentals WHERE id=%s AND company_id=%s",
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
            SET quantity_available = quantity_available - %s
            WHERE id=%s AND company_id=%s AND quantity_available >= %s
            """,
            (u, sid, company_id, u),
        )
        if cursor.rowcount != 1:
            return False, "Could not adjust sub-rental stock"
    return True, ""


def reserve_stock_for_quote_lines(cursor: object, company_id: int, lines: list) -> tuple[bool, str]:
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
                "SELECT id FROM equipment WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
                (company_id, name),
            )
            found = cursor.fetchone()
            if not found:
                return False, f"No equipment named {name!r}."
            eid = int(found["id"])
        cursor.execute("SELECT quantity, quantity_rented FROM equipment WHERE id=%s AND company_id=%s", (eid, company_id))
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
                "SELECT id FROM equipment WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
                (company_id, name),
            )
            eid = int(cursor.fetchone()["id"])
        cursor.execute(
            "UPDATE equipment SET quantity_rented = COALESCE(quantity_rented,0) + %s WHERE id=%s AND company_id=%s",
            (qty, eid, company_id),
        )
        sync_equipment_row(cursor, eid, company_id)
    return True, ""


def apply_rental_return_units(
    cursor: object, company_id: int, equipment_name: str, return_units: int
) -> None:
    remaining = return_units
    while remaining > 0:
        cursor.execute(
            """
            SELECT id, COALESCE(units, 1) AS u FROM rental_history
            WHERE equipment_name=%s AND company_id=%s AND date_returned IS NULL
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
                "UPDATE rental_history SET date_returned=%s WHERE id=%s",
                (datetime.now().strftime("%Y-%m-%d"), h["id"]),
            )
            remaining -= hu
        else:
            cursor.execute("UPDATE rental_history SET units=%s WHERE id=%s", (hu - remaining, h["id"]))
            remaining = 0


def next_invoice_number(cursor: object, company_id: int) -> str:
    cursor.execute("SELECT COUNT(*) AS c FROM invoices WHERE company_id=%s", (company_id,))
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


def cloudinary_configured() -> bool:
    return bool(
        os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
        and os.environ.get("CLOUDINARY_API_KEY", "").strip()
        and os.environ.get("CLOUDINARY_API_SECRET", "").strip()
    )


def company_logo_url_from_settings(settings: dict | None) -> str | None:
    if not settings:
        return None
    url = str(settings.get("logo_url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def company_has_logo(settings: dict | None, company_id: int | None = None) -> bool:
    if company_logo_url_from_settings(settings):
        return True
    cid = company_id if company_id is not None else (settings or {}).get("company_id")
    if cid is not None:
        try:
            return company_static_logo_path(int(cid)).is_file()
        except (TypeError, ValueError, OSError):
            pass
    return False


def company_logo_file_uri(settings: dict | None) -> str | None:
    url = company_logo_url_from_settings(settings)
    if url:
        return url
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
    """Browser-safe logo URL (Cloudinary HTTPS or legacy static path)."""
    url = company_logo_url_from_settings(settings)
    if url:
        return url
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


def refresh_invoice_payment_aggregate(cursor: object, invoice_id: int, company_id: int) -> None:
    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS s FROM invoice_payments
        WHERE invoice_id=%s AND company_id=%s
        """,
        (invoice_id, company_id),
    )
    paid_sum = int(cursor.fetchone()["s"] or 0)
    cursor.execute(
        "SELECT total FROM invoices WHERE id=%s AND company_id=%s",
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
        "UPDATE invoices SET amount_paid=%s, payment_status=%s WHERE id=%s AND company_id=%s",
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
    cid = int(inv.get("company_id") or 0)
    created_raw = str(inv.get("created_at") or "")
    created = created_raw[:10] if created_raw else "—"
    left_fields = [
        ("No.", str(inv.get("invoice_number") or "")),
        ("Date created", created),
        ("Due date", str(inv.get("due_date") or "—")),
    ]
    story.extend(pdf_document_header_flowables("INVOICE", left_fields, settings, cid, styles))

    client_name = str(inv.get("client_name") or "—")
    client_prof = client_profile_by_name(cid, client_name) if cid else None
    story.extend(pdf_client_section_flowables(client_name, client_prof, styles))

    quote_row = None
    qid = inv.get("quote_id")
    if cid and qid:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM quotes WHERE id=%s AND company_id=%s", (int(qid), cid))
            quote_row = cur.fetchone()
    if quote_row:
        qd = dict(quote_row)
        story.extend(
            pdf_job_section_flowables(
                qd.get("job_name"),
                qd.get("site_location"),
                qd.get("start_date"),
                qd.get("end_date"),
                qd.get("special_notes"),
                styles,
            )
        )

    story.extend(pdf_equipment_table_flowables(line_items, styles, quote_style=False))
    transport = transport_display_from_row(inv)
    story.extend(pdf_transport_section_flowables(transport, styles))
    story.extend(pdf_personnel_section_flowables(line_items, styles))

    fin = invoice_financials_from_row(inv, line_items)
    transport = transport_display_from_row(inv)
    fin["transport_amount"] = transport["transport_amount"]
    fin["transport_type"] = transport["transport_type"]
    fin["transport_description"] = transport["transport_description"]
    fin["equipment_subtotal"] = sum(
        int(l.get("line_total", 0) or 0)
        for l in line_items
        if str(l.get("line_type") or "").lower() not in ("personnel", "function", "technician")
    )
    inv_col_widths = [112 * mm, 14 * mm, 26 * mm, 26 * mm]
    story.extend(
        pdf_totals_table_flowables(
            fin,
            styles,
            line_items_width=sum(inv_col_widths),
            amount_paid=amount_paid,
            remaining=remaining,
        )
    )

    inv_terms = str(inv.get("quote_terms") or "").strip()
    if not inv_terms and quote_row:
        inv_terms = str(dict(quote_row).get("quote_terms") or "").strip()
    story.extend(pdf_terms_section_flowables(inv_terms, styles))
    story.extend(pdf_banking_section_flowables(settings, styles))

    ps = (payment_status or "unpaid").lower()
    if ps == "paid":
        status_label, color_hex = "PAID", "#166534"
    elif ps == "partial":
        status_label, color_hex = "PARTIAL", "#c2410c"
    else:
        status_label, color_hex = "UNPAID", "#b91c1c"
    story.append(
        Paragraph(
            f'<para align="center"><b><font size="14" color="{color_hex}">Payment status: {status_label}</font></b></para>',
            styles["Normal"],
        )
    )
    story.extend(pdf_thank_you_footer_flowables(settings, styles))

    doc.build(story)
    return buf.getvalue()


def client_contact_from_directory(company_id: int, client_name: str | None) -> str:
    """Best-effort client contact from clients table; empty if not found or no extra columns."""
    name = (client_name or "").strip()
    if not name:
        return ""
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT * FROM clients WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
            (company_id, name),
        )
        row = cursor.fetchone()
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


def send_password_reset_email(to_email, reset_link):
    api_key = os.environ.get("SENDGRID_API_KEY")
    sender = os.environ.get("SENDER_EMAIL")
    print(f"SENDGRID KEY EXISTS: {bool(api_key)}", flush=True)
    print(f"SENDGRID KEY LENGTH: {len(api_key) if api_key else 0}", flush=True)
    print(f"SENDER: {sender}", flush=True)
    print(f"RECIPIENT: {to_email}", flush=True)
    if not api_key or not sender:
        print("SENDGRID NOT CONFIGURED - SKIPPING", flush=True)
        return False
    try:
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": sender},
                "subject": "Password Reset - GearGrid",
                "content": [
                    {
                        "type": "text/plain",
                        "value": (
                            f"Click this link to reset your password:\n\n{reset_link}\n\n"
                            "This link expires in 1 hour."
                        ),
                    }
                ],
            },
            timeout=10,
        )
        print(f"SENDGRID STATUS: {response.status_code}", flush=True)
        print(f"SENDGRID BODY: {response.text}", flush=True)
        return response.status_code == 202
    except Exception as e:
        print(f"SENDGRID EXCEPTION: {str(e)}", flush=True)
        return False


def fetch_reset_token_row(cursor: object, token: str) -> dict | None:
    cursor.execute(
        "SELECT id, user_id, expires_at, used FROM password_reset_tokens WHERE token=%s",
        (token,),
    )
    return cursor.fetchone()


def reset_token_error_message(row: dict | None) -> str | None:
    if not row:
        return "This reset link is invalid or has expired. Please request a new one."
    if row["used"]:
        return "This reset link has already been used. Please request a new one."
    if datetime.utcnow() >= datetime.fromisoformat(row["expires_at"]):
        return "This reset link has expired. Please request a new one."
    return None


def create_session(response: RedirectResponse, user_id: int) -> None:
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
        cursor.execute("INSERT INTO sessions (token, user_id, csrf_token, expires_at) VALUES (%s, %s, %s, %s)", (token, user_id, csrf_token, expires_at))
        response.set_cookie("session_token", token, httponly=True, samesite="lax", secure=SESSION_SECURE_COOKIE, max_age=SESSION_DAYS * 24 * 3600)
    
    
def current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT u.id as user_id, u.full_name, u.email, u.company_id, u.role, u.must_change_password, c.name as company_record_name, cs.company_name, s.csrf_token
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            JOIN companies c ON c.id = u.company_id
            LEFT JOIN company_settings cs ON cs.company_id = u.company_id
            WHERE s.token=%s
            """,
            (token,),
        )
        user = cursor.fetchone()
        if not user:
            return None
        cursor.execute("SELECT expires_at FROM sessions WHERE token=%s", (token,))
        expiry = cursor.fetchone()
        if not expiry or datetime.fromisoformat(expiry["expires_at"]) < datetime.utcnow():
            cursor.execute("DELETE FROM sessions WHERE token=%s", (token,))
            return None
        user_dict = dict(user)
        if not user_dict.get("csrf_token"):
            new_csrf = secrets.token_urlsafe(24)
            cursor.execute("UPDATE sessions SET csrf_token=%s WHERE token=%s", (new_csrf, token))
            user_dict["csrf_token"] = new_csrf
        try:
            cid_nav = int(user_dict["company_id"])
            cursor.execute("SELECT logo_url FROM companies WHERE id=%s", (cid_nav,))
            co_row = cursor.fetchone()
            logo_url = str(co_row["logo_url"]).strip() if co_row and co_row.get("logo_url") else ""
            if logo_url.startswith("http://") or logo_url.startswith("https://"):
                user_dict["logo_url"] = logo_url
            else:
                user_dict["logo_url"] = None
            local_path = company_static_logo_path(cid_nav)
            local_href = f"/static/logos/company_{cid_nav}.png" if local_path.is_file() else None
            user_dict["logo_display_url"] = user_dict.get("logo_url") or local_href
            user_dict["has_company_logo"] = bool(user_dict.get("logo_display_url"))
        except (TypeError, ValueError, KeyError):
            user_dict["logo_url"] = None
            user_dict["logo_display_url"] = None
            user_dict["has_company_logo"] = False
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM companies WHERE id=%s", (company_id,))
        co = cursor.fetchone()
        cursor.execute("SELECT * FROM company_settings WHERE company_id=%s", (company_id,))
        cs = cursor.fetchone()
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
            logo_url = str(cod.get("logo_url") or "").strip()
            out["logo_url"] = logo_url if logo_url.startswith(("http://", "https://")) else ""
        out.setdefault("quote_footer", "")
        out.setdefault("logo_url", "")
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
        out["logo_display_url"] = company_logo_web_path(out)
        return out
    
    
def company_banking_configured(settings: dict) -> bool:
    return bool((settings.get("bank_name") or "").strip() and (settings.get("bank_account_number") or "").strip())


PDF_NAVY = colors.HexColor(PDF_NAVY_HEX)
PDF_GRID = colors.HexColor("#d9dce3")


def pdf_section_divider() -> list:
    line_table = Table([[""]], colWidths=[169 * mm], rowHeights=[2])
    line_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.75, PDF_GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [Spacer(1, 8), line_table, Spacer(1, 8)]


def pdf_section_heading(label: str, styles) -> Paragraph:
    return Paragraph(
        f'<font color="{PDF_NAVY_HEX}"><b>{escape(label)}</b></font>',
        ParagraphStyle(
            name=f"PdfHeading_{label[:12]}",
            parent=styles["Normal"],
            fontSize=10,
            fontName="Helvetica-Bold",
            spaceAfter=6,
            spaceBefore=0,
        ),
    )


def pdf_company_details_paragraph(settings: dict, styles, *, align_right: bool = False) -> Paragraph:
    cn = escape(str(settings.get("company_name") or "Company"))
    parts = [f"<b><font size='12'>{cn}</font></b>"]
    if settings.get("tagline"):
        parts.append(escape(str(settings["tagline"]).strip()))
    if settings.get("email"):
        parts.append(f"Email: {escape(str(settings['email']).strip())}")
    if settings.get("phone"):
        parts.append(f"Phone: {escape(str(settings['phone']).strip())}")
    if settings.get("address"):
        parts.append(f"Address: {escape(str(settings['address']).strip())}")
    if settings.get("vat_number"):
        parts.append(f"VAT/Reg: {escape(str(settings['vat_number']).strip())}")
    para_style = styles["Normal"]
    if align_right:
        para_style = ParagraphStyle(
            name="PdfCompanyDetailsRight",
            parent=styles["Normal"],
            alignment=TA_RIGHT,
        )
    return Paragraph("<br/>".join(parts), para_style)


def pdf_document_header_flowables(
    doc_title: str,
    left_fields: list[tuple[str, str]],
    settings: dict,
    company_id: int,
    styles,
) -> list:
    """Quote/invoice header: document info left, logo and company details right."""
    left_lines = [f'<b><font size="16" color="{PDF_NAVY_HEX}">{escape(doc_title)}</font></b>']
    for label, value in left_fields:
        left_lines.append(f"<b>{escape(label)}</b> {escape(str(value or '—'))}")
    left_para = Paragraph("<br/>".join(left_lines), styles["Normal"])
    logo_flow = pdf_company_logo_flowable(company_id) if company_id else None
    company_para = pdf_company_details_paragraph(settings, styles, align_right=True)
    right_rows: list[list] = []
    if logo_flow:
        right_rows.append([logo_flow])
    right_rows.append([company_para])
    right_cell: object = Table(right_rows, colWidths=[84 * mm])
    right_style: list = [
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]
    if logo_flow:
        right_style.append(("BOTTOMPADDING", (0, 0), (-1, 0), 4))
    right_cell.setStyle(TableStyle(right_style))
    header = Table([[left_para, right_cell]], colWidths=[85 * mm, 84 * mm])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [header, Spacer(1, 6)] + pdf_section_divider()


def pdf_client_section_flowables(client_name: str, profile: dict | None, styles) -> list:
    parts = [f"<b>{escape(str(client_name or '').strip() or '—')}</b>"]
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
    return [pdf_section_heading("CLIENT", styles), Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 6)] + pdf_section_divider()


def pdf_job_section_flowables(
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
    parts: list[str] = []
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
    return [pdf_section_heading("JOB DETAILS", styles), Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 6)] + pdf_section_divider()


def pdf_personnel_lines(lines: list) -> list[dict]:
    out = []
    for line in lines:
        if str(line.get("line_type") or "").lower() in ("personnel", "function", "technician"):
            out.append(line)
    return out


def pdf_equipment_lines_for_table(lines: list, quote_style: bool) -> list[list]:
    personnel_types = {"personnel", "function", "technician"}
    rows: list[list] = []
    for line in lines:
        if str(line.get("line_type") or "").lower() in personnel_types:
            continue
        if quote_style and line.get("equipment_id") is None and quote_line_is_sub_rental(line) and not show_sub_rental_on_client_pdf(line):
            continue
        name = str(line.get("name", "—"))
        try:
            qty = int(line.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        try:
            unit_price = int(line.get("unit_price", 0) or 0)
        except (TypeError, ValueError):
            unit_price = 0
        try:
            line_total = int(line.get("line_total", 0) or 0)
        except (TypeError, ValueError):
            line_total = 0
        if quote_style:
            try:
                days = int(line.get("days", 1) or 1)
            except (TypeError, ValueError):
                days = 1
            rows.append([str(qty), name, f"R{unit_price}", str(days), f"R{line_total}"])
        else:
            rows.append([name, str(qty), f"R{unit_price}", f"R{line_total}"])
    return rows


def pdf_equipment_table_flowables(lines: list, styles, *, quote_style: bool = False) -> list:
    cell_style = ParagraphStyle(
        name="PdfEquipCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    body_rows = pdf_equipment_lines_for_table(lines, quote_style)
    if quote_style:
        headers = ["Qty", "Equipment / Description", "Unit Price", "Days", "Line Total"]
        col_widths = [12 * mm, 104 * mm, 26 * mm, 12 * mm, 24 * mm]
        name_col = 1
    else:
        headers = ["Equipment", "Qty", "Unit price", "Line total"]
        col_widths = [112 * mm, 14 * mm, 26 * mm, 26 * mm]
        name_col = 0
    table_rows: list[list] = [headers]
    if not body_rows:
        empty = Paragraph(escape("No equipment line items."), cell_style)
        if quote_style:
            table_rows.append(["—", empty, "", "", ""])
        else:
            table_rows.append([empty, "—", "R0", "R0"])
    else:
        for br in body_rows:
            if quote_style:
                table_rows.append(
                    [br[0], Paragraph(escape(str(br[1])), cell_style), br[2], br[3], br[4]]
                )
            else:
                table_rows.append(
                    [Paragraph(escape(str(br[0])), cell_style), br[1], br[2], br[3]]
                )
    items_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
    tbl_style: list = [
        ("BACKGROUND", (0, 0), (-1, 0), PDF_NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (name_col, 0), (name_col, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, PDF_GRID),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if quote_style:
        tbl_style.insert(5, ("ALIGN", (0, 0), (0, -1), "CENTER"))
    else:
        tbl_style.insert(5, ("ALIGN", (1, 0), (1, -1), "CENTER"))
    items_table.setStyle(TableStyle(tbl_style))
    return [pdf_section_heading("EQUIPMENT", styles), items_table, Spacer(1, 6)] + pdf_section_divider()


def pdf_transport_section_flowables(transport: dict, styles) -> list:
    if str(transport.get("transport_type") or "none") == "none":
        return []
    try:
        amount = int(transport.get("transport_amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return []
    desc = str(transport.get("transport_description") or "").strip()
    parts = [f"<b>Transport &amp; Delivery — R{amount}</b>"]
    if desc:
        parts.append(escape(desc))
    return [pdf_section_heading("TRANSPORT & DELIVERY", styles), Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 6)] + pdf_section_divider()


def pdf_personnel_section_flowables(lines: list, styles) -> list:
    personnel = pdf_personnel_lines(lines)
    if not personnel:
        return []
    cell_style = ParagraphStyle(
        name="PdfPersonnelCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    # Match EQUIPMENT table full width (178mm): Function widest, Qty/Days small, Rate/Line total medium
    col_widths = [104 * mm, 12 * mm, 12 * mm, 26 * mm, 24 * mm]
    rows = [["Function", "Qty", "Days", "Rate", "Line total"]]
    for line in personnel:
        try:
            days = int(line.get("days", 1) or 1)
        except (TypeError, ValueError):
            days = 1
        rows.append(
            [
                Paragraph(escape(str(line.get("name") or line.get("role") or "—")), cell_style),
                str(line.get("qty", 1)),
                str(days),
                f"R{int(line.get('unit_price', 0) or 0)}",
                f"R{int(line.get('line_total', 0) or 0)}",
            ]
        )
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PDF_NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, PDF_GRID),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return [pdf_section_heading("PERSONNEL", styles), tbl, Spacer(1, 6)] + pdf_section_divider()


def pdf_totals_table_flowables(
    fin: dict,
    styles,
    *,
    line_items_width: float,
    amount_paid: int | None = None,
    remaining: int | None = None,
) -> list:
    red_hex = "#b91c1c"
    amt_style = ParagraphStyle(name="PdfTotAmt", parent=styles["Normal"], fontSize=10, alignment=TA_RIGHT)
    disc_lbl = ParagraphStyle(name="PdfDiscLbl", parent=styles["Normal"], textColor=colors.HexColor(red_hex), fontSize=10)
    disc_amt = ParagraphStyle(name="PdfDiscAmt", parent=styles["Normal"], textColor=colors.HexColor(red_hex), fontSize=10, alignment=TA_RIGHT)
    grand_lbl = ParagraphStyle(name="PdfGrandLbl", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10)
    grand_amt = ParagraphStyle(name="PdfGrandAmt", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10, alignment=TA_RIGHT)

    equipment_sub = fin.get("equipment_subtotal")
    transport_amt = int(fin.get("transport_amount") or 0)
    summary_rows: list[list] = []
    if transport_amt > 0 and equipment_sub is not None:
        summary_rows.append(["Equipment subtotal", Paragraph(escape(f"R{int(equipment_sub)}"), amt_style)])
        summary_rows.append(
            ["Transport & Delivery", Paragraph(escape(f"R{transport_amt}"), amt_style)]
        )
    summary_rows.append(["Subtotal", Paragraph(escape(f"R{int(fin['subtotal'])}"), amt_style)])
    if int(fin.get("discount_amount") or 0) > 0:
        dp = fin["discount_percent"]
        summary_rows.append(
            [
                Paragraph(escape(f"Discount ({dp:g}%)"), disc_lbl),
                Paragraph(escape(f"R{int(fin['discount_amount'])}"), disc_amt),
            ]
        )
    if fin.get("vat_enabled") and int(fin.get("vat_amount") or 0) > 0:
        vp = fin["vat_percent"]
        summary_rows.append(
            [
                Paragraph(escape(f"VAT ({vp:g}%)"), styles["Normal"]),
                Paragraph(escape(f"R{int(fin['vat_amount'])}"), amt_style),
            ]
        )
    summary_rows.append(
        [
            Paragraph("Grand total", grand_lbl),
            Paragraph(escape(f"R{int(fin['grand_total'])}"), grand_amt),
        ]
    )
    if amount_paid is not None:
        summary_rows.append(["Amount paid", Paragraph(escape(f"R{amount_paid}"), amt_style)])
    if remaining is not None:
        summary_rows.append(["Remaining balance", Paragraph(escape(f"R{remaining}"), amt_style)])

    amt_col_w = 52 * mm
    label_col_w = line_items_width - amt_col_w
    summary_tbl = Table(summary_rows, colWidths=[label_col_w, amt_col_w])
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
                ("LINEABOVE", (0, 0), (-1, 0), 0.75, PDF_GRID),
            ]
        )
    )
    return [pdf_section_heading("TOTALS", styles), summary_tbl, Spacer(1, 6)] + pdf_section_divider()


def pdf_banking_section_flowables(settings: dict, styles) -> list:
    if not company_banking_configured(settings):
        return []
    parts = [
        f"Bank: {escape(str(settings['bank_name']).strip())}",
        f"Account Holder: {escape(str(settings.get('bank_account_holder') or '').strip())}",
        f"Account Number: {escape(str(settings['bank_account_number']).strip())}",
        f"Account Type: {escape(str(settings.get('bank_account_type') or '').strip())}",
        f"Branch Code: {escape(str(settings.get('bank_branch_code') or '').strip())}",
    ]
    ref = str(settings.get("bank_reference") or "").strip()
    if ref:
        parts.append(f"Reference: {escape(ref)}")
    return [pdf_section_heading("BANKING DETAILS", styles), Paragraph("<br/>".join(parts), styles["Normal"]), Spacer(1, 6)] + pdf_section_divider()


def pdf_terms_section_flowables(terms: str | None, styles) -> list:
    text = str(terms or "").strip()
    if not text:
        return []
    body = escape(text).replace("\n", "<br/>")
    return [pdf_section_heading("TERMS & CONDITIONS", styles), Paragraph(body, styles["Normal"]), Spacer(1, 6)] + pdf_section_divider()


def pdf_thank_you_footer_flowables(settings: dict, styles) -> list:
    footer = str(settings.get("quote_footer") or "").strip()
    if not footer:
        footer = "Thank you for your business."
    return [
        Spacer(1, 10),
        Paragraph(
            f'<para align="center"><i>{escape(footer)}</i></para>',
            styles["Italic"],
        ),
    ]


def pdf_banking_detail_flowables(settings: dict, styles) -> list:
    return pdf_banking_section_flowables(settings, styles)


def client_row_to_dict(row: dict | None) -> dict | None:
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT * FROM clients WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
            (company_id, name),
        )
        row = cursor.fetchone()
        return client_row_to_dict(row)
    
    
def client_profile_by_id(company_id: int, client_id: int) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM clients WHERE id=%s AND company_id=%s", (client_id, company_id))
        row = cursor.fetchone()
        return client_row_to_dict(row)
    
    
def pdf_client_details_flowables(client_name: str, profile: dict | None, styles) -> list:
    return pdf_client_section_flowables(client_name, profile, styles)


def pdf_job_details_flowables(
    job_name: str | None,
    site_location: str | None,
    start_date: str | None,
    end_date: str | None,
    special_notes: str | None,
    styles,
) -> list:
    return pdf_job_section_flowables(job_name, site_location, start_date, end_date, special_notes, styles)


def pdf_terms_flowables(terms: str | None, styles) -> list:
    return pdf_terms_section_flowables(terms, styles)


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
        fin = {
            "subtotal": int(qdict["subtotal"]),
            "discount_percent": float(qdict.get("discount_percent") or 0),
            "discount_amount": int(qdict.get("discount_amount") or 0),
            "vat_enabled": bool(int(qdict.get("vat_enabled") or 0)),
            "vat_percent": float(qdict.get("vat_percent") or 15),
            "vat_amount": int(qdict.get("vat_amount") or 0),
            "grand_total": int(qdict.get("grand_total") or qdict.get("total") or 0),
        }
    else:
        st = invoice_line_subtotal(lines) + transport_display_from_row(qdict)["transport_amount"]
        fin = compute_financial_totals(st, 0.0, False, 15.0)
    transport = transport_display_from_row(qdict)
    fin["equipment_subtotal"] = sum(
        int(l.get("line_total", 0) or 0)
        for l in lines
        if str(l.get("line_type") or "").lower() not in ("personnel", "function", "technician")
    )
    fin["transport_amount"] = transport["transport_amount"]
    fin["transport_type"] = transport["transport_type"]
    fin["transport_description"] = transport["transport_description"]
    return fin


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


def _pdf_logo_flowable_from_image_source(source: object) -> RLImage | None:
    try:
        ir = ImageReader(source)
        iw, ih = ir.getSize()
        if iw <= 0 or ih <= 0:
            return None
        max_h_pt = 60.0
        max_w_pt = 200.0
        scale = min(max_w_pt / float(iw), max_h_pt / float(ih), 1.0)
        dw = iw * scale
        dh = ih * scale
        return RLImage(source, width=dw, height=dh)
    except Exception:
        return None


def pdf_company_logo_flowable(company_id: int) -> RLImage | None:
    try:
        with get_db() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("SELECT logo_url FROM companies WHERE id=%s", (company_id,))
            row = cursor.fetchone()
        logo_url = str(row["logo_url"]).strip() if row and row.get("logo_url") else ""
        if logo_url.startswith(("http://", "https://")):
            try:
                resp = requests.get(logo_url, timeout=15)
                resp.raise_for_status()
                buf = BytesIO(resp.content)
                flow = _pdf_logo_flowable_from_image_source(buf)
                if flow:
                    return flow
            except Exception:
                logger.exception("Could not fetch Cloudinary logo for PDF company_id=%s", company_id)
        path = company_static_logo_path(company_id)
        if path.is_file():
            return _pdf_logo_flowable_from_image_source(str(path))
    except Exception:
        logger.exception("Could not load company logo for PDF company_id=%s", company_id)
    return None


def _company_logo_png_buffer(raw: bytes) -> BytesIO | None:
    try:
        from PIL import Image

        img = Image.open(BytesIO(raw))
        img = img.convert("RGBA")
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img.thumbnail((300, 300), resample)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


def _save_company_logo_local_file(company_id: int, png_buf: BytesIO) -> None:
    (BASE_DIR / "static" / "logos").mkdir(parents=True, exist_ok=True)
    dest = company_static_logo_path(company_id)
    with open(dest, "wb") as f:
        f.write(png_buf.getvalue())


def _delete_company_logo_local_file(company_id: int) -> None:
    try:
        path = company_static_logo_path(company_id)
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def save_company_logo_upload(company_id: int, upload: UploadFile | None) -> str | None:
    """Upload logo to Cloudinary when configured; otherwise save locally. Returns error message or None."""
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
    png_buf = _company_logo_png_buffer(raw)
    if png_buf is None:
        return "Could not process logo image"
    if cloudinary_configured():
        try:
            import cloudinary
            import cloudinary.uploader

            png_buf.seek(0)
            cloudinary.config(
                cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip(),
                api_key=os.environ.get("CLOUDINARY_API_KEY", "").strip(),
                api_secret=os.environ.get("CLOUDINARY_API_SECRET", "").strip(),
                secure=True,
            )
            result = cloudinary.uploader.upload(
                png_buf,
                folder="geargridsa/logos",
                public_id=f"company_{company_id}",
                overwrite=True,
                resource_type="image",
                format="png",
            )
            logo_url = str(result.get("secure_url") or result.get("url") or "").strip()
            if not logo_url:
                return "Cloudinary did not return a logo URL"
            with get_db() as conn:
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute("UPDATE companies SET logo_url=%s WHERE id=%s", (logo_url, company_id))
            _delete_company_logo_local_file(company_id)
            return None
        except Exception:
            logger.exception("Logo upload failed company_id=%s", company_id)
            return "Could not upload logo image"
    try:
        _save_company_logo_local_file(company_id, png_buf)
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("INSERT INTO companies (name, created_at) VALUES (%s, %s) RETURNING id", (clean_company, datetime.utcnow().isoformat()))
            company_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO users (company_id, full_name, email, password_hash, password_salt, role, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (company_id, clean_full_name, clean_email, hash_password(password, "bcrypt"), "bcrypt", "admin", datetime.utcnow().isoformat()),
            )
            cursor.execute(
                """
                INSERT INTO company_settings
                (company_id, company_name, tagline, email, phone, address, vat_number, quote_footer)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (company_id, clean_company, "Professional Equipment Rentals", clean_email, "", "", "", "Thank you for your business."),
            )
            cursor.execute(
                """
                UPDATE companies
                SET tagline=%s, email=%s, vat_percent=15, vat_enabled=0, default_discount_percent=0
                WHERE id=%s
                """,
                ("Professional Equipment Rentals", clean_email, company_id),
            )
            seed_default_equipment_categories(cursor, company_id)
            seed_default_technician_functions(cursor, company_id)
        except psycopg2.IntegrityError:
            conn.rollback()
            return RedirectResponse(url="/register?error=Company%20or%20email%20already%20exists", status_code=303)
        cursor.execute("SELECT id FROM users WHERE email=%s", (clean_email,))
        user = cursor.fetchone()
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
    submitted_email = email.strip().lower()
    logging.warning(
        f"Forgot password triggered for email: {submitted_email}, SendGrid configured: {bool(os.environ.get('SENDGRID_API_KEY'))}"
    )
    print("CHECKPOINT 1 - about to query user", flush=True)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id, company_id, email FROM users WHERE email=%s", (submitted_email,))
        user = cursor.fetchone()
        print(f"CHECKPOINT 2 - user found: {user is not None}", flush=True)
        if user:
            cursor.execute("SELECT id FROM password_reset_requests WHERE user_id=%s", (user["id"],))
            existing = cursor.fetchone()
            if not existing:
                cursor.execute(
                    "INSERT INTO password_reset_requests (user_id, company_id, requested_at) VALUES (%s, %s, %s)",
                    (user["id"], user["company_id"], datetime.utcnow().isoformat()),
                )
            if sendgrid_configured():
                token = secrets.token_urlsafe(32)
                expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
                reset_link = f"{BASE_URL}/reset-password/{token}"
                print("CHECKPOINT 3 - entering try block", flush=True)
                try:
                    cursor.execute(
                        "INSERT INTO password_reset_tokens (user_id, token, expires_at, used) VALUES (%s, %s, %s, 0)",
                        (user["id"], token, expires_at),
                    )
                except Exception as e:
                    print(f"FORGOT PASSWORD ERROR: {str(e)}", flush=True)
                email_sent = send_password_reset_email(user["email"], reset_link)
                print(f"EMAIL SENT RESULT: {email_sent}", flush=True)
        return RedirectResponse(url="/forgot-password?submitted=1", status_code=303)
    
    
@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str):
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        row = fetch_reset_token_row(cursor, token)
        error = reset_token_error_message(row)
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"title": "Reset Password", "error": error, "token": None if error else token},
        )
    
    
@app.post("/reset-password/{token}")
def reset_password_submit(request: Request, token: str, password: str = Form(...), confirm_password: str = Form(...)):
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        row = fetch_reset_token_row(cursor, token)
        error = reset_token_error_message(row)
        if error:
            return templates.TemplateResponse(
                request=request,
                name="reset_password.html",
                context={"title": "Reset Password", "error": error, "token": None},
            )
        if len(password) < 8:
            return RedirectResponse(url=f"/reset-password/{token}?error=Password%20must%20be%20at%20least%208%20characters", status_code=303)
        if password != confirm_password:
            return RedirectResponse(url=f"/reset-password/{token}?error=Passwords%20do%20not%20match", status_code=303)
        cursor.execute(
            "UPDATE users SET password_hash=%s, password_salt='bcrypt', must_change_password=0 WHERE id=%s",
            (hash_password(password, "bcrypt"), row["user_id"]),
        )
        cursor.execute("UPDATE password_reset_tokens SET used=1 WHERE id=%s", (row["id"],))
        cursor.execute("DELETE FROM password_reset_requests WHERE user_id=%s", (row["user_id"],))
        return RedirectResponse(
            url="/login?success=Password%20reset%20successfully.%20Please%20log%20in.",
            status_code=303,
        )
    
    
@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    clean_email = email.strip().lower()
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE email=%s", (clean_email,))
        user = cursor.fetchone()
        if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
            return RedirectResponse(url="/login?error=Invalid%20credentials", status_code=303)
        if user["password_salt"] != "bcrypt":
            cursor.execute("UPDATE users SET password_hash=%s, password_salt=%s WHERE id=%s", (hash_password(password, "bcrypt"), "bcrypt", user["id"]))
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "UPDATE users SET password_hash=%s, password_salt='bcrypt', must_change_password=0 WHERE id=%s",
            (hash_password(password, "bcrypt"), user["user_id"]),
        )
        return RedirectResponse(url="/", status_code=303)
    
    
@app.post("/logout")
async def logout(request: Request):
    user = current_user(request)
    if user and not await validate_csrf(request, user):
        return RedirectResponse(url="/?error=Invalid%20security%20token", status_code=303)
    token = request.cookies.get("session_token")
    if token:
        with get_db() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("DELETE FROM sessions WHERE token=%s", (token,))
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM equipment WHERE company_id=%s ORDER BY id DESC", (user["company_id"],))
        all_items = cursor.fetchall()
        today = datetime.now().strftime("%Y-%m-%d")
        total_count = sum(qty_total(r) for r in all_items)
        available_count = sum(qty_available(r) for r in all_items)
        rented_count = sum(qty_rented(r) for r in all_items)
        overdue_count = sum(
            (qty_rented(r) if (r["due_date"] and r["due_date"] < today and qty_rented(r) > 0) else 0) for r in all_items
        )
        items = list(all_items)
        show_onboarding = len(all_items) == 0
        cursor.execute(
            "SELECT * FROM rental_history WHERE company_id=%s ORDER BY id DESC",
            (user["company_id"],),
        )
        history_rows = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            "SELECT * FROM sub_rentals WHERE company_id=%s ORDER BY id DESC",
            (user["company_id"],),
        )
        sub_rental_rows = cursor.fetchall()
        settings = get_company_settings(user["company_id"])
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={
                "title": f"{settings.get('company_name', 'Dashboard')} Dashboard",
                "items": items,
                "today": today,
                "current_user": user,
                "equipment_categories": [c["name"] for c in list_equipment_categories(user["company_id"])],
                "show_onboarding": show_onboarding,
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id, full_name, email, role, created_at FROM users WHERE company_id=%s ORDER BY id DESC", (user["company_id"],))
        users = cursor.fetchall()
        cursor.execute(
            """
            SELECT pr.id, pr.user_id, pr.requested_at, u.full_name, u.email
            FROM password_reset_requests pr
            JOIN users u ON u.id = pr.user_id
            WHERE pr.company_id=%s
            ORDER BY pr.requested_at DESC
            """,
            (user["company_id"],),
        )
        reset_requests = cursor.fetchall()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                """
                INSERT INTO users (company_id, full_name, email, password_hash, password_salt, role, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user["company_id"], clean_name, clean_email, hash_password(password, "bcrypt"), "bcrypt", role, datetime.utcnow().isoformat()),
            )
        except psycopg2.IntegrityError:
            conn.rollback()
            return RedirectResponse(url="/users?error=Email%20already%20exists", status_code=303)
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("UPDATE users SET role=%s WHERE id=%s AND company_id=%s", (role, user_id, user["company_id"]))
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE id=%s AND company_id=%s)", (user_id, user["company_id"]))
        cursor.execute("DELETE FROM users WHERE id=%s AND company_id=%s", (user_id, user["company_id"]))
        return RedirectResponse(url="/users?saved=1", status_code=303)
    
    
@app.post("/users/{user_id}/reset-password")
async def users_reset_password(request: Request, user_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/users", user)

    temp_password = secrets.token_urlsafe(8)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id FROM users WHERE id=%s AND company_id=%s", (user_id, user["company_id"]))
        target = cursor.fetchone()
        if not target:
            return RedirectResponse(url="/users?error=User%20not%20found", status_code=303)
    
        cursor.execute(
            "UPDATE users SET password_hash=%s, password_salt='bcrypt', must_change_password=1 WHERE id=%s AND company_id=%s",
            (hash_password(temp_password, "bcrypt"), user_id, user["company_id"]),
        )
        cursor.execute("DELETE FROM password_reset_requests WHERE user_id=%s AND company_id=%s", (user_id, user["company_id"]))
        return RedirectResponse(url=f"/users?saved=1&temp_password={temp_password}", status_code=303)
    
    
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    settings = get_company_settings(user["company_id"])
    has_logo = company_has_logo(settings, user["company_id"])
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "Company Settings",
            "settings": settings,
            "has_logo": has_logo,
            "logo_url": settings.get("logo_display_url"),
            "company_id": user["company_id"],
            "current_user": user,
            "equipment_categories": list_equipment_categories(user["company_id"]),
            "technician_functions": list_technician_functions(user["company_id"]),
        },
    )


@app.post("/settings/technician-functions/add")
async def settings_technician_function_add(
    request: Request, function_name: str = Form(...), day_rate: str = Form(default="0")
):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    name = function_name.strip()
    if not name:
        return RedirectResponse(url="/settings?error=Function%20name%20is%20required", status_code=303)
    try:
        rate = max(0, int(str(day_rate).strip() or 0))
    except ValueError:
        return RedirectResponse(url="/settings?error=Day%20rate%20must%20be%20a%20whole%20number", status_code=303)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                """
                INSERT INTO technician_functions (company_id, function_name, day_rate, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (user["company_id"], name, rate, datetime.utcnow().isoformat()),
            )
        except psycopg2.IntegrityError:
            conn.rollback()
            return RedirectResponse(url="/settings?error=Function%20already%20exists", status_code=303)
        return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/technician-functions/{function_id}/edit")
async def settings_technician_function_edit(
    request: Request, function_id: int, function_name: str = Form(...), day_rate: str = Form(...)
):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    name = function_name.strip()
    if not name:
        return RedirectResponse(url="/settings?error=Function%20name%20is%20required", status_code=303)
    try:
        rate = max(0, int(str(day_rate).strip() or 0))
    except ValueError:
        return RedirectResponse(url="/settings?error=Day%20rate%20must%20be%20a%20whole%20number", status_code=303)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            UPDATE technician_functions
            SET function_name=%s, day_rate=%s
            WHERE id=%s AND company_id=%s
            """,
            (name, rate, function_id, user["company_id"]),
        )
        if cursor.rowcount < 1:
            return RedirectResponse(url="/settings?error=Function%20not%20found", status_code=303)
        return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/technician-functions/{function_id}/delete")
async def settings_technician_function_delete(request: Request, function_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "DELETE FROM technician_functions WHERE id=%s AND company_id=%s",
            (function_id, user["company_id"]),
        )
        return RedirectResponse(url="/settings?saved=1", status_code=303)


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

    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            UPDATE companies
            SET name=%s, tagline=%s, email=%s, phone=%s, address=%s, vat_number=%s, vat_percent=%s, vat_enabled=%s, default_discount_percent=%s,
                bank_name=%s, bank_account_holder=%s, bank_account_number=%s, bank_account_type=%s, bank_branch_code=%s, bank_reference=%s,
                terms_and_conditions=%s
            WHERE id=%s
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
            SET company_name=%s, tagline=%s, email=%s, phone=%s, address=%s, vat_number=%s, quote_footer=%s
            WHERE company_id=%s
            """,
            (company_name, tagline, email, phone, address, vat_number, quote_footer, user["company_id"]),
        )
        return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/categories/add")
async def settings_category_add(request: Request, category_name: str = Form(...)):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    name = category_name.strip()
    if not name:
        return RedirectResponse(url="/settings?error=Category%20name%20is%20required", status_code=303)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                "INSERT INTO equipment_categories (company_id, name, created_at) VALUES (%s, %s, %s)",
                (user["company_id"], name, datetime.utcnow().isoformat()),
            )
        except psycopg2.IntegrityError:
            conn.rollback()
            return RedirectResponse(url="/settings?error=Category%20already%20exists", status_code=303)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/categories/{category_id}/delete")
async def settings_category_delete(request: Request, category_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/settings", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "DELETE FROM equipment_categories WHERE id=%s AND company_id=%s",
            (category_id, user["company_id"]),
        )
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT id, quote_number, client_name, quote_date, total, status, created_at
            FROM quotes
            WHERE company_id=%s
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
            WHERE j.company_id=%s
            ORDER BY j.id DESC
            """,
            (user["company_id"],),
        )
        jobs = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) AS c FROM quotes WHERE company_id=%s", (user["company_id"],))
        quote_total = int(cursor.fetchone()["c"] or 0)
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END), 0) AS a,
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS p,
                COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END), 0) AS r
            FROM quotes
            WHERE company_id=%s
            """,
            (user["company_id"],),
        )
        st = cursor.fetchone()
        approved_count = int(st["a"] or 0)
        pending_count = int(st["p"] or 0)
        rejected_count = int(st["r"] or 0)
        conversion_pct = round((approved_count / quote_total) * 100, 1) if quote_total else 0.0
        cursor.execute(
            "SELECT COALESCE(SUM(total), 0) AS t FROM quotes WHERE company_id=%s AND status='approved'",
            (user["company_id"],),
        )
        approved_value = int(cursor.fetchone()["t"] or 0)
        if has_column(cursor, "invoices", "amount_paid"):
            cursor.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN payment_status IN ('unpaid', 'partial')
                    THEN (total - COALESCE(amount_paid, 0)) ELSE 0 END), 0) AS o
                FROM invoices WHERE company_id=%s
                """,
                (user["company_id"],),
            )
            outstanding_invoices = int(cursor.fetchone()["o"] or 0)
        else:
            outstanding_invoices = 0
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM quotes WHERE id=%s AND company_id=%s", (quote_id, user["company_id"]))
        qrow = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                "SELECT * FROM quotes WHERE id=%s AND company_id=%s",
                (quote_id, user["company_id"]),
            )
            quote_row = cursor.fetchone()
            if not quote_row or quote_row["status"] != "pending":
                conn.rollback()
                return RedirectResponse(url="/quotes/dashboard?error=Quote%20not%20found%20or%20already%20processed", status_code=303)
            try:
                lines = json.loads(quote_row["line_items_json"])
            except json.JSONDecodeError:
                lines = []
            ok, err = reserve_stock_for_quote_lines(cursor, user["company_id"], lines)
            if not ok:
                conn.rollback()
                qe = urlencode({"error": err or "Could not reserve stock"})
                return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
            ok2, err2 = reserve_sub_rental_stock_for_quote_lines(cursor, user["company_id"], lines)
            if not ok2:
                conn.rollback()
                qe = urlencode({"error": err2 or "Could not reserve sub-rental stock"})
                return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
            inv_num = next_invoice_number(cursor, user["company_id"])
            due_date = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
            created = datetime.utcnow().isoformat()
            fin_inv = quote_financials_from_saved_row(dict(quote_row), lines)
            qd = dict(quote_row)
            tr = transport_display_from_row(qd)
            cursor.execute(
                """
                INSERT INTO invoices (
                    company_id, invoice_number, quote_id, client_name, line_items_json, total, created_at, due_date, payment_status, amount_paid,
                    subtotal, discount_percent, discount_amount, vat_enabled, vat_percent, vat_amount, grand_total,
                    transport_type, transport_description, transport_amount
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'unpaid', 0, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
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
                    tr["transport_type"],
                    tr["transport_description"] or None,
                    tr["transport_amount"],
                ),
            )
            invoice_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO jobs (company_id, quote_id, invoice_id, client_name, job_date, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'upcoming', %s)
                RETURNING id
                """,
                (user["company_id"], quote_id, invoice_id, quote_row["client_name"], quote_row["quote_date"], created),
            )
            job_id = cursor.fetchone()["id"]
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
                        VALUES (%s, %s, NULL, %s, %s, 0, 'sub_rental', %s, %s, 0)
                        """,
                        (user["company_id"], job_id, desc or "Sub-rental", qty, sid, supplier),
                    )
                    cursor.execute(
                        """
                        INSERT INTO sub_rental_usage (company_id, sub_rental_id, job_id, units_used, show_on_quote)
                        VALUES (%s, %s, %s, %s, %s)
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
                        "SELECT id FROM equipment WHERE company_id=%s AND name=%s ORDER BY id ASC LIMIT 1",
                        (user["company_id"], name),
                    )
                    fr = cursor.fetchone()
                    eid = int(fr["id"]) if fr else None
                cursor.execute(
                    """
                    INSERT INTO job_prep_items (company_id, job_id, equipment_id, equipment_name, quantity, packed, line_type, sub_rental_id, supplier_name, received_from_supplier)
                    VALUES (%s, %s, %s, %s, %s, 0, 'owned', NULL, NULL, 0)
                    """,
                    (user["company_id"], job_id, eid, name, max(1, qty)),
                )
            cursor.execute("UPDATE quotes SET status='approved' WHERE id=%s AND company_id=%s", (quote_id, user["company_id"]))
        except Exception:
            conn.rollback()
            raise
        return RedirectResponse(url="/quotes/dashboard", status_code=303)
    
    
@app.post("/quotes/{quote_id}/reject")
async def quote_reject(request: Request, quote_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/quotes/dashboard", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT id FROM quotes WHERE id=%s AND company_id=%s AND status='pending'",
            (quote_id, user["company_id"]),
        )
        if not cursor.fetchone():
            return RedirectResponse(url="/quotes/dashboard?error=Quote%20not%20found%20or%20already%20processed", status_code=303)
        cursor.execute("UPDATE quotes SET status='rejected' WHERE id=%s AND company_id=%s", (quote_id, user["company_id"]))
        return RedirectResponse(url="/quotes/dashboard", status_code=303)
    
    
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT j.id, j.client_name, j.job_date, j.status, j.quote_id, q.quote_number
            FROM jobs j
            LEFT JOIN quotes q ON q.id = j.quote_id
            WHERE j.company_id=%s
            ORDER BY j.job_date DESC, j.id DESC
            """,
            (user["company_id"],),
        )
        jobs = cursor.fetchall()
        movements_by_job = movements_by_job_for_company(cursor, user["company_id"])
        return templates.TemplateResponse(
            request=request,
            name="jobs.html",
            context={
                "title": "Jobs",
                "jobs": jobs,
                "current_user": user,
                "job_statuses": ["upcoming", "active", "done"],
                "movements_by_job": movements_by_job,
            },
        )
    
    
@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_dashboard(request: Request):
    user, response = get_current_user(request, allowed_roles={"warehouse"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT j.id, j.client_name, j.job_date, j.status,
                   (SELECT COUNT(*) FROM job_prep_items jpi WHERE jpi.job_id = j.id AND jpi.company_id = j.company_id) AS item_count
            FROM jobs j
            WHERE j.company_id=%s AND j.status IN ('upcoming', 'active')
            ORDER BY j.job_date ASC, j.id ASC
            """,
            (user["company_id"],),
        )
        jobs = cursor.fetchall()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT * FROM jobs WHERE id=%s AND company_id=%s",
            (job_id, user["company_id"]),
        )
        job = cursor.fetchone()
        if not job:
            return render_message(request, "Not found", "Job not found.", "/warehouse" if user["role"] == "warehouse" else "/jobs", user)
        if user["role"] == "warehouse" and job["status"] not in ("upcoming", "active"):
            return render_message(request, "Not available", "This job is not available for prep.", "/warehouse", user)
        cursor.execute(
            "SELECT * FROM job_prep_items WHERE job_id=%s AND company_id=%s ORDER BY id ASC",
            (job_id, user["company_id"]),
        )
        prep_items = cursor.fetchall()
        movements = list_equipment_movements_for_job(cursor, user["company_id"], job_id)
        prep_states: dict[int, str] = {}
        for pi in prep_items:
            prep_states[int(pi["id"])] = prep_item_collection_state(movements, int(pi["id"]))
        show_sign_sections = job["status"] in ("upcoming", "active")
        back_url = "/warehouse" if user["role"] == "warehouse" else "/jobs"
        return templates.TemplateResponse(
            request=request,
            name="warehouse_job.html",
            context={
                "title": f"Prep — Job #{job_id}",
                "job": job,
                "prep_items": prep_items,
                "prep_states": prep_states,
                "movements": movements,
                "show_sign_sections": show_sign_sections,
                "conditions_out": sorted(EQUIPMENT_CONDITIONS_OUT),
                "conditions_in": sorted(EQUIPMENT_CONDITIONS_IN),
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("SELECT status FROM jobs WHERE id=%s AND company_id=%s", (job_id, user["company_id"]))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return RedirectResponse(url="/warehouse?error=Job%20not%20found", status_code=303)
            old = row["status"]
            if not warehouse_may_set_job_status(old, job_status):
                conn.rollback()
                return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20status%20change", status_code=303)
            ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
            if not ok:
                conn.rollback()
                qe = urlencode({"error": err or "Not enough units available"})
                return RedirectResponse(url=f"/warehouse/job/{job_id}?{qe}", status_code=303)
            cursor.execute("UPDATE jobs SET status=%s WHERE id=%s AND company_id=%s", (job_status, job_id, user["company_id"]))
            log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        except Exception:
            conn.rollback()
            raise
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("SELECT status FROM jobs WHERE id=%s AND company_id=%s", (job_id, user["company_id"]))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return RedirectResponse(url="/jobs?error=Job%20not%20found", status_code=303)
            old = row["status"]
            if old == job_status:
                conn.rollback()
                safe_next = redirect_to if redirect_to.startswith("/") and not redirect_to.startswith("//") else "/jobs"
                return RedirectResponse(url=safe_next, status_code=303)
            ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
            if not ok:
                conn.rollback()
                qe = urlencode({"error": err or "Not enough units available"})
                return RedirectResponse(url=f"/jobs?{qe}", status_code=303)
            cursor.execute("UPDATE jobs SET status=%s WHERE id=%s AND company_id=%s", (job_status, job_id, user["company_id"]))
            log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        except Exception:
            conn.rollback()
            raise
        safe_next = redirect_to if redirect_to.startswith("/") and not redirect_to.startswith("//") else "/jobs"
        return RedirectResponse(url=safe_next, status_code=303)
    
    
@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/toggle")
async def warehouse_prep_toggle(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT packed FROM job_prep_items WHERE id=%s AND job_id=%s AND company_id=%s",
            (prep_item_id, job_id, user["company_id"]),
        )
        row = cursor.fetchone()
        if not row:
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
        new_packed = 0 if row["packed"] else 1
        cursor.execute(
            "UPDATE job_prep_items SET packed=%s WHERE id=%s AND job_id=%s AND company_id=%s",
            (new_packed, prep_item_id, job_id, user["company_id"]),
        )
        return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)
    
    
@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/received")
async def warehouse_prep_received_toggle(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT received_from_supplier, line_type FROM job_prep_items WHERE id=%s AND job_id=%s AND company_id=%s",
            (prep_item_id, job_id, user["company_id"]),
        )
        row = cursor.fetchone()
        if not row or (row["line_type"] or "owned") != "sub_rental":
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
        new_val = 0 if int(row["received_from_supplier"] or 0) else 1
        cursor.execute(
            "UPDATE job_prep_items SET received_from_supplier=%s WHERE id=%s AND job_id=%s AND company_id=%s",
            (new_val, prep_item_id, job_id, user["company_id"]),
        )
        return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/warehouse/job/{job_id}/sign-out-all")
async def warehouse_equipment_sign_out_all(request: Request, job_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    form = await request.form()
    collected_by = str(form.get("collected_by_name", "")).strip()
    if not collected_by:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Collected%20by%20name%20is%20required", status_code=303)
    contact = str(form.get("collected_by_contact", "")).strip()
    job_notes = str(form.get("notes", "")).strip()
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT status FROM jobs WHERE id=%s AND company_id=%s",
            (job_id, user["company_id"]),
        )
        job = cursor.fetchone()
        if not job or job["status"] not in ("upcoming", "active"):
            return RedirectResponse(
                url=f"/warehouse/job/{job_id}?error=Sign-out%20not%20available%20for%20this%20job%20status",
                status_code=303,
            )
        cursor.execute(
            """
            SELECT id, equipment_id, equipment_name, line_type
            FROM job_prep_items
            WHERE job_id=%s AND company_id=%s
            ORDER BY id ASC
            """,
            (job_id, user["company_id"]),
        )
        prep_items = cursor.fetchall()
        movements = list_equipment_movements_for_job(cursor, user["company_id"], job_id)
        to_sign: list[tuple[dict, str]] = []
        for prep in prep_items:
            if (prep.get("line_type") or "owned") != "owned":
                continue
            prep_id = int(prep["id"])
            if prep_item_collection_state(movements, prep_id) == "out":
                continue
            condition_out = str(form.get(f"condition_out_{prep_id}", "")).strip()
            if condition_out not in EQUIPMENT_CONDITIONS_OUT:
                return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20condition%20on%20departure", status_code=303)
            to_sign.append((prep, condition_out))
        if not to_sign:
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=No%20equipment%20to%20sign%20out", status_code=303)
        now = datetime.utcnow().isoformat()
        for prep, condition_out in to_sign:
            cursor.execute(
                """
                INSERT INTO equipment_movements
                (company_id, job_id, prep_item_id, equipment_id, movement_type,
                 collected_by_name, collected_by_contact, condition_out, notes,
                 processed_by_user_id, processed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user["company_id"],
                    job_id,
                    prep["id"],
                    prep.get("equipment_id"),
                    "collected",
                    collected_by,
                    contact,
                    condition_out,
                    job_notes,
                    user["user_id"],
                    now,
                ),
            )
        return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/warehouse/job/{job_id}/sign-in-all")
async def warehouse_equipment_sign_in_all(request: Request, job_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    form = await request.form()
    returned_by = str(form.get("returned_by_name", "")).strip()
    if not returned_by:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Returned%20by%20name%20is%20required", status_code=303)
    job_notes = str(form.get("notes", "")).strip()
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT status FROM jobs WHERE id=%s AND company_id=%s",
            (job_id, user["company_id"]),
        )
        job = cursor.fetchone()
        if not job:
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Job%20not%20found", status_code=303)
        cursor.execute(
            """
            SELECT id, equipment_id, equipment_name, line_type
            FROM job_prep_items
            WHERE job_id=%s AND company_id=%s
            ORDER BY id ASC
            """,
            (job_id, user["company_id"]),
        )
        prep_items = cursor.fetchall()
        movements = list_equipment_movements_for_job(cursor, user["company_id"], job_id)
        to_sign: list[tuple[dict, str]] = []
        needs_damage_notes = False
        for prep in prep_items:
            if (prep.get("line_type") or "owned") != "owned":
                continue
            prep_id = int(prep["id"])
            if prep_item_collection_state(movements, prep_id) != "out":
                continue
            condition_in = str(form.get(f"condition_in_{prep_id}", "")).strip()
            if condition_in not in EQUIPMENT_CONDITIONS_IN:
                return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20return%20condition", status_code=303)
            if condition_in in ("Damaged", "Missing items"):
                needs_damage_notes = True
            to_sign.append((prep, condition_in))
        if not to_sign:
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=No%20equipment%20to%20sign%20in", status_code=303)
        if needs_damage_notes and not job_notes:
            return RedirectResponse(
                url=f"/warehouse/job/{job_id}?error=Notes%20required%20when%20any%20item%20is%20damaged%20or%20missing",
                status_code=303,
            )
        now = datetime.utcnow().isoformat()
        for prep, condition_in in to_sign:
            cursor.execute(
                """
                INSERT INTO equipment_movements
                (company_id, job_id, prep_item_id, equipment_id, movement_type,
                 collected_by_name, condition_in, notes,
                 processed_by_user_id, processed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user["company_id"],
                    job_id,
                    prep["id"],
                    prep.get("equipment_id"),
                    "returned",
                    returned_by,
                    condition_in,
                    job_notes,
                    user["user_id"],
                    now,
                ),
            )
        return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/sign-out")
async def warehouse_equipment_sign_out(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    form = await request.form()
    collected_by = str(form.get("collected_by_name", "")).strip()
    if not collected_by:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Collected%20by%20name%20is%20required", status_code=303)
    contact = str(form.get("collected_by_contact", "")).strip()
    condition_out = str(form.get("condition_out", "")).strip()
    if condition_out not in EQUIPMENT_CONDITIONS_OUT:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20condition", status_code=303)
    notes = str(form.get("notes", "")).strip()
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT status FROM jobs WHERE id=%s AND company_id=%s",
            (job_id, user["company_id"]),
        )
        job = cursor.fetchone()
        if not job or job["status"] not in ("upcoming", "active"):
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Sign-out%20not%20available%20for%20this%20job%20status", status_code=303)
        cursor.execute(
            """
            SELECT id, equipment_id, equipment_name, line_type
            FROM job_prep_items
            WHERE id=%s AND job_id=%s AND company_id=%s
            """,
            (prep_item_id, job_id, user["company_id"]),
        )
        prep = cursor.fetchone()
        if not prep or (prep.get("line_type") or "owned") != "owned":
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
        movements = list_equipment_movements_for_job(cursor, user["company_id"], job_id)
        if prep_item_collection_state(movements, prep_item_id) == "out":
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Equipment%20already%20signed%20out", status_code=303)
        now = datetime.utcnow().isoformat()
        cursor.execute(
            """
            INSERT INTO equipment_movements
            (company_id, job_id, prep_item_id, equipment_id, movement_type,
             collected_by_name, collected_by_contact, condition_out, notes,
             processed_by_user_id, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user["company_id"],
                job_id,
                prep_item_id,
                prep.get("equipment_id"),
                "collected",
                collected_by,
                contact,
                condition_out,
                notes,
                user["user_id"],
                now,
            ),
        )
        return RedirectResponse(url=f"/warehouse/job/{job_id}", status_code=303)


@app.post("/warehouse/job/{job_id}/prep/{prep_item_id}/sign-in")
async def warehouse_equipment_sign_in(request: Request, job_id: int, prep_item_id: int):
    user, response = get_current_user(request, allowed_roles={"warehouse", "admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", f"/warehouse/job/{job_id}", user)
    form = await request.form()
    returned_by = str(form.get("returned_by_name", "")).strip()
    if not returned_by:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Returned%20by%20name%20is%20required", status_code=303)
    condition_in = str(form.get("condition_in", "")).strip()
    if condition_in not in EQUIPMENT_CONDITIONS_IN:
        return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Invalid%20return%20condition", status_code=303)
    damage_notes = str(form.get("damage_notes", "")).strip()
    if condition_in in ("Damaged", "Missing items") and not damage_notes:
        return RedirectResponse(
            url=f"/warehouse/job/{job_id}?error=Damage%20notes%20required%20for%20damaged%20or%20missing%20items",
            status_code=303,
        )
    notes = damage_notes
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT status FROM jobs WHERE id=%s AND company_id=%s",
            (job_id, user["company_id"]),
        )
        job = cursor.fetchone()
        if not job:
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Job%20not%20found", status_code=303)
        cursor.execute(
            """
            SELECT id, equipment_id, line_type
            FROM job_prep_items
            WHERE id=%s AND job_id=%s AND company_id=%s
            """,
            (prep_item_id, job_id, user["company_id"]),
        )
        prep = cursor.fetchone()
        if not prep or (prep.get("line_type") or "owned") != "owned":
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Item%20not%20found", status_code=303)
        movements = list_equipment_movements_for_job(cursor, user["company_id"], job_id)
        if prep_item_collection_state(movements, prep_item_id) != "out":
            return RedirectResponse(url=f"/warehouse/job/{job_id}?error=Equipment%20must%20be%20signed%20out%20first", status_code=303)
        now = datetime.utcnow().isoformat()
        cursor.execute(
            """
            INSERT INTO equipment_movements
            (company_id, job_id, prep_item_id, equipment_id, movement_type,
             collected_by_name, condition_in, notes,
             processed_by_user_id, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user["company_id"],
                job_id,
                prep_item_id,
                prep.get("equipment_id"),
                "returned",
                returned_by,
                condition_in,
                notes,
                user["user_id"],
                now,
            ),
        )
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("SELECT status FROM jobs WHERE id=%s AND company_id=%s", (job_id, user["company_id"]))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return RedirectResponse(url="/quotes/dashboard?error=Job%20not%20found", status_code=303)
            old = row["status"]
            if old != job_status:
                ok, err = process_job_status_stock_delta(cursor, user["company_id"], job_id, old, job_status)
                if not ok:
                    conn.rollback()
                    qe = urlencode({"error": err or "Not enough units available"})
                    return RedirectResponse(url=f"/quotes/dashboard?{qe}", status_code=303)
                cursor.execute("UPDATE jobs SET status=%s WHERE id=%s AND company_id=%s", (job_status, job_id, user["company_id"]))
                log_job_status_change(cursor, user["company_id"], job_id, old, job_status, user["user_id"])
        except Exception:
            conn.rollback()
            raise
        return RedirectResponse(url="/quotes/dashboard", status_code=303)
    
    
@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    return templates.TemplateResponse(
        request=request,
        name="add.html",
        context={
            "title": "Add Equipment",
            "current_user": user,
            "equipment_categories": [c["name"] for c in list_equipment_categories(user["company_id"])],
        },
    )


@app.post("/add")
async def add_equipment(
    request: Request,
    name: str = Form(...),
    price: int = Form(...),
    quantity: int = Form(default=1),
    category: str = Form(default=""),
):
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
    clean_category = normalize_equipment_category(user["company_id"], category)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            INSERT INTO equipment (name, status, price, prep_status, company_id, quantity, quantity_rented, category)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
            """,
            (clean_name, "available", price, "pending", user["company_id"], quantity, clean_category),
        )
        return templates.TemplateResponse(
            request=request,
            name="add.html",
            context={
                "title": "Add Equipment",
                "current_user": user,
                "equipment_categories": [c["name"] for c in list_equipment_categories(user["company_id"])],
                "success_name": clean_name,
            },
        )
    
    
@app.get("/equipment/{item_id}/edit", response_class=HTMLResponse)
def edit_equipment_page(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
        item = cursor.fetchone()
        if not item:
            return render_message(request, "Error", "Equipment not found.", "/", user)
        return templates.TemplateResponse(
            request=request,
            name="equipment_edit.html",
            context={
                "title": "Edit Equipment",
                "item": item,
                "current_user": user,
                "equipment_categories": [c["name"] for c in list_equipment_categories(user["company_id"])],
            },
        )
    
    
@app.post("/equipment/{item_id}/edit")
async def edit_equipment(
    request: Request,
    item_id: int,
    name: str = Form(...),
    price: int = Form(...),
    quantity: int = Form(...),
    category: str = Form(default=""),
):
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT quantity_rented FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
        cur = cursor.fetchone()
        if not cur:
            return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Not%20found", status_code=303)
        qr = int(cur["quantity_rented"] or 0)
        if quantity < qr:
            return RedirectResponse(url=f"/equipment/{item_id}/edit?error=Quantity%20cannot%20be%20less%20than%20rented%20units", status_code=303)
        clean_category = normalize_equipment_category(user["company_id"], category)
        cursor.execute(
            "UPDATE equipment SET name=%s, price=%s, quantity=%s, category=%s WHERE id=%s AND company_id=%s",
            (clean_name, price, quantity, clean_category, item_id, user["company_id"]),
        )
        sync_equipment_row(cursor, item_id, user["company_id"])
        return RedirectResponse(url="/", status_code=303)
    
    
@app.post("/equipment/{item_id}/delete")
async def delete_equipment(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("DELETE FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
        return RedirectResponse(url="/", status_code=303)
    
    
@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM clients WHERE company_id=%s ORDER BY name ASC", (user["company_id"],))
        clients = cursor.fetchall()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            INSERT INTO clients (name, company_id, contact_person, phone, email, address, vat_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (clean_name, user["company_id"], contact_person, phone, email, address, vat_number),
        )
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM clients WHERE id=%s AND company_id=%s", (client_id, user["company_id"]))
        client = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id FROM clients WHERE id=%s AND company_id=%s", (client_id, user["company_id"]))
        if not cursor.fetchone():
            return RedirectResponse(url="/clients?error=Not%20found", status_code=303)
        cursor.execute(
            """
            UPDATE clients
            SET name=%s, contact_person=%s, phone=%s, email=%s, address=%s, vat_number=%s
            WHERE id=%s AND company_id=%s
            """,
            (clean_name, contact_person, phone, email, address, vat_number, client_id, user["company_id"]),
        )
        return RedirectResponse(url="/clients", status_code=303)
    
    
@app.get("/sub-rentals", response_class=HTMLResponse)
def sub_rentals_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT * FROM sub_rentals WHERE company_id=%s ORDER BY id DESC",
            (user["company_id"],),
        )
        rows = cursor.fetchall()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            INSERT INTO sub_rentals (company_id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user["company_id"], sup, desc, quantity_total, quantity_total, cost_per_unit, notes.strip(), created),
        )
        return RedirectResponse(url="/sub-rentals", status_code=303)
    
    
@app.get("/sub-rentals/{sub_id}/edit", response_class=HTMLResponse)
def sub_rental_edit_page(request: Request, sub_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM sub_rentals WHERE id=%s AND company_id=%s", (sub_id, user["company_id"]))
        row = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM sub_rentals WHERE id=%s AND company_id=%s", (sub_id, user["company_id"]))
        cur = cursor.fetchone()
        if not cur:
            return RedirectResponse(url="/sub-rentals?error=Not%20found", status_code=303)
        old_total = int(cur["quantity_total"] or 0)
        old_avail = int(cur["quantity_available"] or 0)
        in_use = max(0, old_total - old_avail)
        new_total = quantity_total
        new_avail = new_total - in_use
        if new_avail < 0:
            return RedirectResponse(
                url=f"/sub-rentals/{sub_id}/edit?error=Total%20units%20cannot%20be%20less%20than%20units%20already%20allocated",
                status_code=303,
            )
        cursor.execute(
            """
            UPDATE sub_rentals
            SET supplier_name=%s, equipment_description=%s, quantity_total=%s, quantity_available=%s, cost_per_unit=%s, notes=%s
            WHERE id=%s AND company_id=%s
            """,
            (sup, desc, new_total, new_avail, cost_per_unit, notes.strip(), sub_id, user["company_id"]),
        )
        return RedirectResponse(url="/sub-rentals", status_code=303)
    
    
@app.post("/sub-rentals/{sub_id}/delete")
async def sub_rental_delete(request: Request, sub_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    if not await validate_csrf(request, user):
        return render_message(request, "Security Error", "Invalid security token.", "/sub-rentals", user)
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id FROM sub_rentals WHERE id=%s AND company_id=%s", (sub_id, user["company_id"]))
        if not cursor.fetchone():
            return RedirectResponse(url="/sub-rentals?error=Not%20found", status_code=303)
        cursor.execute(
            """
            SELECT 1 FROM sub_rental_usage u
            JOIN jobs j ON j.id = u.job_id AND j.company_id = u.company_id
            WHERE u.sub_rental_id = %s AND u.company_id = %s
            AND j.status IN ('upcoming', 'active')
            LIMIT 1
            """,
            (sub_id, user["company_id"]),
        )
        if cursor.fetchone():
            return RedirectResponse(
                url="/sub-rentals?error=Cannot%20delete%20while%20units%20are%20on%20an%20open%20job",
                status_code=303,
            )
        cursor.execute("DELETE FROM sub_rental_usage WHERE sub_rental_id=%s AND company_id=%s", (sub_id, user["company_id"]))
        cursor.execute("DELETE FROM sub_rentals WHERE id=%s AND company_id=%s", (sub_id, user["company_id"]))
        return RedirectResponse(url="/sub-rentals", status_code=303)
    
    
@app.get("/quote", response_class=HTMLResponse)
def quote_page(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT id, name, price, quantity, quantity_rented,
                   (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) AS qty_available
            FROM equipment
            WHERE company_id=%s AND (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) > 0
            ORDER BY name ASC
            """,
            (user["company_id"],),
        )
        items = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit, notes
            FROM sub_rentals
            WHERE company_id=%s AND quantity_available > 0
            ORDER BY supplier_name ASC, id ASC
            """,
            (user["company_id"],),
        )
        sub_rentals = cursor.fetchall()
        cursor.execute("SELECT * FROM clients WHERE company_id=%s ORDER BY name ASC", (user["company_id"],))
        clients = cursor.fetchall()
        company = get_company_settings(user["company_id"])
        technician_functions = list_technician_functions(user["company_id"])
        return templates.TemplateResponse(
            request=request,
            name="quote.html",
            context={
                "title": "Create Quote",
                "items": items,
                "sub_rentals": sub_rentals,
                "clients": clients,
                "company": company,
                "technician_functions": technician_functions,
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT id, name, price, quantity, quantity_rented
            FROM equipment
            WHERE company_id=%s AND (COALESCE(quantity, 1) - COALESCE(quantity_rented, 0)) > 0
            ORDER BY name ASC
            """,
            (user["company_id"],),
        )
        items = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, supplier_name, equipment_description, quantity_total, quantity_available, cost_per_unit
            FROM sub_rentals
            WHERE company_id=%s AND quantity_available > 0
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
                return render_message(request, "Create Quote", "Quantity and days must be whole numbers.", "/quote", user)
            if qty < 0 or days < 1:
                return render_message(request, "Create Quote", "Quantity must be 0+ and days at least 1.", "/quote", user)
            if qty == 0:
                continue
            avail = qty_available(item)
            if qty > avail:
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
                return render_message(request, "Create Quote", "Sub-rental quantity and days must be whole numbers.", "/quote", user)
            if qty < 0 or days < 1:
                return render_message(request, "Create Quote", "Sub-rental quantity must be 0+ and days at least 1.", "/quote", user)
            if qty == 0:
                continue
            avail = max(0, int(sr["quantity_available"] or 0))
            if qty > avail:
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
        personnel_lines, personnel_err = parse_personnel_lines_from_form(form_data, user["company_id"], cursor)
        if personnel_err:
            return render_message(request, "Create Quote", personnel_err, "/quote", user)
        lines.extend(personnel_lines)
        if not lines:
            return render_message(
                request,
                "Create Quote",
                "Please add at least one equipment, sub-rental, or personnel line.",
                "/quote",
                user,
            )
        transport = parse_transport_from_form(form_data)
        equipment_subtotal = sum(
            int(l.get("line_total", 0) or 0)
            for l in lines
            if str(l.get("line_type") or "").lower() not in ("personnel", "function", "technician")
        )
        personnel_subtotal = sum(
            int(l.get("line_total", 0) or 0)
            for l in lines
            if str(l.get("line_type") or "").lower() in ("personnel", "function", "technician")
        )
        subtotal = equipment_subtotal + transport["transport_amount"] + personnel_subtotal
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
        totals["equipment_subtotal"] = equipment_subtotal
        totals["personnel_subtotal"] = personnel_subtotal
        totals["transport_amount"] = transport["transport_amount"]
        totals["transport_type"] = transport["transport_type"]
        totals["transport_description"] = transport["transport_description"]
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
                        job_name, site_location, start_date, end_date, special_notes, quote_terms,
                        transport_type, transport_description, transport_amount
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        transport["transport_type"],
                        transport["transport_description"] or None,
                        transport["transport_amount"],
                    ),
                )
                break
            except psycopg2.IntegrityError:
                conn.rollback()
                quote_number = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        else:
            return render_message(request, "Create Quote", "Could not save quote. Please try again.", "/quote", user)
        quote_id = cursor.fetchone()["id"]
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
    with tempfile.NamedTemporaryFile(prefix=f"quote_{qnum}_", suffix=".pdf", delete=False) as tmp:
        temp_pdf_path = Path(tmp.name)
    doc = SimpleDocTemplate(str(temp_pdf_path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    content: list = []
    cid = int(user["company_id"])
    meta = quote_meta or {}
    left_fields = [("Quote #", qnum), ("Date", date)]
    content.extend(pdf_document_header_flowables("QUOTE", left_fields, settings, cid, styles))

    client_prof = meta.get("client_profile")
    if client_prof is None:
        client_prof = client_profile_by_name(cid, client)
    content.extend(pdf_client_section_flowables(client, client_prof, styles))
    content.extend(
        pdf_job_section_flowables(
            meta.get("job_name"),
            meta.get("site_location"),
            meta.get("start_date"),
            meta.get("end_date"),
            meta.get("special_notes"),
            styles,
        )
    )
    content.extend(pdf_equipment_table_flowables(lines, styles, quote_style=True))
    transport = {
        "transport_type": fin.get("transport_type", "none"),
        "transport_description": fin.get("transport_description", ""),
        "transport_amount": int(fin.get("transport_amount") or 0),
    }
    content.extend(pdf_transport_section_flowables(transport, styles))
    content.extend(pdf_personnel_section_flowables(lines, styles))

    quote_col_widths = [12 * mm, 104 * mm, 26 * mm, 12 * mm, 24 * mm]
    content.extend(pdf_totals_table_flowables(fin, styles, line_items_width=sum(quote_col_widths)))
    content.extend(pdf_terms_section_flowables(meta.get("quote_terms"), styles))
    content.extend(pdf_banking_section_flowables(settings, styles))
    content.extend(pdf_thank_you_footer_flowables(settings, styles))
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
        with get_db() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("SELECT * FROM quotes WHERE id=%s AND company_id=%s", (quote_id, user["company_id"]))
            qrow = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM clients WHERE company_id=%s ORDER BY name ASC", (user["company_id"],))
        clients = cursor.fetchall()
        cursor.execute("SELECT * FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
        equipment = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("SELECT * FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
            equipment = cursor.fetchone()
            if not equipment:
                conn.rollback()
                return render_message(request, "Error", "Equipment not found.", "/", user)
            if units > qty_available(equipment):
                conn.rollback()
                return render_message(request, "Rent Equipment", "Not enough units available", f"/rent/{item_id}", user)
            cursor.execute(
                """
                UPDATE equipment
                SET quantity_rented = COALESCE(quantity_rented, 0) + %s,
                    rented_to = %s,
                    due_date = %s,
                    prep_status = 'pending'
                WHERE id=%s AND company_id=%s
                """,
                (units, clean_client, due_date, item_id, user["company_id"]),
            )
            sync_equipment_row(cursor, item_id, user["company_id"])
            cursor.execute(
                """
                INSERT INTO rental_history (equipment_name, client, date_rented, due_date, company_id, units)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (equipment["name"], clean_client, datetime.now().strftime("%Y-%m-%d"), due_date, user["company_id"], units),
            )
        except Exception:
            conn.rollback()
            raise
        return RedirectResponse(url="/", status_code=303)
    
    
@app.get("/return/{item_id}", response_class=HTMLResponse)
def return_item_page(request: Request, item_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
        equipment = cursor.fetchone()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute("SELECT name, rented_to, quantity_rented FROM equipment WHERE id=%s AND company_id=%s", (item_id, user["company_id"]))
            equipment = cursor.fetchone()
            if not equipment:
                conn.rollback()
                return render_message(request, "Error", "Equipment not found.", "/", user)
            qr = qty_rented(equipment)
            if units > qr:
                conn.rollback()
                return RedirectResponse(url=f"/return/{item_id}?error=Not%20enough%20units%20available", status_code=303)
            apply_rental_return_units(cursor, user["company_id"], equipment["name"], units)
            cursor.execute(
                "UPDATE equipment SET quantity_rented = GREATEST(0::integer, (COALESCE(quantity_rented, 0) - %s)::integer) WHERE id=%s AND company_id=%s",
                (units, item_id, user["company_id"]),
            )
            sync_equipment_row(cursor, item_id, user["company_id"])
        except Exception:
            conn.rollback()
            raise
        return RedirectResponse(url="/", status_code=303)
    
    
@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            """
            SELECT id, invoice_number, client_name, created_at, due_date, total,
                   COALESCE(amount_paid, 0) AS amount_paid, payment_status
            FROM invoices
            WHERE company_id=%s
            ORDER BY id DESC
            """,
            (user["company_id"],),
        )
        rows = cursor.fetchall()
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM invoices WHERE id=%s AND company_id=%s", (invoice_id, user["company_id"]))
        inv_row = cursor.fetchone()
        if not inv_row:
            return render_message(request, "Not found", "Invoice not found.", "/invoices", user)
        inv = dict(inv_row)
        quote_terms = ""
        if inv.get("quote_id"):
            cursor.execute(
                "SELECT quote_terms FROM quotes WHERE id=%s AND company_id=%s",
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
            WHERE ip.invoice_id=%s AND ip.company_id=%s
            ORDER BY ip.recorded_at ASC, ip.id ASC
            """,
            (invoice_id, user["company_id"]),
        )
        payments = [dict(p) for p in cursor.fetchall()]
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
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(
                "SELECT id FROM invoices WHERE id=%s AND company_id=%s",
                (invoice_id, user["company_id"]),
            )
            if not cursor.fetchone():
                conn.rollback()
                return RedirectResponse(url="/invoices?error=Invoice%20not%20found", status_code=303)
            cursor.execute(
                """
                INSERT INTO invoice_payments (company_id, invoice_id, amount, recorded_by_user_id, recorded_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user["company_id"], invoice_id, amt, user["user_id"], datetime.utcnow().isoformat()),
            )
            refresh_invoice_payment_aggregate(cursor, invoice_id, user["company_id"])
        except Exception:
            conn.rollback()
            raise
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)
    
    
@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf_download(request: Request, invoice_id: int):
    user, response = get_current_user(request, allowed_roles={"admin", "management"})
    if response:
        return response
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM invoices WHERE id=%s AND company_id=%s", (invoice_id, user["company_id"]))
        inv_row = cursor.fetchone()
        if not inv_row:
            return render_message(request, "Not found", "Invoice not found.", "/invoices", user)
        inv = dict(inv_row)
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

