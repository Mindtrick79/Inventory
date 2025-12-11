import os
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
    with pd.ExcelWriter(LOCAL_XLSX, engine="openpyxl") as writer:
        master_df.to_excel(writer, sheet_name=MASTER_SHEET, index=False)
        if not tx_df.empty:
            tx_df.to_excel(writer, sheet_name=TX_SHEET, index=False)
        vendors_df.to_excel(writer, sheet_name=VENDOR_SHEET, index=False)
        reorder_log_df.to_excel(writer, sheet_name=REORDER_LOG_SHEET, index=False)
