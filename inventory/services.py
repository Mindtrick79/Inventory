from datetime import datetime
from typing import List, Dict, Any, DefaultDict, Optional
from collections import defaultdict
import smtplib
import re
from email.message import EmailMessage
from email.utils import make_msgid
import mimetypes

import pandas as pd

from .excel_io import load_inventory_workbook, save_inventory_workbook, VENDOR_COLUMNS
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL
import json
import os

from config import DB_PATH
from .sqlite_db import get_all_products as sqlite_get_all_products
from .sqlite_db import get_pending_reorders as sqlite_get_pending_reorders
from .sqlite_db import get_reorder_log as sqlite_get_reorder_log
from .sqlite_db import adjust_product_quantity as sqlite_adjust_product_quantity
from .sqlite_db import insert_reorder_log as sqlite_insert_reorder_log
from .sqlite_db import update_reorder_status as sqlite_update_reorder_status


LOW_STOCK_STATUS = "LOW_STOCK"


SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")


def load_settings() -> Dict[str, Any]:
    """Load app settings from settings.json, with sensible defaults."""
    defaults = {
        "company_name": "Roberts Inventory Manager",
        "email_subject_prefix": "Reorder Request - ",
        "default_email_cc": "",
        "email_footer": "",
        "stock_use_notify_emails": "",
        "checkout_email_to": "office@robertspest.com",
        "checkout_email_cc": "",
        "company_address": "",
        "company_phone": "",
        "company_logo_path": "",
        "po_footer_pickup": "Please print this form for pickup. Attach vendor receipt to provide to the employee picking up the order.",
        "po_footer_ship": "Please include a copy of this form with the shipment and packing slip.",
        # Theme settings (colors/fonts)
        "theme_bg": "#f5f5f5",
        "theme_text": "#111111",
        "theme_header_bg": "#1f4e79",
        "theme_header_text": "#ffffff",
        "theme_nav_link": "#ffffff",
        "theme_nav_link_hover": "#ffffff",
        "theme_table_header_bg": "#e3edf5",
        "theme_table_row_alt": "#fafafa",
        "theme_table_row_hover": "#f1f7ff",
        "theme_card_bg": "#ffffff",
        "theme_button_bg": "#1f4e79",
        "theme_button_hover_bg": "#173958",
        "theme_button_text": "#ffffff",
        "theme_font_family": "Arial, sans-serif",
        "theme_google_font_url": "",
        "theme_radius": "6px",
        "theme_border_width": "1px",
        "theme_spacing": "1",
        "theme_header_height": "",
        # Email transport configuration; if left blank, falls back to config.py
        "smtp_provider": "bluehost",  # for future presets
        "smtp_host": "",
        "smtp_port": SMTP_PORT,
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_use_tls": True,
        "from_email": FROM_EMAIL,
    }
    if not os.path.exists(SETTINGS_PATH):
        return defaults
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults.update(data or {})
    except Exception:
        return defaults
    return defaults


def save_settings(settings: Dict[str, Any]) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        # Non-fatal; ignore write errors for now.
        pass


def _resolve_smtp_config(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return SMTP configuration using settings.json with config.py as fallback."""

    if settings is None:
        settings = load_settings()

    host = settings.get("smtp_host") or SMTP_HOST
    try:
        port = int(settings.get("smtp_port", SMTP_PORT) or SMTP_PORT)
    except (TypeError, ValueError):
        port = SMTP_PORT

    user = settings.get("smtp_user") or SMTP_USER
    password = settings.get("smtp_pass") or SMTP_PASS
    from_email = settings.get("from_email") or FROM_EMAIL
    use_tls = bool(settings.get("smtp_use_tls", True))

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_email": from_email,
        "use_tls": use_tls,
    }


def send_basic_email(subject: str, body: str, to_addresses: List[str]) -> bool:
    """Send a simple text email using the SMTP settings.

    Used by various flows (stock use notifications, tests, etc.).
    """

    settings = load_settings()
    smtp_cfg = _resolve_smtp_config(settings)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_email"]
    msg["To"] = ", ".join(to_addresses)
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
            if smtp_cfg["use_tls"]:
                server.starttls()
            if smtp_cfg["user"] and smtp_cfg["password"] and smtp_cfg["password"] != "CHANGE_ME":
                server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg, from_addr=smtp_cfg["from_email"], to_addrs=to_addresses)
        return True
    except Exception:
        return False


def send_html_email(
    subject: str,
    text_body: str,
    html_body: str,
    to_addresses: List[str],
    cc_addresses: Optional[List[str]] = None,
    inline_image_path: Optional[str] = None,
) -> bool:
    settings = load_settings()
    smtp_cfg = _resolve_smtp_config(settings)

    cc_addresses = cc_addresses or []

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_email"]
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if inline_image_path:
        try:
            with open(inline_image_path, "rb") as f:
                img_data = f.read()
            mime_type, _ = mimetypes.guess_type(inline_image_path)
            if not mime_type:
                mime_type = "application/octet-stream"
            maintype, subtype = mime_type.split("/", 1)
            cid = make_msgid(domain="robertspest.local")
            cid_ref = cid[1:-1] if cid.startswith("<") and cid.endswith(">") else cid

            # Replace placeholder token in HTML with the generated CID reference.
            if "{{INLINE_IMAGE_CID}}" in html_body:
                html_body = html_body.replace("{{INLINE_IMAGE_CID}}", cid_ref)
                msg.set_payload([msg.get_payload()[0]])
                msg.add_alternative(html_body, subtype="html")

            html_part = msg.get_payload()[-1]
            html_part.add_related(img_data, maintype=maintype, subtype=subtype, cid=cid)
            msg["X-Inline-Image-CID"] = cid_ref
        except Exception:
            pass

    recipients = list(to_addresses) + list(cc_addresses)

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
            if smtp_cfg["use_tls"]:
                server.starttls()
            if smtp_cfg["user"] and smtp_cfg["password"] and smtp_cfg["password"] != "CHANGE_ME":
                server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg, from_addr=smtp_cfg["from_email"], to_addrs=recipients)
        return True
    except Exception:
        return False


def get_all_products() -> List[Dict[str, Any]]:
    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            return sqlite_get_all_products(DB_PATH)
        except Exception:
            pass

    master_df, _, _, _ = load_inventory_workbook()
    if master_df.empty:
        return []
    # Ensure Cost Per Unit column exists for downstream analytics/UI
    if "Cost Per Unit" not in master_df.columns:
        master_df["Cost Per Unit"] = ""

    # Replace NaN with empty strings for cleaner display in templates
    master_df = master_df.fillna("")
    return master_df.to_dict(orient="records")


def get_distinct_product_values(column: str) -> List[str]:
    """Return sorted distinct non-empty values for a given product column.

    Used to populate dropdowns (e.g. Container Unit, Reorder Quantity).
    """
    master_df, _, _, _ = load_inventory_workbook()
    if master_df.empty or column not in master_df.columns:
        return []
    series = master_df[column].dropna().astype(str).str.strip()
    values = sorted({v for v in series.tolist() if v})
    return values


def rename_product_value(column: str, old_value: str, new_value: str) -> bool:
    """Rename a value globally in the given product column.

    Returns True if any rows were updated. This is used by the admin
    Units & Labels screen to clean up container units, reorder labels,
    or distributors across all products.
    """

    if not new_value or column not in {"Container Unit", "Reorder Quantity", "Distributor"}:
        return False

    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    if master_df.empty or column not in master_df.columns:
        return False

    mask = master_df[column].astype(str) == str(old_value)
    if not mask.any():
        return False

    master_df.loc[mask, column] = new_value
    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)
    return True


def adjust_product_quantity(
    product_name: str,
    delta: float,
    user: str,
    location: str = "",
    notes: str = "",
) -> Optional[Dict[str, Any]]:
    """Adjust Quantity on Hand for a product and log to All Transactions.

    Returns dict with updated product info (including new quantity) or None
    if the product was not found.
    """

    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            return sqlite_adjust_product_quantity(
                DB_PATH,
                product_name=product_name,
                delta=delta,
                user=user,
                location=location,
                notes=notes,
            )
        except Exception:
            pass

    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    if master_df.empty or "Product Name" not in master_df.columns or "Quantity on Hand" not in master_df.columns:
        return None

    mask = master_df["Product Name"].astype(str) == str(product_name)
    if not mask.any():
        return None

    current_qty = float(master_df.loc[mask, "Quantity on Hand"].iloc[0] or 0)
    new_qty = current_qty + float(delta)
    if new_qty < 0:
        new_qty = 0

    master_df.loc[mask, "Quantity on Hand"] = new_qty

    # Prepare transaction row
    ts = datetime.utcnow().isoformat()
    row = master_df[mask].iloc[0].fillna("")
    tx_entry = {
        "Timestamp": ts,
        "User": user,
        "Product Name": row.get("Product Name", ""),
        "Delta": float(delta),
        "New Quantity on Hand": new_qty,
        "Location": location or row.get("Location", ""),
        "Notes": notes,
    }

    # Ensure tx_df has needed columns
    if tx_df.empty:
        tx_df = pd.DataFrame(columns=list(tx_entry.keys()))
    for col in tx_entry.keys():
        if col not in tx_df.columns:
            tx_df[col] = ""

    tx_df = pd.concat([tx_df, pd.DataFrame([tx_entry])], ignore_index=True)

    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)

    return {
        "product_name": row.get("Product Name", ""),
        "old_quantity": current_qty,
        "new_quantity": new_qty,
        "location": tx_entry["Location"],
    }


def get_product_by_name(product_name: str) -> Optional[Dict[str, Any]]:
    """Return a single product row by Product Name, or None if not found."""
    master_df, _, _, _ = load_inventory_workbook()
    if master_df.empty or "Product Name" not in master_df.columns:
        return None

    mask = master_df["Product Name"].astype(str) == str(product_name)
    if not mask.any():
        return None

    row = master_df[mask].iloc[0].fillna("")
    # Ensure Cost Per Unit key exists for the form
    if "Cost Per Unit" not in row.index:
        row["Cost Per Unit"] = ""
    return row.to_dict()


def update_product(original_name: str, data: Dict[str, Any]) -> bool:
    """Update a product identified by its original Product Name.

    Returns True if an existing row was updated, False if no matching row
    was found. This treats Product Name as the key.
    """

    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    if master_df.empty or "Product Name" not in master_df.columns:
        return False

    mask = master_df["Product Name"].astype(str) == str(original_name)
    if not mask.any():
        return False

    # Update columns; if a new column is introduced (e.g. Image Path), add it.
    for col, value in data.items():
        if col not in master_df.columns:
            master_df[col] = ""
        master_df.loc[mask, col] = value

    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)
    return True


def get_low_stock_products() -> List[Dict[str, Any]]:
    master_df, _, _, _ = load_inventory_workbook()
    if master_df.empty:
        return []
    if "Quantity on Hand" not in master_df.columns or "Reorder Threshold" not in master_df.columns:
        return []
    mask = master_df["Quantity on Hand"] <= master_df["Reorder Threshold"]
    low_df = master_df[mask].copy()
    low_df = low_df.fillna("")
    return low_df.to_dict(orient="records")


def get_low_stock_grouped_by_vendor() -> Dict[str, List[Dict[str, Any]]]:
    """Return low stock items grouped by Distributor (vendor).

    {"Vendor A": [item1, item2], "Vendor B": [...], ...}
    """
    items = get_low_stock_products()
    grouped: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        vendor = item.get("Distributor") or "(No Vendor)"
        grouped[vendor].append(item)
    return dict(grouped)


def add_product(data: Dict[str, Any]) -> None:
    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    new_row = pd.DataFrame([data])
    master_df = pd.concat([master_df, new_row], ignore_index=True)
    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)


def _get_vendor_contact(vendor_name: str) -> Optional[Dict[str, Any]]:
    """Look up a vendor row in the Vendors sheet by Vendor Name."""
    _, _, vendors_df, _ = load_inventory_workbook()
    if vendors_df.empty or "Vendor Name" not in vendors_df.columns:
        return None

    match = vendors_df[vendors_df["Vendor Name"].astype(str) == str(vendor_name)]
    if match.empty:
        return None
    row = match.iloc[0].fillna("")
    return row.to_dict()


def send_reorder_email(
    vendor: str,
    items_description: str,
    notes: str = "",
    extra_cc: Optional[List[str]] = None,
    order_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send a reorder email to the vendor.

    Returns True if the email was sent without raising, False otherwise.
    """
    contact = _get_vendor_contact(vendor)
    if not contact:
        return False

    to_email = str(contact.get("Email", "")).strip()
    cc_raw = str(contact.get("CC Emails", ""))
    cc_emails = [e.strip() for e in cc_raw.split(",") if e.strip()]
    vendor_notes = str(contact.get("Notes", "")).strip()

    if not to_email:
        return False

    settings = load_settings()
    smtp_cfg = _resolve_smtp_config(settings)
    prefix = settings.get("email_subject_prefix", "Reorder Request - ")
    default_cc_raw = settings.get("default_email_cc", "")
    default_cc = [e.strip() for e in str(default_cc_raw).split(",") if e.strip()]

    subject = f"{prefix}{vendor}"

    delivery_method = str((order_meta or {}).get("delivery_method") or "SHIP").upper()
    po_number = str((order_meta or {}).get("po_number") or "").strip()
    pickup_by = str((order_meta or {}).get("pickup_by") or "").strip()
    needed_by = str((order_meta or {}).get("needed_by") or "").strip()
    delivery_notes = str((order_meta or {}).get("delivery_notes") or "").strip()

    delivery_label = "Ship to our address" if delivery_method != "PICKUP" else "Pickup (we will pick up)"

    body_lines = [
        f"Vendor: {vendor}",
        f"PO Number: {po_number}" if po_number else "",
        f"Delivery: {delivery_label}",
    ]
    if pickup_by:
        body_lines.append(f"Pickup By: {pickup_by}")
    if needed_by:
        body_lines.append(f"Needed By: {needed_by}")
    if delivery_notes:
        body_lines.append(f"Delivery Notes: {delivery_notes}")
    body_lines = [line for line in body_lines if line]
    body_lines.extend(
        [
            "",
            "The following items are requested for reorder:",
            items_description,
        ]
    )
    if notes:
        body_lines.extend(["", f"Request Notes: {notes}"])
    if vendor_notes:
        body_lines.extend(["", f"Vendor Notes: {vendor_notes}"])

    footer = settings.get("email_footer", "")
    if footer:
        body_lines.extend(["", footer])

    # Append company branding if available
    company_name = settings.get("company_name", "").strip()
    company_address = settings.get("company_address", "").strip()
    company_phone = settings.get("company_phone", "").strip()
    branding_lines = [line for line in [company_name, company_address, company_phone] if line]
    if branding_lines:
        body_lines.extend(["", *branding_lines])

    body = "\n".join(body_lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_email"]
    msg["To"] = to_email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg.set_content(body)

    # Optional PDF purchase order attachment
    try:
        from fpdf import FPDF

        def _parse_items(items_text: str) -> List[Dict[str, str]]:
            rows: List[Dict[str, str]] = []
            parts = [p.strip() for p in str(items_text).split(";") if p.strip()]
            for part in parts:
                product_name = ""
                qty = ""
                unit_label = ""
                location = ""
                try:
                    if "Order:" in part:
                        name_part, rest = part.split("Order:", 1)
                        product_name = name_part.split("–", 1)[0].strip()
                        rest = rest.strip()
                        tokens = rest.split()
                        if tokens:
                            qty = tokens[0]
                        if len(tokens) >= 2:
                            unit_label = tokens[1]
                        if "Location:" in part:
                            location = part.split("Location:", 1)[1].strip().rstrip(")")
                except Exception:
                    continue

                if product_name:
                    row = {
                        "product": product_name,
                        "qty": qty,
                        "unit": unit_label,
                        "location": location,
                    }

                    # Enrich with product metadata if available
                    try:
                        p = get_product_by_name(product_name)
                    except Exception:
                        p = None
                    if isinstance(p, dict):
                        row["container_unit"] = str(p.get("Container Unit", "")).strip()
                        pack = (
                            str(p.get("Units Per Case", "")).strip()
                            or str(p.get("Pack Size", "")).strip()
                            or str(p.get("Case Pack", "")).strip()
                            or str(p.get("Pieces Per Case", "")).strip()
                        )
                        if pack:
                            row["pack"] = pack

                    rows.append(row)
            return rows

        def _pdf_bytes() -> bytes:
            settings_local = settings
            company_name_local = str(settings_local.get("company_name", "")).strip() or "Purchase Order"
            company_address_local = str(settings_local.get("company_address", "")).strip()
            company_phone_local = str(settings_local.get("company_phone", "")).strip()
            logo_path = str(settings_local.get("company_logo_path", "")).strip()

            pickup_footer = str(settings_local.get("po_footer_pickup", "")).strip()
            ship_footer = str(settings_local.get("po_footer_ship", "")).strip()
            footer_text = pickup_footer if delivery_method == "PICKUP" else ship_footer

            pdf = FPDF(orientation="P", unit="mm", format="Letter")
            pdf.set_auto_page_break(auto=True, margin=12)
            pdf.add_page()

            if logo_path:
                try:
                    abs_logo = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", logo_path)
                    if os.path.exists(abs_logo):
                        pdf.image(abs_logo, x=12, y=12, w=28)
                except Exception:
                    pass

            pdf.set_xy(44, 12)
            pdf.set_font("Helvetica", "B", 16)
            pdf.cell(0, 8, company_name_local, ln=1)
            pdf.set_font("Helvetica", "", 10)
            if company_address_local:
                for line in company_address_local.splitlines():
                    pdf.set_x(44)
                    pdf.cell(0, 5, line, ln=1)
            if company_phone_local:
                pdf.set_x(44)
                pdf.cell(0, 5, company_phone_local, ln=1)

            pdf.ln(6)
            pdf.set_x(12)
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 7, "Purchase Order", ln=1)
            pdf.set_font("Helvetica", "", 10)
            if po_number:
                pdf.cell(0, 5, f"PO Number: {po_number}", ln=1)
            pdf.cell(0, 5, f"Vendor: {vendor}", ln=1)
            pdf.cell(0, 5, f"Delivery: {delivery_label}", ln=1)
            if pickup_by:
                pdf.cell(0, 5, f"Pickup By: {pickup_by}", ln=1)
            if needed_by:
                pdf.cell(0, 5, f"Needed By: {needed_by}", ln=1)
            if delivery_notes:
                pdf.multi_cell(0, 5, f"Delivery Notes: {delivery_notes}")
            approved_by = str((order_meta or {}).get("approved_by") or "").strip()
            if approved_by:
                pdf.cell(0, 5, f"Approved By: {approved_by}", ln=1)

            pdf.ln(4)

            items = _parse_items(items_description)
            if not items:
                pdf.multi_cell(0, 5, "No line items found.")
            else:
                headers = ["Product", "Qty", "Unit", "Container", "Pack", "Location"]
                col_w = [70, 12, 18, 22, 18, 45]
                pdf.set_font("Helvetica", "B", 9)
                for i, h in enumerate(headers):
                    pdf.cell(col_w[i], 7, h, border=1)
                pdf.ln()
                pdf.set_font("Helvetica", "", 9)
                for it in items:
                    pdf.cell(col_w[0], 6, str(it.get("product", ""))[:45], border=1)
                    pdf.cell(col_w[1], 6, str(it.get("qty", ""))[:10], border=1)
                    pdf.cell(col_w[2], 6, str(it.get("unit", ""))[:12], border=1)
                    pdf.cell(col_w[3], 6, str(it.get("container_unit", ""))[:14], border=1)
                    pdf.cell(col_w[4], 6, str(it.get("pack", ""))[:12], border=1)
                    pdf.cell(col_w[5], 6, str(it.get("location", ""))[:30], border=1)
                    pdf.ln()

            if notes:
                pdf.ln(3)
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(0, 6, "Notes", ln=1)
                pdf.set_font("Helvetica", "", 10)
                pdf.multi_cell(0, 5, notes)

            if footer_text:
                pdf.ln(4)
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(0, 6, "Instructions", ln=1)
                pdf.set_font("Helvetica", "", 9)
                pdf.multi_cell(0, 4.5, footer_text)

            return bytes(pdf.output(dest="S"))

        pdf_data = _pdf_bytes()
        filename = f"purchase_order_{vendor}_{datetime.utcnow().date().isoformat()}.pdf"
        filename = re.sub(r"[^a-zA-Z0-9_.-]+", "_", filename)
        msg.add_attachment(pdf_data, maintype="application", subtype="pdf", filename=filename)
    except Exception:
        pass

    extra_cc = extra_cc or []
    recipients = [to_email] + cc_emails + default_cc + extra_cc

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
            if smtp_cfg["use_tls"]:
                server.starttls()
            if smtp_cfg["user"] and smtp_cfg["password"] and smtp_cfg["password"] != "CHANGE_ME":
                server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg, from_addr=smtp_cfg["from_email"], to_addrs=recipients)
        return True
    except Exception:
        # For now, swallow and signal failure; web UI can show FAILED status.
        return False


def log_reorder(
    user: str,
    ip: str,
    vendor: str,
    items_description: str,
    status: str = "PENDING",
    notes: str = "",
) -> None:
    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            sqlite_insert_reorder_log(
                DB_PATH,
                user=user,
                ip=ip,
                vendor=vendor,
                items_description=items_description,
                status=status,
                notes=notes,
            )
            return
        except Exception:
            pass

    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()

    timestamp = datetime.utcnow().isoformat()
    new_entry = {
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

    reorder_log_df = pd.concat([reorder_log_df, pd.DataFrame([new_entry])], ignore_index=True)
    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)


def get_reorder_log() -> List[Dict[str, Any]]:
    """Return all rows from the Reorder Log sheet as dicts."""
    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            return sqlite_get_reorder_log(DB_PATH)
        except Exception:
            pass

    _, _, _, reorder_log_df = load_inventory_workbook()
    if reorder_log_df.empty:
        return []
    return reorder_log_df.to_dict(orient="records")


def get_pending_reorders() -> List[Dict[str, Any]]:
    """Return only rows with Status == 'PENDING'."""
    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            return sqlite_get_pending_reorders(DB_PATH)
        except Exception:
            pass

    _, _, _, reorder_log_df = load_inventory_workbook()
    if reorder_log_df.empty or "Status" not in reorder_log_df.columns:
        return []
    pending_df = reorder_log_df[reorder_log_df["Status"] == "PENDING"]
    return pending_df.to_dict(orient="records")


def get_all_vendors() -> List[Dict[str, Any]]:
    """Return all vendors as dicts, ensuring standard columns exist."""
    _, _, vendors_df, _ = load_inventory_workbook()
    if vendors_df.empty:
        vendors_df = pd.DataFrame(columns=VENDOR_COLUMNS)

    for col in VENDOR_COLUMNS:
        if col not in vendors_df.columns:
            vendors_df[col] = ""

    vendors_df = vendors_df.fillna("")
    return vendors_df.to_dict(orient="records")


def upsert_vendor(data: Dict[str, Any]) -> None:
    """Insert or update a vendor row based on Vendor Name."""
    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    name = str(data.get("Vendor Name", "")).strip()
    if not name:
        return

    if vendors_df.empty:
        vendors_df = pd.DataFrame(columns=VENDOR_COLUMNS)

    for col in VENDOR_COLUMNS:
        if col not in vendors_df.columns:
            vendors_df[col] = ""

    vendors_df = vendors_df.fillna("")

    mask = vendors_df["Vendor Name"].astype(str) == name
    row_data = {col: data.get(col, "") for col in VENDOR_COLUMNS}

    if mask.any():
        # Update existing
        for col, value in row_data.items():
            vendors_df.loc[mask, col] = value
    else:
        # Insert new
        vendors_df = pd.concat([vendors_df, pd.DataFrame([row_data])], ignore_index=True)

    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)


def update_reorder_status(
    timestamp: str,
    vendor: str,
    new_status: str,
    approved_by: str,
    approved_ip: str,
    internal_notes: str = "",
    po_number: str = "",
    pickup_by: str = "",
    delivery_method: str = "",
    needed_by: str = "",
    delivery_notes: str = "",
) -> None:
    """Update the status and approval metadata for a reorder log row.

    Rows are identified by (Timestamp, Vendor). This is simple and good enough for
    this internal tool; if duplicates ever happen, all matching rows are updated.
    """
    backend = (os.environ.get("INVENTORY_BACKEND") or "excel").strip().lower()
    if backend == "sqlite":
        try:
            sqlite_update_reorder_status(
                DB_PATH,
                timestamp=timestamp,
                vendor=vendor,
                new_status=new_status,
                approved_by=approved_by,
                approved_ip=approved_ip,
                internal_notes=internal_notes,
                meta={
                    "PO Number": po_number,
                    "Pickup By": pickup_by,
                    "Delivery Method": delivery_method,
                    "Needed By": needed_by,
                    "Delivery Notes": delivery_notes,
                },
            )
            return
        except Exception:
            pass

    master_df, tx_df, vendors_df, reorder_log_df = load_inventory_workbook()
    if reorder_log_df.empty:
        return

    mask = (reorder_log_df["Timestamp"].astype(str) == str(timestamp)) & (
        reorder_log_df["Vendor"].astype(str) == str(vendor)
    )

    if not mask.any():
        return

    reorder_log_df.loc[mask, "Status"] = new_status
    now = datetime.utcnow().isoformat()
    reorder_log_df.loc[mask, "Approved Timestamp"] = now
    reorder_log_df.loc[mask, "Approved By"] = approved_by
    reorder_log_df.loc[mask, "Approved IP"] = approved_ip

    # Ensure Internal Notes column exists and store any provided notes
    if "Internal Notes" not in reorder_log_df.columns:
        reorder_log_df["Internal Notes"] = ""
    if internal_notes:
        reorder_log_df.loc[mask, "Internal Notes"] = internal_notes

    # Persist PO / delivery metadata for reporting
    for col_name, value in {
        "PO Number": po_number,
        "Pickup By": pickup_by,
        "Delivery Method": delivery_method,
        "Needed By": needed_by,
        "Delivery Notes": delivery_notes,
    }.items():
        if col_name not in reorder_log_df.columns:
            reorder_log_df[col_name] = ""
        if value:
            reorder_log_df.loc[mask, col_name] = value

    save_inventory_workbook(master_df, tx_df, vendors_df, reorder_log_df)


def get_products_for_vendor(vendor_name: str) -> List[Dict[str, Any]]:
    """Return all products whose Distributor matches the given vendor.

    Ensures Cost Per Unit exists for downstream pricing workflows.
    """
    master_df, _, _, _ = load_inventory_workbook()
    if master_df.empty or "Distributor" not in master_df.columns:
        return []

    if "Cost Per Unit" not in master_df.columns:
        master_df["Cost Per Unit"] = ""

    mask = master_df["Distributor"].astype(str) == str(vendor_name)
    subset = master_df[mask].copy()
    subset = subset.fillna("")
    return subset.to_dict(orient="records")


def send_pricing_request_email(
    vendor: str,
    products: List[Dict[str, Any]],
    notes: str = "",
    extra_cc: Optional[List[str]] = None,
) -> bool:
    """Send a pricing request email listing the given products for a vendor.

    This reuses the vendor contact information and email settings but uses a
    different subject prefix and body wording so vendors know it's a quote
    request instead of a reorder.
    """

    contact = _get_vendor_contact(vendor)
    if not contact:
        return False

    to_email = str(contact.get("Email", "")).strip()
    cc_raw = str(contact.get("CC Emails", ""))
    cc_emails = [e.strip() for e in cc_raw.split(",") if e.strip()]
    vendor_notes = str(contact.get("Notes", "")).strip()

    if not to_email:
        return False

    settings = load_settings()
    smtp_cfg = _resolve_smtp_config(settings)
    # Reuse email_subject_prefix but clarify this is a pricing request
    prefix = settings.get("email_subject_prefix", "Reorder Request - ")
    subject = f"Pricing Request - {vendor}"
    default_cc_raw = settings.get("default_email_cc", "")
    default_cc = [e.strip() for e in str(default_cc_raw).split(",") if e.strip()]

    lines: List[str] = [
        f"Vendor: {vendor}",
        "",
        "We are requesting current pricing for the following products:",
        "",
    ]

    if not products:
        lines.append("(No products specified)")
    else:
        for p in products:
            name = str(p.get("Product Name", "")).strip()
            unit = str(p.get("Container Unit", "")).strip()
            reorder_qty = str(p.get("Reorder Quantity", "")).strip()
            current_cost = str(p.get("Cost Per Unit", "")).strip()
            line = f"- {name} | Unit: {unit or '-'} | Label: {reorder_qty or '-'} | Current Cost: {current_cost or 'N/A'}"
            lines.append(line)

    if notes:
        lines.extend(["", f"Request Notes: {notes}"])
    if vendor_notes:
        lines.extend(["", f"Vendor Notes: {vendor_notes}"])

    footer = settings.get("email_footer", "")
    if footer:
        lines.extend(["", footer])

    # Append company branding if available
    company_name = settings.get("company_name", "").strip()
    company_address = settings.get("company_address", "").strip()
    company_phone = settings.get("company_phone", "").strip()
    branding_lines = [line for line in [company_name, company_address, company_phone] if line]
    if branding_lines:
        lines.extend(["", *branding_lines])

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg.set_content(body)

    extra_cc = extra_cc or []
    recipients = [to_email] + cc_emails + default_cc + extra_cc

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
            if smtp_cfg["use_tls"]:
                server.starttls()
            if smtp_cfg["user"] and smtp_cfg["password"] and smtp_cfg["password"] != "CHANGE_ME":
                server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg, from_addr=smtp_cfg["from_email"], to_addrs=recipients)
        return True
    except Exception:
        return False


def get_reorder_analytics(start_date: Optional[str], end_date: Optional[str]) -> Dict[str, Any]:
    """Compute basic spend analytics from the Reorder Log using Cost Per Unit.

    Dates are expected as ISO strings (YYYY-MM-DD). We only count rows with
    Status == "SENT" so that only actually-sent reorders impact spend.
    """

    master_df, _, _, reorder_log_df = load_inventory_workbook()

    # Compute effective date range first so the dashboard date inputs always prefill.
    today = datetime.utcnow().date()
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date).date()
        except ValueError:
            start_dt = datetime(today.year, today.month, 1).date()
    else:
        start_dt = datetime(today.year, today.month, 1).date()

    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date).date()
        except ValueError:
            # Default: last day of current month
            next_month = (datetime(today.year, today.month, 28) + pd.Timedelta(days=4)).date().replace(day=1)
            end_dt = next_month - pd.Timedelta(days=1)
            end_dt = end_dt.date() if hasattr(end_dt, "date") else end_dt
    else:
        next_month = (datetime(today.year, today.month, 28) + pd.Timedelta(days=4)).date().replace(day=1)
        end_dt = next_month - pd.Timedelta(days=1)
        end_dt = end_dt.date() if hasattr(end_dt, "date") else end_dt

    # Default empty structure for when there is no data
    empty_result: Dict[str, Any] = {
        "total_spend": 0.0,
        "total_orders": 0,
        "total_vendors": 0,
        "top_vendor": None,
        "top_product": None,
        "spend_by_day": {"labels": [], "data": []},
        "spend_by_vendor": {"labels": [], "data": []},
        "top_products": {"labels": [], "data": []},
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
    }

    if reorder_log_df.empty or "Status" not in reorder_log_df.columns:
        return empty_result

    # Parse timestamps
    if "Timestamp" not in reorder_log_df.columns:
        return empty_result

    df = reorder_log_df.copy()
    df["Timestamp_dt"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df.dropna(subset=["Timestamp_dt"])
    if df.empty:
        return empty_result

    # Filter by status (only SENT counted as real spend)
    df = df[df["Status"] == "SENT"]
    if df.empty:
        return empty_result

    # Date range filtering (use the effective range computed above)

    mask = (df["Timestamp_dt"].dt.date >= start_dt) & (df["Timestamp_dt"].dt.date <= end_dt)
    df = df[mask]
    if df.empty:
        return empty_result

    # Build item-level rows by parsing the Items description field.
    item_rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        vendor = str(row.get("Vendor", ""))
        ts_date = row["Timestamp_dt"].date()
        items_text = str(row.get("Items", ""))
        if not items_text:
            continue
        # Items are stored as "; "-separated descriptions
        parts = [p.strip() for p in items_text.split(";") if p.strip()]
        for part in parts:
            # Expected format: "Product Name – Order: X UNIT (Suggested: ... )"
            product_name = ""
            quantity = 0.0

            try:
                # Split on the en dash / hyphen sequence before "Order:"
                if "Order:" in part:
                    name_part, rest = part.split("Order:", 1)
                    # name_part like "Product Name – "
                    product_name = name_part.split("–", 1)[0].strip()

                    # rest like " X UNIT (Suggested: ... )"
                    rest = rest.strip()
                    # quantity is first token
                    qty_token = rest.split()[0]
                    quantity = float(qty_token)
                else:
                    # Fallback: treat whole part as name with qty 0
                    product_name = part.strip()
            except Exception:
                # If parsing fails, skip this line but keep others
                continue

            if not product_name:
                continue

            item_rows.append(
                {
                    "date": ts_date,
                    "vendor": vendor,
                    "product": product_name,
                    "quantity": quantity,
                }
            )

    if not item_rows:
        return empty_result

    items_df = pd.DataFrame(item_rows)

    # Prepare master data for join: Product Name + Cost Per Unit + Distributor
    if master_df.empty or "Product Name" not in master_df.columns:
        # We can still return volume-based metrics without cost
        items_df["Cost Per Unit"] = 0.0
        items_df["spend"] = 0.0
    else:
        if "Cost Per Unit" not in master_df.columns:
            master_df["Cost Per Unit"] = 0.0
        if "Distributor" not in master_df.columns:
            master_df["Distributor"] = ""

        cost_df = master_df[["Product Name", "Distributor", "Cost Per Unit"]].copy()
        cost_df["Cost Per Unit"] = pd.to_numeric(cost_df["Cost Per Unit"], errors="coerce").fillna(0.0)

        merged = items_df.merge(
            cost_df,
            left_on="product",
            right_on="Product Name",
            how="left",
        )

        merged["Cost Per Unit"] = pd.to_numeric(merged["Cost Per Unit"], errors="coerce").fillna(0.0)
        merged["spend"] = merged["quantity"] * merged["Cost Per Unit"]
        items_df = merged

    total_spend = float(items_df["spend"].sum()) if "spend" in items_df.columns else 0.0
    total_orders = int(df.shape[0])
    total_vendors = int(df["Vendor"].nunique()) if "Vendor" in df.columns else 0

    # Spend by day
    if "spend" in items_df.columns:
        by_day = items_df.groupby("date")["spend"].sum().sort_index()
        spend_by_day = {
            "labels": [d.isoformat() for d in by_day.index],
            "data": [round(float(v), 2) for v in by_day.values],
        }
    else:
        spend_by_day = {"labels": [], "data": []}

    # Spend by vendor
    if "spend" in items_df.columns:
        by_vendor = items_df.groupby("vendor")["spend"].sum().sort_values(ascending=False)
        spend_by_vendor = {
            "labels": [str(k) for k in by_vendor.index],
            "data": [round(float(v), 2) for v in by_vendor.values],
        }
        top_vendor = (
            {"name": str(by_vendor.index[0]), "spend": round(float(by_vendor.iloc[0]), 2)}
            if not by_vendor.empty
            else None
        )
    else:
        spend_by_vendor = {"labels": [], "data": []}
        top_vendor = None

    # Top products by spend
    if "spend" in items_df.columns:
        by_product = items_df.groupby("product")["spend"].sum().sort_values(ascending=False).head(10)
        top_products = {
            "labels": [str(k) for k in by_product.index],
            "data": [round(float(v), 2) for v in by_product.values],
        }
        top_product = (
            {"name": str(by_product.index[0]), "spend": round(float(by_product.iloc[0]), 2)}
            if not by_product.empty
            else None
        )
    else:
        top_products = {"labels": [], "data": []}
        top_product = None

    return {
        "total_spend": round(total_spend, 2),
        "total_orders": total_orders,
        "total_vendors": total_vendors,
        "top_vendor": top_vendor,
        "top_product": top_product,
        "spend_by_day": spend_by_day,
        "spend_by_vendor": spend_by_vendor,
        "top_products": top_products,
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
    }
