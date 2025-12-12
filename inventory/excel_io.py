import os
import time
import shutil
from typing import Tuple

import pandas as pd
from openpyxl import load_workbook, Workbook

from config import (
    LOCAL_XLSX,
    MASTER_SHEET,
    TX_SHEET,
    VENDOR_SHEET,
    REORDER_LOG_SHEET,
)


def _lock_path_for(xlsx_path: str) -> str:
    return f"{xlsx_path}.lock"


def _acquire_lock(lock_path: str, timeout_seconds: int = 20) -> int:
    """Create an exclusive lock file.

    This avoids concurrent writers corrupting the workbook.
    """
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd
        except FileExistsError:
            if time.time() - start > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}")
            time.sleep(0.1)


def _release_lock(fd: int, lock_path: str) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(lock_path)
    except Exception:
        pass


def _ensure_backup_dir(xlsx_path: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(xlsx_path))
    backup_dir = os.path.join(base_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def _backup_workbook(xlsx_path: str) -> None:
    if not os.path.exists(xlsx_path):
        return
    backup_dir = _ensure_backup_dir(xlsx_path)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(xlsx_path)
    backup_path = os.path.join(backup_dir, f"{base}.{ts}.bak")
    try:
        shutil.copy2(xlsx_path, backup_path)
    except Exception:
        # Backups are best-effort; do not block the write.
        pass


VENDOR_COLUMNS = [
    "Vendor Name",
    "Address",
    "Phone",
    "Email",
    "CC Emails",
    "Notes",
]

REORDER_LOG_COLUMNS = [
    "Timestamp",
    "User",
    "IP",
    "Vendor",
    "Items",
    "Status",
    "Notes",
    "Approved Timestamp",
    "Approved By",
    "Approved IP",
]
def load_inventory_workbook() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load all key sheets as DataFrames.

    Returns master_df, tx_df, vendors_df, reorder_log_df.
    Missing optional sheets are created with headers and empty rows.
    """
    xls = pd.ExcelFile(LOCAL_XLSX, engine="openpyxl")

    sheet_names = xls.sheet_names

    # Choose the master inventory sheet intelligently:
    # 1) Exact MASTER_SHEET match if present
    # 2) First sheet whose name contains "inventory" (case-insensitive)
    # 3) Fallback to the first sheet
    master_sheet_name = None
    if MASTER_SHEET in sheet_names:
        master_sheet_name = MASTER_SHEET
    else:
        for name in sheet_names:
            if "inventory" in name.lower():
                master_sheet_name = name
                break
    if master_sheet_name is None and sheet_names:
        master_sheet_name = sheet_names[0]

    if master_sheet_name is not None:
        master_df = pd.read_excel(xls, master_sheet_name)
    else:
        master_df = pd.DataFrame()

    tx_df = pd.read_excel(xls, TX_SHEET) if TX_SHEET in sheet_names else pd.DataFrame()

    if VENDOR_SHEET in sheet_names:
        vendors_df = pd.read_excel(xls, VENDOR_SHEET)
    else:
        vendors_df = pd.DataFrame(columns=VENDOR_COLUMNS)

    if REORDER_LOG_SHEET in sheet_names:
        reorder_log_df = pd.read_excel(xls, REORDER_LOG_SHEET)
    else:
        reorder_log_df = pd.DataFrame(columns=REORDER_LOG_COLUMNS)

    return master_df, tx_df, vendors_df, reorder_log_df


def save_inventory_workbook(
    master_df: pd.DataFrame,
    tx_df: pd.DataFrame,
    vendors_df: pd.DataFrame,
    reorder_log_df: pd.DataFrame,
) -> None:
    """Persist all sheets back to the Excel file.

    Hook point: extend later to log transactions into All Transactions.
    """
    lock_path = _lock_path_for(LOCAL_XLSX)
    fd = _acquire_lock(lock_path)
    try:
        _backup_workbook(LOCAL_XLSX)

        tmp_path = f"{LOCAL_XLSX}.tmp"
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            master_df.to_excel(writer, sheet_name=MASTER_SHEET, index=False)
            if not tx_df.empty:
                tx_df.to_excel(writer, sheet_name=TX_SHEET, index=False)
            vendors_df.to_excel(writer, sheet_name=VENDOR_SHEET, index=False)
            reorder_log_df.to_excel(writer, sheet_name=REORDER_LOG_SHEET, index=False)

        # Atomic replace on POSIX; on Windows, this still replaces reliably via os.replace.
        os.replace(tmp_path, LOCAL_XLSX)
    finally:
        _release_lock(fd, lock_path)
