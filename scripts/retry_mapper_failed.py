"""One-off retry pilot: revive Queue rows that failed only because the
Groq field mapper was dead (Cloudflare HTTP 403 error 1010 blocked
Python-urllib's default User-Agent until the 2026-11-07 fix).

Only rows that are safe to retry are flipped:
  - status == 'failed'
  - notes == 'not_enough_fields_filled' exactly (the form was never
    submitted, so there is no duplicate risk)
  - dedupe_key not already in the RetryLog tab (each row flips once)

Rows that failed for honest reasons (captcha, no form, no honest option,
client budget, bot block) or with an unknown submit outcome
(no_success_response) are never touched.

The flip only changes status back to 'queued' and notes to
'retry_mapper_failed'; the normal send workflow then attempts them like
any other queued row, with its usual guards (already-sent websites are
skipped, screenshots are kept, Log counts only). Writes one Log row
(counts only). No lead data in repo commits or public logs.

Write discipline: the Sheets API rate-limits rapid writes, so flips
pause briefly and RetryLog appends are batched (flushed every 50).
A startup reconcile logs any row the Sheet already shows as flipped
but RetryLog missed, so the once-only rule holds even if a write
failed midway on an earlier run.
"""
import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets

QUEUE_COLS = ["company", "website", "region", "contact_form_url",
    "personalization_line", "agency_size", "services", "source",
    "status", "approved", "created_at", "last_send_at", "send_count",
    "figma_link", "notes", "dedupe_key"]


def _flush_log(rows):
    if not rows:
        return
    try:
        sheets.append_values("RetryLog!A:C", rows)
    except Exception as e:
        print(f"retry-mapper-failed: retrylog append failed: {type(e).__name__}")
    rows.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-flip", type=int, default=300)
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        sheets.ensure_tab("RetryLog", ["dedupe_key", "flipped_at_utc", "notes_before"])
        queue = sheets.rows_to_dicts(sheets.read_values("Queue!A:P"))
        retry_rows = sheets.rows_to_dicts(sheets.read_values("RetryLog!A:C"))
    except Exception as e:
        print(f"retry-mapper-failed: sheet unavailable: {type(e).__name__}")
        return
    already = {r.get("dedupe_key", "").strip() for r in retry_rows
               if r.get("dedupe_key", "").strip()}
    # Reconcile: rows the Sheet already shows as flipped but RetryLog
    # missed get logged now, so the once-only rule still holds.
    repaired = 0
    pending_log = []
    for row in queue:
        if (row.get("notes", "") or "").strip() == "retry_mapper_failed":
            key = (row.get("dedupe_key", "") or "").strip()
            if key and key not in already:
                pending_log.append([key, now.isoformat(), "not_enough_fields_filled"])
                already.add(key)
                repaired += 1
    flipped = 0
    skipped_done = 0
    for row in queue:
        if flipped >= args.max_flip:
            break
        if (row.get("status", "") or "").strip().lower() != "failed":
            continue
        if (row.get("notes", "") or "").strip() != "not_enough_fields_filled":
            continue
        key = (row.get("dedupe_key", "") or "").strip()
        if not key or key in already:
            skipped_done += 1
            continue
        values = [row.get(c, "") for c in QUEUE_COLS]
        values[8] = "queued"
        values[14] = "retry_mapper_failed"
        try:
            sheets.update_row("Queue", row["_row"], values)
        except Exception as e:
            print(f"retry-mapper-failed: sheet update failed row {row['_row']}: "
                  f"{type(e).__name__}")
            continue
        pending_log.append([key, now.isoformat(), "not_enough_fields_filled"])
        already.add(key)
        flipped += 1
        if len(pending_log) >= 50:
            _flush_log(pending_log)
        time.sleep(0.5)
    _flush_log(pending_log)
    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "retry-mapper-failed",
            f"flipped={flipped} repaired={repaired} skipped_already={skipped_done} "
            f"max_flip={args.max_flip}", flipped]])
    except Exception:
        pass
    print(f"retry-mapper-failed: flipped={flipped} repaired={repaired} "
          f"skipped_already={skipped_done}")


if __name__ == "__main__":
    main()
