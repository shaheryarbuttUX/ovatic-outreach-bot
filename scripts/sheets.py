"""Google Sheet adapter for the outreach bot.

Data lives ONLY in the Sheet, never in repo commits or public logs.
Secrets come from GitHub Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON.
This module prints counts only, never lead names, websites or messages.

Required packages in the workflow (pip install step):
google-api-python-client, google-auth
"""
import json
import os


def _client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sheet_id = os.environ.get("SHEET_ID", "").strip()
    key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sheet_id or not key_json:
        raise RuntimeError("SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON secret missing")
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service, sheet_id


def read_values(tab_range):
    service, sheet_id = _client()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab_range
    ).execute()
    return result.get("values", [])


def append_values(tab_range, rows):
    if not rows:
        return
    service, sheet_id = _client()
    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=tab_range,
        valueInputOption="USER_ENTERED", body=body
    ).execute()


def update_row(tab, row_number, values):
    """Update one row, 1-based, starting at column A."""
    service, sheet_id = _client()
    end_col = chr(ord("A") + len(values) - 1)
    body = {"values": [values]}
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A{row_number}:{end_col}{row_number}",
        valueInputOption="USER_ENTERED", body=body
    ).execute()


def ensure_tab(title, headers):
    """Create tab with headers if missing. No-op if it already exists."""
    service, sheet_id = _client()
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = [s.get("properties", {}).get("title") for s in meta.get("sheets", [])]
    if title in existing:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{title}!A1",
        valueInputOption="USER_ENTERED", body={"values": [headers]}
    ).execute()


def rows_to_dicts(rows):
    if not rows:
        return []
    header = rows[0]
    out = []
    for i, row in enumerate(rows[1:], start=2):
        d = {"_row": i}
        for j, col in enumerate(header):
            d[col] = row[j] if j < len(row) else ""
        out.append(d)
    return out
