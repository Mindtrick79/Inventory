import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Excel workbook path
# By default, use a file named "Central Inventory Log (1).xlsx" next to this config.
# You can override this with the INVENTORY_XLSX_PATH environment variable
# (recommended for Raspberry Pi / Google Drive sync locations).
DEFAULT_XLSX_PATH = os.path.join(BASE_DIR, "Central Inventory Log (1).xlsx")
XLSX_PATH = os.environ.get("INVENTORY_XLSX_PATH", DEFAULT_XLSX_PATH)
LOCAL_XLSX = XLSX_PATH

# SQLite database path (recommended for durability and multi-user safety)
DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "RobertsInventory", "inventory.db")
DB_PATH = os.environ.get("INVENTORY_DB_PATH", DEFAULT_DB_PATH)

MASTER_SHEET = "Master Inventory"
TX_SHEET = "All Transactions"
VENDOR_SHEET = "Vendors"
REORDER_LOG_SHEET = "Reorder Log"

# Email settings – use environment variables when present
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "robertspestcontrolllc@gmail.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "CHANGE_ME")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change_this_secret_key")
