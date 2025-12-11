from flask import Flask, render_template, request, redirect, url_for, flash, session
from config import SECRET_KEY, BASE_DIR, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL
import os
import json
import hashlib
from functools import wraps
from datetime import timedelta
import smtplib
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
)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY

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
                existing = {"username": username, "password_hash": "", "role": role}
                users.append(existing)

            existing["role"] = role

            if password:
                existing["password_hash"] = _hash_password(username, password)

            _save_users(users)
            flash("User saved.", "success")
            return redirect(url_for("manage_users"))

        # Do not expose password hashes to the template
        display_users = [
            {"username": u.get("username", ""), "role": u.get("role", "VIEW")}
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
                # Respect remember-me checkbox: make session persistent if checked.
                remember = request.form.get("remember_me")
                session.permanent = bool(remember)
                flash("Logged in successfully.", "success")
                next_url = request.args.get("next") or url_for("index")
                return redirect(next_url)
            flash("Invalid username or password.", "danger")
        return render_template("login.html", settings=app.config["APP_SETTINGS"])

    @app.route("/logout")
    def logout():
        session.pop("username", None)
        session.pop("role", None)
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

        return render_template("products.html", products=products_list, query=q)

    @app.route("/products/new", methods=["GET", "POST"])
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
            }

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
        )

    @app.route("/products/<product_name>/edit", methods=["GET", "POST"])
    @login_required("REQUEST")
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
            }

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
        return render_template("reorder.html", grouped=grouped)

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
                        notes="",
                        extra_cc=extra_cc,
                    )
                    new_status = "SENT" if email_ok else "FAILED"

                update_reorder_status(
                    timestamp=timestamp,
                    vendor=vendor,
                    new_status=new_status,
                    approved_by=user,
                    approved_ip=ip,
                )
                flash(f"Reorder {new_status}.", "success")
            else:
                flash("Invalid approval request.", "danger")

            return redirect(url_for("approvals"))

        pending = get_pending_reorders()
        return render_template("approvals.html", pending=pending)

    @app.route("/reorder-log")
    @login_required("VIEW")
    def reorder_log():
        log_rows = get_reorder_log()
        return render_template("reorder_log.html", log_rows=log_rows, settings=app.config["APP_SETTINGS"])

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

            try:
                amount = float(amount_raw)
            except ValueError:
                amount = 0

            if not product_name or amount <= 0:
                flash("Select a product and enter a positive amount used.", "danger")
            else:
                result = adjust_product_quantity(
                    product_name=product_name,
                    delta=-amount,
                    user=user,
                    location=location,
                    notes="Stock use",
                )
                if not result:
                    flash("Unable to record stock use for that product.", "danger")
                else:
                    # Email notification
                    settings = app.config["APP_SETTINGS"]
                    notify_raw = settings.get("stock_use_notify_emails", "")
                    notify_list = [e.strip() for e in str(notify_raw).split(",") if e.strip()]
                    if notify_list:
                        subject = f"Stock Use - {result['product_name']}"
                        body_lines = [
                            f"User: {user}",
                            f"Product: {result['product_name']}",
                            f"Location: {result['location']}",
                            f"Amount Used: {amount}",
                            f"Old Quantity: {result['old_quantity']}",
                            f"New Quantity: {result['new_quantity']}",
                        ]
                        body = "\n".join(body_lines)
                        msg = f"Subject: {subject}\nFrom: {FROM_EMAIL}\nTo: {', '.join(notify_list)}\n\n{body}"

                        try:
                            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                                server.starttls()
                                if SMTP_USER and SMTP_PASS and SMTP_PASS != "CHANGE_ME":
                                    server.login(SMTP_USER, SMTP_PASS)
                                server.sendmail(FROM_EMAIL, notify_list, msg)
                        except Exception:
                            # Non-fatal, we still adjusted quantity and logged tx
                            flash("Stock use recorded, but email notification failed.", "warning")
                        else:
                            flash("Stock use recorded and notification sent.", "success")
                    else:
                        flash("Stock use recorded.", "success")

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
            current["company_name"] = request.form.get("company_name", current.get("company_name", ""))
            current["email_subject_prefix"] = request.form.get("email_subject_prefix", current.get("email_subject_prefix", ""))
            current["default_email_cc"] = request.form.get("default_email_cc", current.get("default_email_cc", ""))
            current["email_footer"] = request.form.get("email_footer", current.get("email_footer", ""))
            current["stock_use_notify_emails"] = request.form.get("stock_use_notify_emails", current.get("stock_use_notify_emails", ""))

            save_settings(current)
            app.config["APP_SETTINGS"] = current
            flash("Settings saved.", "success")
            return redirect(url_for("settings_view"))

        return render_template("settings.html", settings=app.config["APP_SETTINGS"])

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
