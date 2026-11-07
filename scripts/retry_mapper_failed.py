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
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets

QUEUE_COLS = ["company", "website", "region", "contact_form_url",
    "personalization_line", "agency_size", "services", "source",
    "status", "approved", "created_at", "last_send_at", "send_count",
    "figma_link", "notes", "dedupe_key"]


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
            sheets.append_values("RetryLog!A:C",
                                 [[key, now.isoformat(), "not_enough_fields_filled"]])
        except Exception as e:
            print(f"retry-mapper-failed: sheet update failed row {row['_row']}: "
                  f"{type(e).__name__}")
            continue
        already.add(key)
        flipped += 1
    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "retry-mapper-failed",
            f"flipped={flipped} skipped_already={skipped_done} max_flip={args.max_flip}",
            flipped]])
    except Exception:
        pass
    print(f"retry-mapper-failed: flipped={flipped} skipped_already={skipped_done}")


if __name__ == "__main__":
    main()
