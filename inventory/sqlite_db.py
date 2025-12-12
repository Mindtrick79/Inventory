import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from config import LOCAL_XLSX


def ensure_parent_dir(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)


def connect(db_path: str) -> sqlite3.Connection:
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_name TEXT NOT NULL,
              distributor TEXT,
              category TEXT,
              location TEXT,
              quantity_on_hand REAL,
              reorder_threshold REAL,
              reorder_amount REAL,
              cost_per_unit REAL,
              container_unit TEXT,
              data_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_products_name ON products(product_name);
            CREATE INDEX IF NOT EXISTS idx_products_vendor ON products(distributor);

            CREATE TABLE IF NOT EXISTS vendors (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor_name TEXT NOT NULL,
              email TEXT,
              cc_emails TEXT,
              data_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_vendors_name ON vendors(vendor_name);

            CREATE TABLE IF NOT EXISTS reorder_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL,
              user TEXT,
              ip TEXT,
              vendor TEXT,
              status TEXT,
              items TEXT,
              notes TEXT,
              approved_timestamp TEXT,
              approved_by TEXT,
              approved_ip TEXT,
              internal_notes TEXT,
              data_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reorder_ts ON reorder_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_reorder_vendor ON reorder_log(vendor);

            CREATE TABLE IF NOT EXISTS transactions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL,
              user TEXT,
              action TEXT,
              product_name TEXT,
              delta REAL,
              location TEXT,
              notes TEXT,
              data_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tx_ts ON transactions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_tx_product ON transactions(product_name);
            """
        )


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _to_json(row: Dict[str, Any]) -> str:
    def _default(o: Any):
        try:
            if pd.isna(o):
                return ""
        except Exception:
            pass
        return str(o)

    return json.dumps(row, default=_default)


def import_from_excel(db_path: str, excel_path: Optional[str] = None) -> Dict[str, int]:
    """Import the current Excel workbook into SQLite.

    This is lossless: we store full row JSON plus a few indexed columns.
    The import is idempotent by replacing tables.
    """

    xlsx = excel_path or LOCAL_XLSX
    init_db(db_path)

    xls = pd.ExcelFile(xlsx, engine="openpyxl")
    sheet_names = set(xls.sheet_names)

    master_df = pd.read_excel(xls, "Master Inventory") if "Master Inventory" in sheet_names else pd.DataFrame()
    vendors_df = pd.read_excel(xls, "Vendors") if "Vendors" in sheet_names else pd.DataFrame()
    reorder_df = pd.read_excel(xls, "Reorder Log") if "Reorder Log" in sheet_names else pd.DataFrame()
    tx_df = pd.read_excel(xls, "All Transactions") if "All Transactions" in sheet_names else pd.DataFrame()

    now = datetime.utcnow().isoformat()

    with connect(db_path) as conn:
        conn.execute("DELETE FROM products")
        conn.execute("DELETE FROM vendors")
        conn.execute("DELETE FROM reorder_log")
        conn.execute("DELETE FROM transactions")

        products_count = 0
        if not master_df.empty:
            master_df = master_df.fillna("")
            for _, r in master_df.iterrows():
                d = r.to_dict()
                conn.execute(
                    """
                    INSERT INTO products(
                      product_name, distributor, category, location,
                      quantity_on_hand, reorder_threshold, reorder_amount,
                      cost_per_unit, container_unit,
                      data_json, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(d.get("Product Name", "")),
                        str(d.get("Distributor", "")),
                        str(d.get("Category", "")),
                        str(d.get("Location", "")),
                        _safe_float(d.get("Quantity on Hand")),
                        _safe_float(d.get("Reorder Threshold")),
                        _safe_float(d.get("Reorder Amount")),
                        _safe_float(d.get("Cost Per Unit")),
                        str(d.get("Container Unit", "")),
                        _to_json(d),
                        now,
                    ),
                )
                products_count += 1

        vendors_count = 0
        if not vendors_df.empty:
            vendors_df = vendors_df.fillna("")
            for _, r in vendors_df.iterrows():
                d = r.to_dict()
                conn.execute(
                    """
                    INSERT INTO vendors(vendor_name, email, cc_emails, data_json, updated_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        str(d.get("Vendor Name", "")),
                        str(d.get("Email", "")),
                        str(d.get("CC Emails", "")),
                        _to_json(d),
                        now,
                    ),
                )
                vendors_count += 1

        reorder_count = 0
        if not reorder_df.empty:
            reorder_df = reorder_df.fillna("")
            for _, r in reorder_df.iterrows():
                d = r.to_dict()
                conn.execute(
                    """
                    INSERT INTO reorder_log(
                      timestamp, user, ip, vendor, status, items, notes,
                      approved_timestamp, approved_by, approved_ip, internal_notes,
                      data_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(d.get("Timestamp", "")),
                        str(d.get("User", "")),
                        str(d.get("IP", "")),
                        str(d.get("Vendor", "")),
                        str(d.get("Status", "")),
                        str(d.get("Items", "")),
                        str(d.get("Notes", "")),
                        str(d.get("Approved Timestamp", "")),
                        str(d.get("Approved By", "")),
                        str(d.get("Approved IP", "")),
                        str(d.get("Internal Notes", "")),
                        _to_json(d),
                    ),
                )
                reorder_count += 1

        tx_count = 0
        if not tx_df.empty:
            tx_df = tx_df.fillna("")
            for _, r in tx_df.iterrows():
                d = r.to_dict()
                conn.execute(
                    """
                    INSERT INTO transactions(timestamp, user, action, product_name, delta, location, notes, data_json)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(d.get("Timestamp", d.get("Date", ""))),
                        str(d.get("User", "")),
                        str(d.get("Action", d.get("Type", ""))),
                        str(d.get("Product Name", d.get("Product", ""))),
                        _safe_float(d.get("Delta", d.get("Quantity", ""))),
                        str(d.get("Location", "")),
                        str(d.get("Notes", "")),
                        _to_json(d),
                    ),
                )
                tx_count += 1

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_import_source", str(xlsx)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_import_utc", now),
        )

    return {
        "products": products_count,
        "vendors": vendors_count,
        "reorders": reorder_count,
        "transactions": tx_count,
    }


def get_counts(db_path: str) -> Dict[str, int]:
    init_db(db_path)
    with connect(db_path) as conn:
        res = {}
        for table in ["products", "vendors", "reorder_log", "transactions"]:
            res[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
        return res
