# Roberts Inventory Manager

Internal inventory and reorder tool backed by the existing Excel workbook `Central Inventory Log (1).xlsx`.

## Quick start (day-to-day)

1) Open the site in a browser.

- If you are on the same machine as the server: `http://localhost:8000`
- If you are on the LAN: `http://<PI_IP>:8000`
- If you need camera support on laptops/phones: `https://<PI_IP>/` (see HTTPS section below)

2) Log in.

- If you don’t have an account, request one (or ask an ADMIN).

3) Use the top navigation:

- **Dashboard**: overview + reporting widgets
- **Products**: add/edit inventory items and photos
- **Reorder**: create reorder requests
- **Approvals**: approve/reject and send vendor PO emails (APPROVER/ADMIN)
- **Reorder Log / Reorder Reports**: history + exports
- **Settings** (ADMIN): company branding + email setup

## Dev setup (WSL)

```bash
cd "/mnt/c/Users/charl/OneDrive/Desktop/Inventory Program/inventory_web"

# activate venv created in parent folder
source ../.venv/bin/activate

# ensure dependencies are installed
pip install flask pandas openpyxl

# point to the real workbook on the Windows side
export INVENTORY_XLSX_PATH="/mnt/c/Users/charl/OneDrive/Desktop/Inventory Program/Central Inventory Log (1).xlsx"

# SMTP/email (optional but needed for sending vendor orders)
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT=587
export SMTP_USER="your_email@example.com"
export SMTP_PASS="your_app_password"  # do NOT commit
export FROM_EMAIL="your_email@example.com"  # optional; defaults to SMTP_USER

# run the app
export FLASK_APP=app:create_app
export FLASK_ENV=development
flask run --host 0.0.0.0 --port 8000
```

## SQLite migration path (recommended source of truth)

SQLite is the recommended backend for **durability** and **multi-user safety**. Excel remains supported for import/export.

### 1) Set your DB path

Choose where the SQLite file should live (example):

```bash
export INVENTORY_DB_PATH="/home/pi/RobertsInventory/inventory.db"
```

### 2) Initialize + import Excel → SQLite

In the app UI (ADMIN):

1) Go to **Database** (`/db`)
2) Click **Initialize SQLite DB**
3) Click **Import Excel → SQLite**

This import is **lossless**: it stores the full Excel row JSON plus indexed columns for lookups.

### 3) Flip the app into SQLite mode

Set:

```bash
export INVENTORY_BACKEND=sqlite
```

Restart the server after changing env vars.

### 4) Verify

- Check **Products**, **Vendors**, **Reorder Log**, **Approvals**, and **Stock Use**.
- Confirm **Database** page shows non-zero row counts.

### 5) Export snapshots (optional)

In the app UI (ADMIN) on the **Database** page:

- **Download Workbook (Excel)**: exports Products/Vendors/Reorder Log as a multi-sheet `.xlsx`
- **SQLite → Excel (Round-trip)**: exports `Master Inventory`, `Vendors`, `Reorder Log`, and `All Transactions` as a workbook
- **Download Snapshot (PDF)**: quick printable snapshot (requires `fpdf2`)

### Safety notes

- You can always temporarily switch back by setting `INVENTORY_BACKEND=excel` and restarting.
- Importing Excel → SQLite does not modify the Excel workbook.

Open `http://127.0.0.1:8000` in a browser.

## Raspberry Pi notes (prod)

On the Pi, clone or copy the same `inventory_web` folder and install Python 3 + dependencies.

Recommended env vars (example):

```bash
export INVENTORY_XLSX_PATH="/home/charlie/xdrive/Central Inventory Log (1).xlsx"

# Recommended when using SQLite backend
export INVENTORY_DB_PATH="/home/pi/RobertsInventory/inventory.db"
export INVENTORY_BACKEND=sqlite

export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT=587
export SMTP_USER="robertspestcontrolllc@gmail.com"
export SMTP_PASS="<app_password>"
export FROM_EMAIL="robertspestcontrolllc@gmail.com"

export FLASK_APP=app:create_app
export FLASK_ENV=production

# example run (adjust to your process manager / systemd)
flask run --host 0.0.0.0 --port 8000
```

## HTTPS (required for camera on laptops/phones)

Modern browsers only allow webcam access (`getUserMedia`) on **secure contexts**:

- `https://...` (recommended)
- `http://localhost...`

If you access the app over the LAN using **plain HTTP** like `http://<PI_IP>:8000`, the browser will typically block camera access on Windows/Android/iOS.

### Recommended: Caddy reverse proxy with internal TLS (Raspberry Pi OS)

This keeps Flask running locally and serves the site over HTTPS on port 443.

1) Install Caddy:

```bash
sudo apt update
sudo apt install -y caddy
```

2) Run Flask locally (recommended):

- Run your app on `127.0.0.1:8000` (or whichever port you use).

3) Configure Caddy:

```bash
sudo nano /etc/caddy/Caddyfile
```

Use this (replace `127.0.0.1:8000` if needed):

```caddy
https://<PI_IP> {
  tls internal
  reverse_proxy 127.0.0.1:8000
}
```

Then restart Caddy:

```bash
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```

Open the app at:

- `https://<PI_IP>/`

### Device trust note

`tls internal` uses a private (self-signed) CA. Devices may show a certificate warning until trusted.

- For quick testing you can proceed through the warning.
- For a clean “no warning” experience, install/trust the Caddy local CA on each device.

## User guide (step-by-step)

### Roles

- **VIEW**: can view dashboards, products, logs, reports
- **REQUEST**: can create reorder requests and stock use entries
- **APPROVER**: can approve/reject reorders and send vendor emails
- **ADMIN**: can manage vendors/users/settings and everything above

### Products (add/edit)

1) Go to **Products**.
2) To add a new item:

- Click **Add Product**
- Fill out:
  - Product Name
  - Quantity on Hand
  - Container Unit (what the product is stored in)
  - Reorder Threshold (when it becomes “low stock”)
  - Reorder Amount (how much to request)
  - Distributor (vendor)
  - Optional: Cost Per Unit, Location, EPA numbers

3) Add a product photo (optional):

- **Upload**: choose a file using the file picker
- **Phone/tablet**: tap the file picker and it should offer the camera
- **Laptop/desktop**: click **Use Camera**
  - If you see a message about HTTPS, open the site via `https://<PI_IP>/` (camera will not work over plain HTTP on most browsers)

4) Click **Save**.

### Reorder workflow (request → approval → vendor email)

This is the standard business workflow.

#### Step 1 — Create a reorder request (REQUEST/APPROVER/ADMIN)

1) Go to **Reorder**.
2) Review low-stock items grouped by vendor.
3) Enter/confirm reorder quantities.
4) Submit the request.

This creates a **PENDING** row in the reorder log.

#### Step 2 — Approve and send vendor PO email (APPROVER/ADMIN)

1) Go to **Approvals**.
2) Fill out the top form fields (these apply to the approval you click):

- **Delivery Method**: SHIP or PICKUP
- **PO Number**: your internal PO number for tracking
- **Pickup By** (optional): who will pick it up
- **Needed By** (optional): requested date
- **Delivery / Pickup Notes** (optional)
- **Notes to include in vendor email** (optional)
- **Internal notes** (stored for reporting, not emailed)

3) Click **Approve** on the row.

Result:

- The system attempts to email the vendor.
- A formal **PDF Purchase Order** is generated and attached (if `fpdf2` is installed on the server).
- The row status updates to:
  - **SENT** if email succeeded
  - **FAILED** if email failed

If you click **Reject**, it marks the row **REJECTED**.

### Purchase Orders (PDF)

The PDF Purchase Order includes:

- Your company name/address/phone/logo (from **Settings**)
- Vendor name
- Delivery method
- PO Number
- Pickup By (if provided)
- Needed By + delivery notes (if provided)
- Line-item table
- Instructions footer (pickup vs ship)

### Reorder Log (history)

Go to **Reorder Log** to view all reorder events.

You can download:

- **Excel** export
- **PDF** export

### Reorder Reports (filters + exports)

Go to **Reorder Reports** for business reporting.

1) Set filters (date range, vendor, status, PO #, delivery method, pickup-by, approved-by).
2) Review KPIs (totals, counts by status).
3) Export what you’re looking at:

- **Export Excel** (filtered)
- **Export PDF** (filtered)

### Settings (ADMIN)

Go to **Settings** to configure:

- Company branding for PDFs/emails (name/address/phone/logo)
- Email SMTP settings (required to send vendor emails)
- Purchase Order footer instructions (pickup vs ship)

### Vendors (ADMIN)

Go to **Vendors** to store:

- Vendor email
- CC emails
- Vendor notes

### Troubleshooting

- **Camera says not supported**:
  - Use `https://<PI_IP>/` (required for laptop/phone webcam support)
  - Try Chrome/Edge/Firefox
  - Verify the device has granted camera permissions

- **Vendor email shows FAILED**:
  - Verify **Settings → SMTP**
  - Use the Settings “test email” action
  - Confirm vendor email/CC fields in **Vendors**

- **PDF not attached**:
  - Install `fpdf2` on the server venv

### Backups (recommended)

- If using Excel backend: back up the Excel file regularly.
- If using SQLite backend: back up the SQLite database file regularly.

At minimum, copy the data file(s) to an external drive weekly.

## Excel expectations

Workbook sheets used:

- `Master Inventory` (or first sheet containing "Inventory") as the product master.
- `All Transactions` (optional; currently just preserved).
- `Vendors` with columns: `Vendor Name`, `Email`, `CC Emails`, `Notes`.
- `Reorder Log` with columns: `Timestamp`, `User`, `IP`, `Vendor`, `Items`, `Status`, `Notes`, `Approved Timestamp`, `Approved By`, `Approved IP`.

The app only reads from the workbook during normal page loads. Writes happen when:

- Adding a product from `/products/new`.
- Creating reorder requests from `/reorder`.
- Approving/rejecting reorder requests in `/approvals`.

Avoid keeping the workbook open in Excel while writing from the app, as Excel may lock the file.
