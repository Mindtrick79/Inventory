# Roberts Inventory Manager

Internal inventory and reorder tool backed by the existing Excel workbook `Central Inventory Log (1).xlsx`.

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

Open `http://127.0.0.1:8000` in a browser.

## Raspberry Pi notes (prod)

On the Pi, clone or copy the same `inventory_web` folder and install Python 3 + dependencies.

Recommended env vars (example):

```bash
export INVENTORY_XLSX_PATH="/home/charlie/xdrive/Central Inventory Log (1).xlsx"

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
