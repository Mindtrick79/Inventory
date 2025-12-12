import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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


def _row_json_to_dict(row_json: str) -> Dict[str, Any]:
    try:
        d = json.loads(row_json or "{}")
        if isinstance(d, dict):
            return d
        return {}
    except Exception:
        return {}


def get_all_products(db_path: str) -> List[Dict[str, Any]]:
    """Return product dicts in the same shape as Excel-based get_all_products()."""
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data_json FROM products ORDER BY product_name COLLATE NOCASE"
        ).fetchall()

    products = [_row_json_to_dict(r["data_json"]) for r in rows]
    for p in products:
        if "Cost Per Unit" not in p:
            p["Cost Per Unit"] = ""
    return products


def get_reorder_log(db_path: str) -> List[Dict[str, Any]]:
    """Return reorder log dicts in the same shape as Excel-based get_reorder_log()."""
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data_json FROM reorder_log ORDER BY timestamp DESC"
        ).fetchall()

    return [_row_json_to_dict(r["data_json"]) for r in rows]


def get_pending_reorders(db_path: str) -> List[Dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data_json FROM reorder_log WHERE status = ? ORDER BY timestamp DESC",
            ("PENDING",),
        ).fetchall()
    return [_row_json_to_dict(r["data_json"]) for r in rows]


def insert_reorder_log(
    db_path: str,
    *,
    user: str,
    ip: str,
    vendor: str,
    items_description: str,
    status: str = "PENDING",
    notes: str = "",
) -> str:
    init_db(db_path)
    timestamp = datetime.utcnow().isoformat()
    entry: Dict[str, Any] = {
        "Timestamp": timestamp,
        "User": user,
        "IP": ip,
        "Vendor": vendor,
        "Items": items_description,
        "Status": status,
        "Notes": notes,
        "Approved Timestamp": "",
        "Approved By": "",
        "Approved IP": "",
    }

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reorder_log(
              timestamp, user, ip, vendor, status, items, notes,
              approved_timestamp, approved_by, approved_ip, internal_notes,
              data_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                timestamp,
                user,
                ip,
                vendor,
                status,
                items_description,
                notes,
                "",
                "",
                "",
                "",
                _to_json(entry),
            ),
        )

    return timestamp


def update_reorder_status(
    db_path: str,
    *,
    timestamp: str,
    vendor: str,
    new_status: str,
    approved_by: str,
    approved_ip: str,
    internal_notes: str = "",
) -> int:
    init_db(db_path)
    now = datetime.utcnow().isoformat()

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, data_json FROM reorder_log WHERE timestamp = ? AND vendor = ?",
            (str(timestamp), str(vendor)),
        ).fetchall()

        updated = 0
        for r in rows:
            d = _row_json_to_dict(r["data_json"])
            d["Status"] = new_status
            d["Approved Timestamp"] = now
            d["Approved By"] = approved_by
            d["Approved IP"] = approved_ip
            if internal_notes:
                d["Internal Notes"] = internal_notes

            cur = conn.execute(
                """
                UPDATE reorder_log
                SET status = ?,
                    approved_timestamp = ?,
                    approved_by = ?,
                    approved_ip = ?,
                    internal_notes = ?,
                    data_json = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    now,
                    approved_by,
                    approved_ip,
                    internal_notes,
                    _to_json(d),
                    int(r["id"]),
                ),
            )
            updated += int(cur.rowcount or 0)

        return updated


def adjust_product_quantity(
    db_path: str,
    *,
    product_name: str,
    delta: float,
    user: str,
    location: str = "",
    notes: str = "",
) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, quantity_on_hand, data_json FROM products WHERE product_name = ?",
            (str(product_name),),
        ).fetchone()
        if not row:
            return None

        current_qty = float(row["quantity_on_hand"] or 0)
        new_qty = current_qty + float(delta)
        if new_qty < 0:
            new_qty = 0

        product_dict = _row_json_to_dict(row["data_json"])
        product_dict["Quantity on Hand"] = new_qty

        ts = datetime.utcnow().isoformat()
        tx_entry: Dict[str, Any] = {
            "Timestamp": ts,
            "User": user,
            "Product Name": product_dict.get("Product Name", product_name),
            "Delta": float(delta),
            "New Quantity on Hand": new_qty,
            "Location": location or str(product_dict.get("Location", "")),
            "Notes": notes,
        }

        conn.execute(
            """
            UPDATE products
            SET quantity_on_hand = ?, data_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_qty, _to_json(product_dict), datetime.utcnow().isoformat(), int(row["id"])),
        )

        conn.execute(
            """
            INSERT INTO transactions(timestamp, user, action, product_name, delta, location, notes, data_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                ts,
                user,
                "ADJUST",
                str(product_name),
                float(delta),
                tx_entry.get("Location", ""),
                notes,
                _to_json(tx_entry),
            ),
        )

    return {
        "product_name": product_dict.get("Product Name", product_name),
        "old_quantity": current_qty,
        "new_quantity": new_qty,
        "location": tx_entry["Location"],
    }


def get_product_by_name(db_path: str, product_name: str) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT data_json FROM products WHERE product_name = ?",
            (str(product_name),),
        ).fetchone()
    if not row:
        return None
    d = _row_json_to_dict(row["data_json"])
    if "Cost Per Unit" not in d:
        d["Cost Per Unit"] = ""
    return d


def upsert_product(db_path: str, original_name: Optional[str], data: Dict[str, Any]) -> bool:
    """Insert or update a product.

    If original_name is provided, we update that row (by product_name) and allow changing
    the Product Name in data. If original_name is None, we insert a new row.
    """

    init_db(db_path)
    name = str(data.get("Product Name", "") or "").strip()
    if not name:
        return False

    distributor = str(data.get("Distributor", "") or "")
    category = str(data.get("Category", "") or "")
    location = str(data.get("Location", "") or "")
    container_unit = str(data.get("Container Unit", "") or "")
    quantity_on_hand = _safe_float(data.get("Quantity on Hand"))
    reorder_threshold = _safe_float(data.get("Reorder Threshold"))
    reorder_amount = _safe_float(data.get("Reorder Amount"))
    cost_per_unit = _safe_float(data.get("Cost Per Unit"))
    now = datetime.utcnow().isoformat()

    with connect(db_path) as conn:
        if original_name:
            existing = conn.execute(
                "SELECT id FROM products WHERE product_name = ?",
                (str(original_name),),
            ).fetchone()
        else:
            existing = None

        if existing:
            conn.execute(
                """
                UPDATE products
                SET product_name = ?,
                    distributor = ?,
                    category = ?,
                    location = ?,
                    quantity_on_hand = ?,
                    reorder_threshold = ?,
                    reorder_amount = ?,
                    cost_per_unit = ?,
                    container_unit = ?,
                    data_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    distributor,
                    category,
                    location,
                    quantity_on_hand,
                    reorder_threshold,
                    reorder_amount,
                    cost_per_unit,
                    container_unit,
                    _to_json(data),
                    now,
                    int(existing["id"]),
                ),
            )
            return True

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
                name,
                distributor,
                category,
                location,
                quantity_on_hand,
                reorder_threshold,
                reorder_amount,
                cost_per_unit,
                container_unit,
                _to_json(data),
                now,
            ),
        )
        return True


def get_all_vendors(db_path: str) -> List[Dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data_json FROM vendors ORDER BY vendor_name COLLATE NOCASE"
        ).fetchall()
    return [_row_json_to_dict(r["data_json"]) for r in rows]


def get_vendor_by_name(db_path: str, vendor_name: str) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT data_json FROM vendors WHERE vendor_name = ?",
            (str(vendor_name),),
        ).fetchone()
    if not row:
        return None
    return _row_json_to_dict(row["data_json"])


def upsert_vendor(db_path: str, data: Dict[str, Any]) -> bool:
    init_db(db_path)
    name = str(data.get("Vendor Name", "") or "").strip()
    if not name:
        return False

    email = str(data.get("Email", "") or "")
    cc_emails = str(data.get("CC Emails", "") or "")
    now = datetime.utcnow().isoformat()

    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM vendors WHERE vendor_name = ?",
            (name,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE vendors
                SET email = ?, cc_emails = ?, data_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (email, cc_emails, _to_json(data), now, int(existing["id"])),
            )
            return True

        conn.execute(
            """
            INSERT INTO vendors(vendor_name, email, cc_emails, data_json, updated_at)
            VALUES (?,?,?,?,?)
            """,
            (name, email, cc_emails, _to_json(data), now),
        )
        return True


def bulk_replace_product_field(db_path: str, column: str, old_value: str, new_value: str) -> int:
    """Replace values across all products for a specific Excel-style column.

    Used by Units & Labels (Container Unit, Reorder Quantity, Distributor).
    Returns number of updated products.
    """

    if column not in {"Container Unit", "Reorder Quantity", "Distributor"}:
        return 0
    if not new_value:
        return 0

    init_db(db_path)
    updated = 0
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, data_json FROM products"
        ).fetchall()

        for r in rows:
            d = _row_json_to_dict(r["data_json"])
            if str(d.get(column, "")) != str(old_value):
                continue
            d[column] = new_value

            # Keep the indexed columns consistent where applicable
            distributor = None
            container_unit = None
            if column == "Distributor":
                distributor = str(new_value)
            if column == "Container Unit":
                container_unit = str(new_value)

            conn.execute(
                """
                UPDATE products
                SET distributor = COALESCE(?, distributor),
                    container_unit = COALESCE(?, container_unit),
                    data_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (distributor, container_unit, _to_json(d), datetime.utcnow().isoformat(), int(r["id"])),
            )
            updated += 1

    return updated
