from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from config import SECRET_KEY, BASE_DIR, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL, DB_PATH
import os
import json
import hashlib
import io
import calendar
from functools import wraps
from datetime import date, timedelta
import smtplib
import re
import pandas as pd
from inventory.sqlite_db import get_counts as sqlite_get_counts
from inventory.sqlite_db import import_from_excel as sqlite_import_from_excel
from inventory.sqlite_db import init_db as sqlite_init_db
from inventory.services import (
    get_all_products,
    get_low_stock_products,
    get_low_stock_grouped_by_vendor,
    add_product,
    log_reorder,
    get_pending_reorders,
    get_reorder_log,
    update_reorder_status,
    send_reorder_email,
    get_all_vendors,
    upsert_vendor,
    load_settings,
    save_settings,
    get_reorder_analytics,
    get_products_for_vendor,
    send_pricing_request_email,
    get_product_by_name,
    update_product,
    get_distinct_product_values,
    rename_product_value,
    adjust_product_quantity,
    send_basic_email,
    send_html_email,
)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads")

    # If session.permanent is set, keep the login for 30 days.
    app.permanent_session_lifetime = timedelta(days=30)

    # Load settings once for header use; can be refreshed after save.
    app.config["APP_SETTINGS"] = load_settings()

    users_path = os.path.join(BASE_DIR, "users.json")
    account_requests_path = os.path.join(BASE_DIR, "account_requests.json")

    role_rank = {"VIEW": 0, "REQUEST": 1, "APPROVER": 2, "ADMIN": 3}

    def _hash_password(username: str, password: str) -> str:
        data = f"{username}:{password}".encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _default_admin_user() -> dict:
        return {
            "username": "admin",
            "password_hash": _hash_password("admin", "admin"),
            "role": "ADMIN",
        }

    def _save_product_image(file_storage, product_name: str) -> str:
        if not file_storage or not getattr(file_storage, "filename", ""):
            return ""

        _, ext = os.path.splitext(file_storage.filename)
        ext = ext.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return ""

        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", product_name).strip("_") or "product"
        rel_dir = os.path.join("uploads", "product_images")
        abs_dir = os.path.join(BASE_DIR, "static", rel_dir)
        os.makedirs(abs_dir, exist_ok=True)

        filename = f"{safe_name}{ext}"
        abs_path = os.path.join(abs_dir, filename)
        file_storage.save(abs_path)

        return os.path.join(rel_dir, filename).replace("\\", "/")

    def _load_users() -> list[dict]:
        """Load users from users.json, creating a default admin/admin if needed.

        If the file is missing, empty, or has invalid data (e.g. blank password_hash),
        it will be replaced with a single ADMIN user with username/password "admin".
        """
        if not os.path.exists(users_path):
            default_user = _default_admin_user()
            with open(users_path, "w", encoding="utf-8") as f:
                json.dump([default_user], f, indent=2)
            return [default_user]

        try:
            with open(users_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []

        # Ensure there is at least one valid user with a non-empty password_hash
        if not isinstance(data, list) or not data:
            data = [_default_admin_user()]
        else:
            # Fix any users that are missing password hashes by regenerating default admin only
            valid = [u for u in data if u.get("username") and u.get("password_hash")]
            if not valid:
                data = [_default_admin_user()]

        with open(users_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data

    def _save_users(users: list[dict]) -> None:
        try:
            with open(users_path, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2)
        except Exception:
            # Non-fatal; ignore write errors for now.
            pass

    def _find_user(username: str) -> dict | None:
        users = _load_users()
        for u in users:
            if u.get("username") == username:
                return u
        return None

    def _check_credentials(username: str, password: str) -> dict | None:
        user = _find_user(username)
        if not user:
            return None
        expected = user.get("password_hash", "")
        if expected and expected == _hash_password(username, password):
            return user
        return None

    def login_required(min_role: str = "VIEW"):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                username = session.get("username")
                role = session.get("role", "VIEW")
                if not username:
                    flash("Please log in to access this page.", "warning")
                    return redirect(url_for("login", next=request.path))
                if role_rank.get(role, 0) < role_rank.get(min_role, 0):
                    flash("You do not have permission to access this page.", "danger")
                    return redirect(url_for("index"))
                return func(*args, **kwargs)

            return wrapper

        return decorator

    @app.route("/request-account", methods=["GET", "POST"])
    def request_account():
        """Allow a user to request an account. Sends an email to the admin and logs the request."""

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            desired_username = request.form.get("desired_username", "").strip()
            notes = request.form.get("notes", "").strip()

            if not email or not desired_username:
                flash("Email and desired username are required.", "danger")
            else:
                # Log to JSON file for record
                try:
                    if os.path.exists(account_requests_path):
                        with open(account_requests_path, "r", encoding="utf-8") as f:
                            existing = json.load(f) or []
                    else:
                        existing = []
                except Exception:
                    existing = []

                entry = {
                    "full_name": full_name,
                    "email": email,
                    "desired_username": desired_username,
                    "notes": notes,
                }
                existing.append(entry)
                try:
                    with open(account_requests_path, "w", encoding="utf-8") as f:
                        json.dump(existing, f, indent=2)
                except Exception:
                    # Non-fatal
                    pass

                # Send a notification email to the admin address
                subject = f"Account Request - {desired_username}"
                body_lines = [
                    "A new account request has been submitted:",
                    "",
                    f"Name: {full_name or '(not provided)'}",
                    f"Email: {email}",
                    f"Desired Username: {desired_username}",
                    "",
                    f"Notes: {notes or '(none)'}",
                ]
                body = "\n".join(body_lines)

                msg = f"Subject: {subject}\nFrom: {FROM_EMAIL}\nTo: {FROM_EMAIL}\n\n{body}"

                try:
                    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                        server.starttls()
                        if SMTP_USER and SMTP_PASS and SMTP_PASS != "CHANGE_ME":
                            server.login(SMTP_USER, SMTP_PASS)
                        server.sendmail(FROM_EMAIL, [FROM_EMAIL], msg)
                    flash("Account request submitted. We will contact you once it is reviewed.", "success")
                except Exception:
                    flash("Account request logged, but email notification failed.", "warning")

                return redirect(url_for("login"))

        return render_template("request_account.html", settings=app.config["APP_SETTINGS"])

    @app.route("/account-requests")
    @login_required("ADMIN")
    def account_requests_view():
        try:
            if os.path.exists(account_requests_path):
                with open(account_requests_path, "r", encoding="utf-8") as f:
                    requests_data = json.load(f) or []
            else:
                requests_data = []
        except Exception:
            requests_data = []

        return render_template(
            "account_requests.html",
            requests=requests_data,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/users", methods=["GET", "POST"])
    @login_required("ADMIN")
    def manage_users():
        users = _load_users()

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "VIEW").strip().upper() or "VIEW"
            display_name = request.form.get("display_name", "").strip()
            license_number = request.form.get("license_number", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            default_truck = request.form.get("default_truck", "").strip()
            photo = request.files.get("photo")

            if not username:
                flash("Username is required.", "danger")
                return redirect(url_for("manage_users"))

            # Normalize role to one of the known roles
            if role not in role_rank:
                role = "VIEW"

            # Find existing user or create new
            existing = None
            for u in users:
                if u.get("username") == username:
                    existing = u
                    break

            if existing is None:
                existing = {
                    "username": username,
                    "password_hash": "",
                    "role": role,
                }
                users.append(existing)

            existing["role"] = role
            existing["display_name"] = display_name
            existing["license_number"] = license_number
            existing["phone"] = phone
            existing["email"] = email
            existing["default_truck"] = default_truck

            if photo and photo.filename:
                _, ext = os.path.splitext(photo.filename)
                ext = ext.lower()
                if ext in {".png", ".jpg", ".jpeg", ".gif"}:
                    safe_user = re.sub(r"[^a-zA-Z0-9_-]+", "_", username) or "user"
                    rel_dir = os.path.join("uploads", "tech_photos")
                    abs_dir = os.path.join(BASE_DIR, "static", rel_dir)
                    os.makedirs(abs_dir, exist_ok=True)
                    filename = f"{safe_user}{ext}"
                    abs_path = os.path.join(abs_dir, filename)
                    photo.save(abs_path)
                    existing["photo_path"] = os.path.join(rel_dir, filename).replace("\\", "/")

            if password:
                existing["password_hash"] = _hash_password(username, password)

            _save_users(users)
            flash("User saved.", "success")
            return redirect(url_for("manage_users"))

        # Do not expose password hashes to the template
        display_users = [
            {
                "username": u.get("username", ""),
                "role": u.get("role", "VIEW"),
                "display_name": u.get("display_name", ""),
                "license_number": u.get("license_number", ""),
                "photo_path": u.get("photo_path", ""),
                "phone": u.get("phone", ""),
                "email": u.get("email", ""),
                "default_truck": u.get("default_truck", ""),
            }
            for u in users
        ]

        return render_template(
            "users.html",
            users=display_users,
            roles=list(role_rank.keys()),
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = _check_credentials(username, password)
            if user:
                session["username"] = user["username"]
                session["role"] = user.get("role", "VIEW")
                session["display_name"] = user.get("display_name", "")
                session["license_number"] = user.get("license_number", "")
                session["photo_path"] = user.get("photo_path", "")
                session["phone"] = user.get("phone", "")
                session["email"] = user.get("email", "")
                session["default_truck"] = user.get("default_truck", "")
                # Respect remember-me checkbox: make session persistent if checked.
                remember = request.form.get("remember_me")
                session.permanent = bool(remember)
                flash("Logged in successfully.", "success")
                next_url = request.args.get("next") or url_for("index")
                return redirect(next_url)
            flash("Invalid username or password.", "danger")
        return render_template("login.html", settings=app.config["APP_SETTINGS"])

    @app.route("/db", methods=["GET", "POST"])
    @login_required("ADMIN")
    def db_admin():
        if request.method == "POST":
            action = request.form.get("action", "").strip()
            if action == "init":
                sqlite_init_db(DB_PATH)
                flash("SQLite database initialized.", "success")
            elif action == "import":
                counts = sqlite_import_from_excel(DB_PATH)
                flash(
                    f"Imported Excel → SQLite. Products={counts.get('products', 0)}, Vendors={counts.get('vendors', 0)}, Reorders={counts.get('reorders', 0)}, Transactions={counts.get('transactions', 0)}",
                    "success",
                )
            else:
                flash("Unknown database action.", "warning")
            return redirect(url_for("db_admin"))

        try:
            counts = sqlite_get_counts(DB_PATH)
        except Exception:
            counts = None

        return render_template(
            "db_admin.html",
            settings=app.config["APP_SETTINGS"],
            db_path=DB_PATH,
            counts=counts,
        )

    @app.route("/logout")
    def logout():
        session.pop("username", None)
        session.pop("role", None)
        session.pop("display_name", None)
        session.pop("license_number", None)
        session.pop("photo_path", None)
        session.pop("phone", None)
        session.pop("email", None)
        session.pop("default_truck", None)
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required("VIEW")
    def index():
        low_stock = get_low_stock_products()
        total_products = len(get_all_products())

        # Optional date range filters for analytics (YYYY-MM-DD)
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        if not start_date or not end_date:
            today = date.today()
            start_date = date(today.year, today.month, 1).isoformat()
            last_day = calendar.monthrange(today.year, today.month)[1]
            end_date = date(today.year, today.month, last_day).isoformat()
        analytics = get_reorder_analytics(start_date, end_date)

        return render_template(
            "index.html",
            low_stock=low_stock,
            total_products=total_products,
            analytics=analytics,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/products")
    @login_required("VIEW")
    def products():
        products_list = get_all_products()

        # Optional simple text search across name, category, distributor, and location
        q = request.args.get("q", "").strip().lower()
        if q:
            def _matches(p: dict) -> bool:
                fields = [
                    str(p.get("Product Name", "")),
                    str(p.get("Category", "")),
                    str(p.get("Distributor", "")),
                    str(p.get("Location", "")),
                ]
                return any(q in value.lower() for value in fields)

            products_list = [p for p in products_list if _matches(p)]

        return render_template(
            "products.html",
            products=products_list,
            query=q,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/products/new", methods=["GET", "POST"])
    @login_required("ADMIN")
    def new_product():
        if request.method == "POST":
            form = request.form
            # Handle "Other..." entries for dropdowns
            def _resolve_select(base_name: str) -> str:
                val = form.get(base_name, "").strip()
                if val == "__other__":
                    return form.get(f"{base_name}_other", "").strip()
                return val

            data = {
                "Product Name": form.get("product_name", "").strip(),
                "Category": form.get("category", "").strip(),
                "Quantity on Hand": float(form.get("quantity_on_hand", 0) or 0),
                "Container Unit": _resolve_select("container_unit"),
                "Reorder Threshold": float(form.get("reorder_threshold", 0) or 0),
                "Reorder Amount": float(form.get("reorder_amount", 0) or 0),
                "Reorder Quantity": _resolve_select("reorder_quantity"),
                "Distributor": _resolve_select("distributor"),
                "Location": form.get("location", "").strip(),
                "Cost Per Unit": form.get("cost_per_unit", "").strip(),
                "EPA Registration Number": form.get("epa_reg_no", "").strip(),
                "EPA Establishment Number": form.get("epa_est_no", "").strip(),
            }

            image_file = request.files.get("product_image")
            image_path = _save_product_image(image_file, data.get("Product Name", ""))
            if image_path:
                data["Image Path"] = image_path

            add_product(data)
            flash("Product added successfully.", "success")
            return redirect(url_for("products"))

        # Build dropdown options from existing data plus some standard units
        distributors = get_distinct_product_values("Distributor")
        container_units = get_distinct_product_values("Container Unit")
        # Seed with common units if not already present
        standard_units = ["fl oz", "oz", "gal", "quart", "pint", "lb", "unit"]
        for u in standard_units:
            if u not in container_units:
                container_units.append(u)
        container_units = sorted(container_units)

        reorder_labels = get_distinct_product_values("Reorder Quantity")

        return render_template(
            "product_form.html",
            distributors=distributors,
            container_units=container_units,
            reorder_labels=reorder_labels,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/products/<product_name>/edit", methods=["GET", "POST"])
    @login_required("ADMIN")
    def edit_product(product_name: str):
        # Look up the existing product row
        existing = get_product_by_name(product_name)
        if not existing:
            flash("Product not found.", "danger")
            return redirect(url_for("products"))

        if request.method == "POST":
            form = request.form

            # We allow changing the product name but still key off the original
            new_name = form.get("product_name", existing.get("Product Name", "")).strip()

            def _to_float(field_name: str, default: float = 0.0) -> float:
                raw = form.get(field_name, "").strip()
                if not raw:
                    return default
                try:
                    return float(raw)
                except ValueError:
                    return default

            def _resolve_select(base_name: str, current: str) -> str:
                val = form.get(base_name, "").strip()
                if val == "__other__":
                    other = form.get(f"{base_name}_other", "").strip()
                    return other or current
                return val or current

            update_data = {
                "Product Name": new_name,
                "Category": form.get("category", existing.get("Category", "")).strip(),
                "Quantity on Hand": _to_float("quantity_on_hand", float(existing.get("Quantity on Hand", 0) or 0)),
                "Container Unit": _resolve_select("container_unit", existing.get("Container Unit", "")),
                "Reorder Threshold": _to_float("reorder_threshold", float(existing.get("Reorder Threshold", 0) or 0)),
                "Reorder Amount": _to_float("reorder_amount", float(existing.get("Reorder Amount", 0) or 0)),
                "Reorder Quantity": _resolve_select("reorder_quantity", existing.get("Reorder Quantity", "")),
                "Distributor": _resolve_select("distributor", existing.get("Distributor", "")),
                "Location": form.get("location", existing.get("Location", "")).strip(),
                "Cost Per Unit": form.get("cost_per_unit", existing.get("Cost Per Unit", "")).strip(),
                "EPA Registration Number": form.get("epa_reg_no", existing.get("EPA Registration Number", "")).strip(),
                "EPA Establishment Number": form.get("epa_est_no", existing.get("EPA Establishment Number", "")).strip(),
            }

            image_file = request.files.get("product_image")
            image_path = _save_product_image(image_file, new_name)
            if image_path:
                update_data["Image Path"] = image_path
            else:
                if existing.get("Image Path"):
                    update_data["Image Path"] = existing.get("Image Path")

            ok = update_product(product_name, update_data)
            if ok:
                flash("Product updated.", "success")
            else:
                flash("Unable to update product.", "danger")
            return redirect(url_for("products"))

        # GET – prefill the form with existing values and dropdown options
        distributors = get_distinct_product_values("Distributor")
        container_units = get_distinct_product_values("Container Unit")
        standard_units = ["fl oz", "oz", "gal", "quart", "pint", "lb", "unit"]
        for u in standard_units:
            if u not in container_units:
                container_units.append(u)
        container_units = sorted(container_units)

        reorder_labels = get_distinct_product_values("Reorder Quantity")

        return render_template(
            "product_edit.html",
            product=existing,
            distributors=distributors,
            container_units=container_units,
            reorder_labels=reorder_labels,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/products/<product_name>/request", methods=["GET", "POST"])
    @login_required("REQUEST")
    def request_product(product_name: str):
        product = get_product_by_name(product_name)
        if not product:
            flash("Product not found.", "danger")
            return redirect(url_for("products"))

        if request.method == "POST":
            form = request.form
            user = session.get("username", "web-user")
            ip = request.remote_addr or ""
            vendor = str(product.get("Distributor") or "(No Vendor)")

            amount_raw = (form.get("order_amount") or "").strip()
            notes = (form.get("notes") or "").strip()

            try:
                amount = float(amount_raw)
            except ValueError:
                amount = 0.0

            if amount <= 0:
                flash("Enter a positive order amount.", "danger")
                return redirect(url_for("request_product", product_name=product_name))

            reorder_qty_label = str(product.get("Reorder Quantity") or "")
            location = str(product.get("Location") or "")
            suggested_amount = str(product.get("Reorder Amount") or "")
            item_desc = (
                f"{product.get('Product Name', product_name)} – Order: {amount} {reorder_qty_label} "
                f"(Suggested: {suggested_amount} {reorder_qty_label}, Location: {location})"
            )

            log_reorder(
                user=user,
                ip=ip,
                vendor=vendor,
                items_description=item_desc,
                status="PENDING",
                notes=notes,
            )

            flash("Reorder request created and logged as PENDING.", "success")
            return redirect(url_for("products"))

        return render_template(
            "request_reorder.html",
            product=product,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/reorder", methods=["GET", "POST"])
    @login_required("REQUEST")
    def reorder():
        if request.method == "POST":
            form = request.form
            user = form.get("user", "web-user")
            ip = request.remote_addr or ""
            extra_cc_raw = form.get("extra_cc", "").strip()

            # We expect fields like order-<vendor_index>-<row_index>
            # and item metadata in hidden inputs.
            # Iterate over form keys to collect orders per vendor.
            orders_by_vendor: dict[str, list[str]] = {}

            for key in form.keys():
                if not key.startswith("order_"):
                    continue
                value = form.get(key)
                if not value:
                    continue
                parts = key.split("_", 3)
                if len(parts) < 4:
                    continue
                _, vendor_id, row_id, field = parts
                vendor_key = form.get(f"vendor_{vendor_id}", "(No Vendor)")

                product_name = form.get(f"item_{vendor_id}_{row_id}_name", "")
                location = form.get(f"item_{vendor_id}_{row_id}_location", "")
                reorder_qty_label = form.get(f"item_{vendor_id}_{row_id}_label", "")
                suggested_amount = form.get(f"item_{vendor_id}_{row_id}_suggested", "")

                # User-entered order quantity
                order_amount = value

                item_desc = f"{product_name} – Order: {order_amount} {reorder_qty_label} (Suggested: {suggested_amount} {reorder_qty_label}, Location: {location})"

                orders_by_vendor.setdefault(vendor_key, []).append(item_desc)

            # Write one log row per vendor
            for vendor, items in orders_by_vendor.items():
                if not items:
                    continue
                items_text = "; ".join(items)
                # Store extra CC emails as a comma-separated string in Notes for now.
                log_reorder(
                    user=user,
                    ip=ip,
                    vendor=vendor,
                    items_description=items_text,
                    status="PENDING",
                    notes=extra_cc_raw,
                )

            if orders_by_vendor:
                flash("Reorder request(s) created and logged as PENDING.", "success")
            else:
                flash("No items selected for reorder.", "warning")

            return redirect(url_for("reorder"))

        grouped = get_low_stock_grouped_by_vendor()
        return render_template(
            "reorder.html",
            grouped=grouped,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/approvals", methods=["GET", "POST"])
    @login_required("APPROVER")
    def approvals():
        if request.method == "POST":
            form = request.form
            action = form.get("action")  # APPROVE or REJECT
            timestamp = form.get("timestamp")
            vendor = form.get("vendor")
            user = form.get("user", "approver")
            ip = request.remote_addr or ""
            delivery_method = (form.get("delivery_method") or "SHIP").strip().upper()
            needed_by = (form.get("needed_by") or "").strip()
            delivery_notes = (form.get("delivery_notes") or "").strip()
            approval_notes = form.get("approval_notes", "").strip()
            internal_notes = form.get("internal_notes", "").strip()

            if action in {"APPROVE", "REJECT"} and timestamp and vendor:
                new_status = "APPROVED" if action == "APPROVE" else "REJECTED"

                # If approving, attempt to send an email before marking as SENT/FAILED.
                if new_status == "APPROVED":
                    # Find the log row to get items description and notes
                    from inventory.services import get_reorder_log

                    log_rows = get_reorder_log()
                    items_desc = ""
                    notes = ""
                    extra_cc: list[str] = []
                    for r in log_rows:
                        if str(r.get("Timestamp")) == str(timestamp) and str(r.get("Vendor")) == str(vendor):
                            items_desc = str(r.get("Items", ""))
                            notes = str(r.get("Notes", ""))
                            break

                    if notes:
                        extra_cc = [e.strip() for e in notes.split(",") if e.strip()]

                    email_ok = send_reorder_email(
                        vendor=vendor,
                        items_description=items_desc,
                        notes=approval_notes,
                        extra_cc=extra_cc,
                        order_meta={
                            "delivery_method": delivery_method,
                            "needed_by": needed_by,
                            "delivery_notes": delivery_notes,
                            "approved_by": user,
                            "timestamp": timestamp,
                        },
                    )
                    new_status = "SENT" if email_ok else "FAILED"

                update_reorder_status(
                    timestamp=timestamp,
                    vendor=vendor,
                    new_status=new_status,
                    approved_by=user,
                    approved_ip=ip,
                    internal_notes=internal_notes,
                )
                flash(f"Reorder {new_status}.", "success")
            else:
                flash("Invalid approval request.", "danger")

            return redirect(url_for("approvals"))

        pending = get_pending_reorders()
        return render_template(
            "approvals.html",
            pending=pending,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/reorder-log")
    @login_required("VIEW")
    def reorder_log():
        log_rows = get_reorder_log()
        return render_template("reorder_log.html", log_rows=log_rows, settings=app.config["APP_SETTINGS"])


    @app.route("/reorder-log/export.xlsx")
    @login_required("VIEW")
    def reorder_log_export_xlsx():
        log_rows = get_reorder_log()
        df = pd.DataFrame(log_rows)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Reorder Log", index=False)
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name="reorder_log.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


    @app.route("/reorder-log/export.pdf")
    @login_required("VIEW")
    def reorder_log_export_pdf():
        try:
            from fpdf import FPDF
        except Exception:
            flash("PDF export requires the fpdf2 package. Install it in the server venv.", "warning")
            return redirect(url_for("reorder_log"))

        log_rows = get_reorder_log()
        columns = [
            "Timestamp",
            "User",
            "Vendor",
            "Items",
            "Status",
            "Notes",
            "Approved By",
        ]

        pdf = FPDF(orientation="L", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=10)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Reorder Log", ln=True)

        pdf.set_font("Helvetica", "", 8)
        col_widths = {
            "Timestamp": 38,
            "User": 18,
            "Vendor": 28,
            "Items": 120,
            "Status": 18,
            "Notes": 45,
            "Approved By": 20,
        }

        pdf.set_font("Helvetica", "B", 8)
        for c in columns:
            pdf.cell(col_widths[c], 6, c, border=1)
        pdf.ln()

        pdf.set_font("Helvetica", "", 8)
        for row in log_rows:
            for c in columns:
                v = str(row.get(c, "") or "")
                v = v.replace("\r", " ").replace("\n", " ")
                if len(v) > 200:
                    v = v[:197] + "..."
                pdf.cell(col_widths[c], 6, v, border=1)
            pdf.ln()

        data = pdf.output(dest="S")
        if isinstance(data, str):
            data = data.encode("latin-1")
        output = io.BytesIO(data)
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name="reorder_log.pdf",
            mimetype="application/pdf",
        )

    @app.route("/stock", methods=["GET", "POST"])
    @login_required("REQUEST")
    def stock_use():
        products_list = get_all_products()

        if request.method == "POST":
            form = request.form
            product_name = form.get("product_name", "").strip()
            amount_raw = form.get("amount", "").strip()
            user = session.get("username", "stock-user")
            location = form.get("location", "").strip()
            job = form.get("job", "").strip()
            truck = form.get("truck", "").strip()
            checkout_notes = form.get("checkout_notes", "").strip()

            display_name = session.get("display_name", "").strip()
            phone = session.get("phone", "").strip()
            email = session.get("email", "").strip()
            license_number = session.get("license_number", "").strip()
            photo_path = session.get("photo_path", "").strip()

            try:
                amount = float(amount_raw)
            except ValueError:
                amount = 0

            if not product_name or amount <= 0:
                flash("Select a product and enter a positive amount used.", "danger")
            else:
                notes_parts: list[str] = ["Checkout"]
                if job:
                    notes_parts.append(f"Job: {job}")
                if truck:
                    notes_parts.append(f"Truck: {truck}")
                if display_name:
                    notes_parts.append(f"Name: {display_name}")
                if phone:
                    notes_parts.append(f"Phone: {phone}")
                if checkout_notes:
                    notes_parts.append(f"Notes: {checkout_notes}")
                notes_text = "; ".join(notes_parts)

                result = adjust_product_quantity(
                    product_name=product_name,
                    delta=-amount,
                    user=user,
                    location=location,
                    notes=notes_text,
                )
                if not result:
                    flash("Unable to record stock use for that product.", "danger")
                else:
                    # Email notification (HTML table with cost)
                    settings = app.config["APP_SETTINGS"]
                    to_email = (settings.get("checkout_email_to") or "").strip()
                    cc_raw = str(settings.get("checkout_email_cc") or "")
                    cc_list = [e.strip() for e in cc_raw.split(",") if e.strip()]

                    product = get_product_by_name(result["product_name"]) or {}
                    unit = str(product.get("Container Unit") or "")
                    cpu_raw = str(product.get("Cost Per Unit") or "").strip()
                    try:
                        unit_cost = float(cpu_raw)
                    except ValueError:
                        unit_cost = 0.0

                    line_total = float(amount) * float(unit_cost)
                    total_cost = line_total

                    subject = f"Checkout - {display_name or user} - {result['product_name']}"

                    text_lines = [
                        f"Technician: {display_name or '(not set)'} ({user})",
                        f"Phone: {phone or '(not set)'}",
                        f"Email: {email or '(not set)'}",
                        f"Truck: {truck or '(not set)'}",
                        f"Job: {job or '(not set)'}",
                        f"Product: {result['product_name']}",
                        f"Location: {result['location']}",
                        f"Quantity: {amount} {unit}",
                        f"Unit Cost: ${unit_cost:,.2f}",
                        f"Line Total: ${line_total:,.2f}",
                        f"Total: ${total_cost:,.2f}",
                        f"Notes: {checkout_notes or '(none)'}",
                    ]
                    text_body = "\n".join(text_lines)

                    def _esc(s: str) -> str:
                        return (
                            str(s)
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace('"', "&quot;")
                            .replace("'", "&#39;")
                        )

                    inline_abs_path = ""
                    if photo_path:
                        inline_abs_path = os.path.join(BASE_DIR, "static", photo_path)
                        if not os.path.exists(inline_abs_path):
                            inline_abs_path = ""

                    photo_cell_html = ""
                    if inline_abs_path:
                        photo_cell_html = """
        <td style=\"width:110px; vertical-align:top; padding-right:12px;\">
          <img src=\"cid:{{INLINE_IMAGE_CID}}\" alt=\"Technician\" style=\"width:96px; height:96px; object-fit:cover; border-radius:6px; border:1px solid #ddd;\" />
        </td>
"""

                    html_body = f"""
<html>
  <body>
    <table cellpadding="0" cellspacing="0" style="border-collapse:collapse; width:100%; max-width:900px; margin-bottom:12px;">
      <tr>
{photo_cell_html}
        <td style="vertical-align:top;">
          <h2 style="margin:0 0 6px 0;">Technician Checkout</h2>
          <div><strong>Technician:</strong> {_esc(display_name or '(not set)')} ({_esc(user)})</div>
          <div><strong>License #:</strong> {_esc(license_number or '(not set)')}</div>
          <div><strong>Phone:</strong> {_esc(phone or '(not set)')}</div>
          <div><strong>Email:</strong> {_esc(email or '(not set)')}</div>
          <div><strong>Truck:</strong> {_esc(truck or '(not set)')}</div>
          <div><strong>Job:</strong> {_esc(job or '(not set)')}</div>
          <div><strong>Location:</strong> {_esc(result['location'])}</div>
        </td>
      </tr>
    </table>

    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%; max-width:900px;">
      <thead>
        <tr>
          <th align="left">Product</th>
          <th align="right">Qty</th>
          <th align="left">Unit</th>
          <th align="right">Unit Cost</th>
          <th align="right">Line Total</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>{_esc(result['product_name'])}</td>
          <td align="right">{amount:g}</td>
          <td>{_esc(unit)}</td>
          <td align="right">${unit_cost:,.2f}</td>
          <td align="right">${line_total:,.2f}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <td colspan="4" align="right"><strong>Total</strong></td>
          <td align="right"><strong>${total_cost:,.2f}</strong></td>
        </tr>
      </tfoot>
    </table>

    <p><strong>Notes:</strong> {_esc(checkout_notes or '(none)')}</p>
  </body>
</html>
"""

                    if to_email:
                        ok = send_html_email(
                            subject,
                            text_body,
                            html_body,
                            [to_email],
                            cc_addresses=cc_list,
                            inline_image_path=inline_abs_path or None,
                        )
                        if ok:
                            flash("Stock use recorded and checkout email sent.", "success")
                        else:
                            flash("Stock use recorded, but checkout email failed.", "warning")
                    else:
                        flash("Stock use recorded (no checkout email configured).", "warning")

            return redirect(url_for("stock_use"))

        # GET – optional search filter
        q = request.args.get("q", "").strip().lower()
        if q:
            def _matches(p: dict) -> bool:
                fields = [
                    str(p.get("Product Name", "")),
                    str(p.get("Category", "")),
                    str(p.get("Distributor", "")),
                    str(p.get("Location", "")),
                ]
                return any(q in value.lower() for value in fields)

            products_list = [p for p in products_list if _matches(p)]

        return render_template(
            "stock_use.html",
            products=products_list,
            query=q,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/vendors")
    @login_required("ADMIN")
    def vendors():
        vendors_list = get_all_vendors()
        return render_template("vendors.html", vendors=vendors_list, settings=app.config["APP_SETTINGS"])

    @app.route("/vendors/new", methods=["GET", "POST"])
    @login_required("ADMIN")
    def vendor_new():
        if request.method == "POST":
            form = request.form
            data = {
                "Vendor Name": form.get("vendor_name", "").strip(),
                "Address": form.get("address", "").strip(),
                "Phone": form.get("phone", "").strip(),
                "Email": form.get("email", "").strip(),
                "CC Emails": form.get("cc_emails", "").strip(),
                "Notes": form.get("notes", "").strip(),
            }
            upsert_vendor(data)
            flash("Vendor saved.", "success")
            return redirect(url_for("vendors"))

        return render_template("vendor_form.html", vendor=None, settings=app.config["APP_SETTINGS"])

    @app.route("/settings", methods=["GET", "POST"])
    @login_required("ADMIN")
    def settings_view():
        if request.method == "POST":
            current = app.config["APP_SETTINGS"].copy()
            form = request.form
            action = form.get("action", "save")
            current["company_name"] = request.form.get("company_name", current.get("company_name", ""))
            current["company_address"] = request.form.get("company_address", current.get("company_address", ""))
            current["company_phone"] = request.form.get("company_phone", current.get("company_phone", ""))
            current["email_subject_prefix"] = request.form.get("email_subject_prefix", current.get("email_subject_prefix", ""))
            current["default_email_cc"] = request.form.get("default_email_cc", current.get("default_email_cc", ""))
            current["email_footer"] = request.form.get("email_footer", current.get("email_footer", ""))
            current["po_footer_pickup"] = request.form.get("po_footer_pickup", current.get("po_footer_pickup", ""))
            current["po_footer_ship"] = request.form.get("po_footer_ship", current.get("po_footer_ship", ""))
            current["stock_use_notify_emails"] = request.form.get("stock_use_notify_emails", current.get("stock_use_notify_emails", ""))
            current["checkout_email_to"] = request.form.get("checkout_email_to", current.get("checkout_email_to", "office@robertspest.com")).strip()
            current["checkout_email_cc"] = request.form.get("checkout_email_cc", current.get("checkout_email_cc", ""))

            current["smtp_provider"] = request.form.get("smtp_provider", current.get("smtp_provider", "bluehost"))
            current["from_email"] = request.form.get("from_email", current.get("from_email", "")).strip()
            current["smtp_host"] = request.form.get("smtp_host", current.get("smtp_host", "")).strip()
            smtp_port_raw = request.form.get("smtp_port", current.get("smtp_port", ""))
            try:
                current["smtp_port"] = int(smtp_port_raw or current.get("smtp_port", 0) or SMTP_PORT)
            except (TypeError, ValueError):
                current["smtp_port"] = SMTP_PORT
            current["smtp_user"] = request.form.get("smtp_user", current.get("smtp_user", "")).strip()
            # Do not strip if empty; keep existing if not provided to avoid wiping by mistake
            new_pass = request.form.get("smtp_pass", "")
            if new_pass:
                current["smtp_pass"] = new_pass
            current["smtp_use_tls"] = bool(request.form.get("smtp_use_tls") or current.get("smtp_use_tls", True))

            # Handle optional logo upload
            logo_file = request.files.get("company_logo")
            if logo_file and logo_file.filename:
                _, ext = os.path.splitext(logo_file.filename)
                ext = ext.lower()
                if ext in {".png", ".jpg", ".jpeg", ".gif"}:
                    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
                    filename = f"company_logo{ext}"
                    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    logo_file.save(save_path)
                    current["company_logo_path"] = f"uploads/{filename}"

            save_settings(current)
            app.config["APP_SETTINGS"] = current

            if action == "autodetect":
                from_email = (current.get("from_email") or "").strip()
                if not from_email or "@" not in from_email:
                    flash("Enter a valid From Email before auto-detecting.", "warning")
                    return redirect(url_for("settings_view"))

                domain = from_email.split("@", 1)[1].lower()

                # Simple domain-based presets for major providers
                if domain in {"gmail.com", "googlemail.com"}:
                    current["smtp_provider"] = "gmail"
                    current["smtp_host"] = "smtp.gmail.com"
                    current["smtp_port"] = 587
                    current["smtp_use_tls"] = True
                    current["smtp_user"] = from_email
                elif domain in {"outlook.com", "hotmail.com", "live.com", "office365.com"}:
                    current["smtp_provider"] = "office365"
                    current["smtp_host"] = "smtp.office365.com"
                    current["smtp_port"] = 587
                    current["smtp_use_tls"] = True
                    current["smtp_user"] = from_email
                # GoDaddy workspace email (secureserver.net) and related
                elif domain in {"secureserver.net", "godaddymail.com"}:
                    current["smtp_provider"] = "godaddy"
                    current["smtp_host"] = "smtp.secureserver.net"
                    current["smtp_port"] = 587
                    current["smtp_use_tls"] = True
                    current["smtp_user"] = from_email
                elif domain in {"yahoo.com", "yahoo.co.uk"}:
                    current["smtp_provider"] = "other"
                    current["smtp_host"] = "smtp.mail.yahoo.com"
                    current["smtp_port"] = 587
                    current["smtp_use_tls"] = True
                    current["smtp_user"] = from_email
                elif domain in {"aol.com"}:
                    current["smtp_provider"] = "other"
                    current["smtp_host"] = "smtp.aol.com"
                    current["smtp_port"] = 587
                    current["smtp_use_tls"] = True
                    current["smtp_user"] = from_email
                else:
                    # Generic guess for hosted domains (cPanel / Bluehost style)
                    # Treat as cPanel-style hosting (Bluehost/HostGator/GoDaddy custom domains, etc.)
                    current["smtp_provider"] = current.get("smtp_provider", "bluehost")
                    current["smtp_host"] = current.get("smtp_host") or f"mail.{domain}"
                    current["smtp_port"] = current.get("smtp_port") or SMTP_PORT
                    current["smtp_use_tls"] = True
                    if not current.get("smtp_user"):
                        current["smtp_user"] = from_email

                save_settings(current)
                app.config["APP_SETTINGS"] = current
                flash("SMTP settings guessed from email domain. Please verify and send a test email.", "info")
                return redirect(url_for("settings_view"))

            if action == "test":
                test_recipient = current.get("from_email") or current.get("default_email_cc", "")
                recipients = [e.strip() for e in str(test_recipient).split(",") if e.strip()]
                if not recipients:
                    flash("Configure From Email or a default CC before sending a test.", "warning")
                else:
                    ok = send_basic_email("Test email from Roberts Inventory Manager", "This is a test message.", recipients)
                    if ok:
                        flash("Test email sent successfully.", "success")
                    else:
                        flash("Failed to send test email. Check SMTP settings.", "danger")
                return redirect(url_for("settings_view"))

            flash("Settings saved.", "success")
            return redirect(url_for("settings_view"))

        settings = app.config["APP_SETTINGS"]
        suggested_host = ""
        if (not settings.get("smtp_host")) and settings.get("from_email"):
            # Very simple preset: assume mail.<domain> for providers like Bluehost
            try:
                domain = settings["from_email"].split("@", 1)[1]
                suggested_host = f"mail.{domain}"
            except Exception:
                suggested_host = ""

        return render_template("settings.html", settings=settings, suggested_smtp_host=suggested_host)

    @app.route("/theme", methods=["GET", "POST"])
    @login_required("VIEW")
    def theme_view():
        if request.method == "POST":
            current = app.config["APP_SETTINGS"].copy()
            form = request.form

            current["theme_bg"] = form.get("theme_bg", current.get("theme_bg", "#f5f5f5"))
            current["theme_text"] = form.get("theme_text", current.get("theme_text", "#111111"))
            current["theme_header_bg"] = form.get("theme_header_bg", current.get("theme_header_bg", "#1f4e79"))
            current["theme_header_text"] = form.get("theme_header_text", current.get("theme_header_text", "#ffffff"))
            current["theme_nav_link"] = form.get("theme_nav_link", current.get("theme_nav_link", "#ffffff"))
            current["theme_nav_link_hover"] = form.get("theme_nav_link_hover", current.get("theme_nav_link_hover", "#ffffff"))
            current["theme_table_header_bg"] = form.get("theme_table_header_bg", current.get("theme_table_header_bg", "#e3edf5"))
            current["theme_table_row_alt"] = form.get("theme_table_row_alt", current.get("theme_table_row_alt", "#fafafa"))
            current["theme_table_row_hover"] = form.get("theme_table_row_hover", current.get("theme_table_row_hover", "#f1f7ff"))
            current["theme_card_bg"] = form.get("theme_card_bg", current.get("theme_card_bg", "#ffffff"))
            current["theme_button_bg"] = form.get("theme_button_bg", current.get("theme_button_bg", "#1f4e79"))
            current["theme_button_hover_bg"] = form.get("theme_button_hover_bg", current.get("theme_button_hover_bg", "#173958"))
            current["theme_button_text"] = form.get("theme_button_text", current.get("theme_button_text", "#ffffff"))
            current["theme_font_family"] = form.get("theme_font_family", current.get("theme_font_family", "Arial, sans-serif"))
            current["theme_google_font_url"] = form.get("theme_google_font_url", current.get("theme_google_font_url", ""))
            current["theme_radius"] = form.get("theme_radius", current.get("theme_radius", "6px"))
            current["theme_border_width"] = form.get("theme_border_width", current.get("theme_border_width", "1px"))
            current["theme_spacing"] = form.get("theme_spacing", current.get("theme_spacing", "1"))
            current["theme_header_height"] = form.get("theme_header_height", current.get("theme_header_height", ""))

            save_settings(current)
            app.config["APP_SETTINGS"] = current
            flash("Theme updated.", "success")
            return redirect(url_for("theme_view"))

        settings = app.config["APP_SETTINGS"]
        return render_template("theme.html", settings=settings)

    @app.route("/units", methods=["GET", "POST"])
    @login_required("ADMIN")
    def manage_units():
        if request.method == "POST":
            column = request.form.get("column", "")
            old_value = request.form.get("old_value", "").strip()
            new_value = request.form.get("new_value", "").strip()

            if not old_value or not new_value:
                flash("Both old and new values are required.", "danger")
            else:
                ok = rename_product_value(column, old_value, new_value)
                if ok:
                    flash("Value updated across products.", "success")
                else:
                    flash("No matching rows found or invalid column.", "warning")

            return redirect(url_for("manage_units"))

        container_units = get_distinct_product_values("Container Unit")
        reorder_labels = get_distinct_product_values("Reorder Quantity")
        distributors = get_distinct_product_values("Distributor")

        return render_template(
            "units.html",
            container_units=container_units,
            reorder_labels=reorder_labels,
            distributors=distributors,
            settings=app.config["APP_SETTINGS"],
        )

    @app.route("/pricing-request", methods=["GET", "POST"])
    @login_required("REQUEST")
    def pricing_request():
        vendors_list = get_all_vendors()
        selected_vendor = request.args.get("vendor") if request.method == "GET" else request.form.get("vendor")

        if request.method == "POST":
            vendor = request.form.get("vendor", "").strip()
            notes = request.form.get("notes", "").strip()
            extra_cc_raw = request.form.get("extra_cc", "").strip()
            extra_cc = [e.strip() for e in extra_cc_raw.split(",") if e.strip()]

            # Collect selected product names from checkboxes
            selected_product_names = request.form.getlist("products")
            products = [
                p
                for p in get_products_for_vendor(vendor)
                if p.get("Product Name") in selected_product_names
            ]

            email_ok = False
            if vendor and products:
                email_ok = send_pricing_request_email(
                    vendor=vendor,
                    products=products,
                    notes=notes,
                    extra_cc=extra_cc,
                )

                # Log this as a special entry in the reorder log
                items_desc = "; ".join([str(p.get("Product Name", "")) for p in products])
                user = request.form.get("user", "pricing-requester")
                ip = request.remote_addr or ""
                log_reorder(
                    user=user,
                    ip=ip,
                    vendor=vendor,
                    items_description=items_desc,
                    status="PRICE_REQUEST",
                    notes=notes,
                )

            if email_ok:
                flash("Pricing request email sent.", "success")
            else:
                flash("Unable to send pricing request email. Check vendor email and settings.", "danger")

            return redirect(url_for("pricing_request", vendor=vendor))

        # GET or initial load
        products_for_vendor = get_products_for_vendor(selected_vendor) if selected_vendor else []

        return render_template(
            "pricing_request.html",
            vendors=vendors_list,
            selected_vendor=selected_vendor,
            products=products_for_vendor,
            settings=app.config["APP_SETTINGS"],
        )

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8000, debug=True)
