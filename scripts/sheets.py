"""Google Sheet adapter for the outreach bot.

Data lives ONLY in the Sheet, never in repo commits or public logs.
Secrets come from GitHub Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON.
This module prints counts only, never lead names, websites or messages.

Required packages in the workflow (pip install step):
google-api-python-client, google-auth

Write discipline: several workflows (maps shards, bridge, send) write
through the same service account, and the Sheets API allows only 60
write requests/minute/user. Bursts used to crash scripts with an
unhandled HttpError 429 (maps runs #57/#58, Nov 2026). Every API call
here retries 429/5xx with exponential backoff instead of crashing, so
a burst becomes a pause, not a failed run.
"""
import json
import os
import time


def _run(request, what):
    """Execute a Sheets API request, retrying quota/transient errors.

    Retries HTTP 429 (quota) and 500/502/503 up to 3 times with
    10s/20s/40s backoff. A 429 means the request was rejected, not
    executed, so a retry cannot duplicate it; a 5xx retry can in
    theory duplicate an append, but downstream code filters by key
    (Directory/Progress) or counts rows (Sent/Log), so the risk is
    a duplicate log line, not a duplicate form submission.
    """
    delay = 10
    for attempt in range(4):
        try:
            return request.execute()
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status in (429, 500, 502, 503) and attempt < 3:
                print(f"sheets: {what} HTTP {status}, retry in {delay}s "
                      f"(attempt {attempt + 1}/4)")
                time.sleep(delay)
                delay *= 2
                continue
            raise


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
    result = _run(service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab_range
    ), f"read {tab_range}")
    return result.get("values", [])


def append_values(tab_range, rows):
    if not rows:
        return
    service, sheet_id = _client()
    body = {"values": rows}
    # INSERT_ROWS (not the API default OVERWRITE): several workflows/shards
    # append concurrently (maps, bridge, extractors). With OVERWRITE two
    # appends landing in the same instant can overwrite each other -- that
    # is how LeadEngine run #2's 2 appended rows vanished on 2026-11-13.
    _run(service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=tab_range,
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body=body
    ), f"append {tab_range}")


def update_row(tab, row_number, values):
    """Update one row, 1-based, starting at column A."""
    service, sheet_id = _client()
    end_col = chr(ord("A") + len(values) - 1)
    body = {"values": [values]}
    _run(service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A{row_number}:{end_col}{row_number}",
        valueInputOption="USER_ENTERED", body=body
    ), f"update {tab}!A{row_number}")


def ensure_tab(title, headers):
    """Create tab with headers if missing. No-op if it already exists."""
    service, sheet_id = _client()
    meta = _run(service.spreadsheets().get(spreadsheetId=sheet_id), "get sheet meta")
    existing = [s.get("properties", {}).get("title") for s in meta.get("sheets", [])]
    if title in existing:
        return
    _run(service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    ), f"addSheet {title}")
    _run(service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{title}!A1",
        valueInputOption="USER_ENTERED", body={"values": [headers]}
    ), f"ensure headers {title}")


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
