"""Directory -> Queue bridge, the cmm-auto step for this pilot.

Reads the Maps Directory tab, and for each business that:
- has a website,
- was never pulled before (Pulled tab, place_key),
- whose domain was never queued or sent before,
fetches its homepage and contact page, checks for a real form,
skips CAPTCHA pages, builds a personalization line from the site's
own title / meta description / H1 (never invented), and appends the
lead to Queue with status queued, approved yes.

Skipped for a permanent reason (CAPTCHA on the form page, no form,
404) is marked pulled so it is never re-fetched. A transient fetch
failure is NOT marked pulled, so a later run retries it.
Domains whose homepage fetch fails on two earlier ticks are marked
pulled (fetch_errors_repeated) via the FetchErrors tab, so dead
sites stop being re-fetched on every tick (2026-11-05).
Counts only in logs, never lead names or websites.
"""

import argparse
import datetime
import hashlib
import os
import re
import urllib.parse
import urllib.request

CAPTCHA_WORDS = ["recaptcha", "g-recaptcha", "turnstile", "hcaptcha", "captcha"]
SKIP_DOMAINS = ["facebook.com", "instagram.com", "linkedin.com", "twitter.com",
                "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com"]
WEBSITE_FIGMA = "https://www.figma.com/design/9LfCw6faDaXvFQfWSFkM2C/Website-Portfolio--Client-Sharing-?node-id=159-12849&t=u2nBTJ3IEOLMJnQh-1"
DASHBOARD_FIGMA = "https://www.figma.com/design/4OzS8KcSeOLgXVbGMjCVZ1/Dashboard-Portfolio--Client-Sharing-?node-id=1-15548&t=YUB9pL13pk5InsaD-1"
PULLED_HEADERS = ["place_key", "pulled_at", "outcome"]
FETCH_ERROR_HEADERS = ["domain", "error_at", "outcome"]


def fetch(url):
    # Full browser UA, NOT bare "Mozilla/5.0": Cloudflare/WAFs return
    # 403/406 or drop the connection for bare script UAs, which buried
    # ~8,000 live agency sites as fetch_errors (Nov 2026). Same UA as
    # the Groq mapper fix.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:160]


def site_text(html):
    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if m:
        title = clean(m.group(1))
    desc = ""
    m = re.search(r'name="description" content="([^"]+)"', html, re.I)
    if m:
        desc = clean(m.group(1))
    h1 = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    if m:
        h1 = clean(m.group(1))
    return title, desc, h1


def has_real_contact_form(low):
    """A real contact form, not a search box or newsletter signup.

    A form block must carry an email field AND a message field
    (textarea, or an input named for a message). Pages whose only form
    is a newsletter signup, a search box, or a button that opens a
    modal are not queued: the send tick cannot fill them, and queueing
    them only burns attempts and drags the delivered ratio down.
    German/Dutch field names are included, because most of the 2026-11-02
    failures were DE/NL forms queued on an English-only check.
    """
    for block in re.findall(r"<form.*?</form>", low, re.I | re.S):
        if "newsletter" in block and "<textarea" not in block:
            continue
        has_email = ("type=\"email\"" in block or "type='email'" in block
                     or "e-mail" in block or "email" in block)
        has_message = ("<textarea" in block or "message" in block
                       or "nachricht" in block or "bericht" in block
                       or "comment" in block)
        if has_email and has_message:
            return True
    return False


def find_contact(html, base):
    for m in re.finditer(r'href="([^"]*(contact|kontakt|contactformulier|neem-contact|get-in-touch|lets-talk)[^"]*)"', html, re.I):
        link = m.group(1)
        if link.startswith("http"):
            return link
        return base.rstrip("/") + "/" + link.lstrip("/")
    return base.rstrip("/") + "/contact"


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=100)
    # Parallel shards (2026-11-04): partitioned by DOMAIN hash (md5,
    # deterministic across processes), not by row number, so the same
    # domain can never land in two shards and be queued twice. A row
    # for another shard is skipped WITHOUT being marked pulled.
    # The chain step in bridge.yml dispatches the next run when this
    # run queued > 0 (file bridge_queued.txt); the schedule stays
    # as backstop.
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        import sheets
        service, sheet_id = sheets._client()
        # Ensure Pulled tab exists, same pattern as maps script.
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        titles = [s.get("properties", {}).get("title") for s in meta.get("sheets", [])]
        if "Pulled" not in titles:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": "Pulled"}}}]},
            ).execute()
        got = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="Pulled!A1:C1").execute()
        if not got.get("values"):
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id, range="Pulled!A1",
                valueInputOption="USER_ENTERED", body={"values": [PULLED_HEADERS]}).execute()
        directory = sheets.rows_to_dicts(sheets.read_values("Directory!A:P"))
        queue = sheets.rows_to_dicts(sheets.read_values("Queue!A:P"))
        sent = sheets.rows_to_dicts(sheets.read_values("Sent!A:H"))
        pulled = sheets.rows_to_dicts(sheets.read_values("Pulled!A:C"))
        sheets.ensure_tab("FetchErrors", FETCH_ERROR_HEADERS)
        fetch_errors = sheets.rows_to_dicts(sheets.read_values("FetchErrors!A:C"))
    except Exception as e:
        print(f"bridge tick: sheet unavailable: {type(e).__name__}")
        return
    pulled_keys = {r.get("place_key", "").strip().lower() for r in pulled}
    error_counts = {}
    for r in fetch_errors:
        d = (r.get("domain", "") or "").strip().lower()
        if d:
            error_counts[d] = error_counts.get(d, 0) + 1
    seen_domains = set()
    for row in queue + sent:
        d = domain_of(row.get("website", ""))
        if d:
            seen_domains.add(d)
        w = (row.get("website", "") or "").strip().lower()
        if w:
            seen_domains.add(w)
    queued = 0
    skipped = 0
    errors = 0
    pulled_rows = []
    queue_rows = []
    fetch_error_rows = []
    # Incremental flush (2026-11-04): bridge runs kept timing out
    # at 30 minutes and, because rows were only appended at the
    # end, a timeout lost the whole run. Flushing every 20 queued
    # rows, and every 100 pulled rows, means a timeout still keeps
    # the work done so far, including skipped rows that would
    # otherwise be rescanned next run.
    def _flush():
        nonlocal queue_rows, pulled_rows, fetch_error_rows
        if queue_rows:
            try:
                sheets.append_values("Queue!A:P", queue_rows)
            except Exception:
                pass
            queue_rows = []
        if pulled_rows:
            try:
                sheets.append_values("Pulled!A:C", pulled_rows)
            except Exception:
                pass
            pulled_rows = []
        if fetch_error_rows:
            try:
                sheets.append_values("FetchErrors!A:C", fetch_error_rows)
            except Exception:
                pass
            fetch_error_rows = []
    for row in directory:
        if queued >= args.max_new:
            break
        if len(pulled_rows) >= 100 or len(fetch_error_rows) >= 100:
            _flush()
        place_key = (row.get("place_key", "") or "").strip().lower()
        website = (row.get("website", "") or "").strip()
        if not place_key or place_key in pulled_keys or not website:
            continue
        domain = domain_of(website)
        if not domain or any(d in domain for d in SKIP_DOMAINS) or domain in seen_domains:
            pulled_rows.append([place_key, now.isoformat(), "domain_seen_or_skipped"])
            pulled_keys.add(place_key)
            skipped += 1
            continue
        if args.shards > 1 and int(hashlib.md5(domain.encode()).hexdigest(), 16) % args.shards != args.shard:
            continue
        # Dead-row cleanup (2026-11-05): a domain whose homepage
        # fetch failed on two earlier ticks is pulled without
        # fetching again, so dead sites stop eating every tick.
        if error_counts.get(domain, 0) >= 2:
            pulled_rows.append([place_key, now.isoformat(), "fetch_errors_repeated"])
            pulled_keys.add(place_key)
            skipped += 1
            continue
        base = f"https://{domain}"
        try:
            home = fetch(base)
        except Exception:
            errors += 1
            fetch_error_rows.append([domain, now.isoformat(), "fetch_error"])
            continue
        title, desc, h1 = site_text(home)
        personalization = h1 or title or desc
        if not personalization:
            pulled_rows.append([place_key, now.isoformat(), "no_personalization"])
            pulled_keys.add(place_key)
            skipped += 1
            continue
        form_url = find_contact(home, base)
        try:
            form_html = fetch(form_url)
        except Exception:
            # Contact page missing: try the homepage itself as form page.
            form_url = base
            form_html = home
        low = form_html.lower()
        if any(w in low for w in CAPTCHA_WORDS):
            pulled_rows.append([place_key, now.isoformat(), "captcha"])
            pulled_keys.add(place_key)
            skipped += 1
            continue
        if not has_real_contact_form(low):
            pulled_rows.append([place_key, now.isoformat(), "no_contact_form"])
            pulled_keys.add(place_key)
            skipped += 1
            continue
        text_for_figma = (title + " " + desc + " " + (row.get("category", "") or "")).lower()
        figma = DASHBOARD_FIGMA if any(w in text_for_figma for w in ["saas", "dashboard", "product design", "app design"]) else WEBSITE_FIGMA
        region = "US"
        locality = row.get("locality", "") or ""
        # Region is carried in the Directory query/locality; keep US/EU
        # split simple: UK/IE/NL/DE/SE/DK/NO/FI/CH/AT localities are EU.
        eu_markers = ["london", "manchester", "birmingham", "leeds", "bristol",
                      "edinburgh", "glasgow", "dublin", "amsterdam", "rotterdam",
                      "berlin", "munich", "hamburg", "copenhagen", "stockholm",
                      "oslo", "helsinki", "zurich", "vienna"]
        if any(m in locality.lower() for m in eu_markers):
            region = "EU"
        queue_rows.append([
            row.get("name", ""), base, region, form_url, personalization,
            "", row.get("category", ""), "maps:directory", "queued", "yes",
            now.isoformat(), "", "0", figma, "", place_key,
        ])
        pulled_rows.append([place_key, now.isoformat(), "queued"])
        pulled_keys.add(place_key)
        seen_domains.add(domain)
        queued += 1
        if len(queue_rows) >= 20:
            _flush()
    _flush()
    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "bridge",
            f"shard={args.shard}/{args.shards} queued={queued} skipped={skipped} errors={errors}", queued]])
    except Exception:
        pass
    # Chaining file (2026-11-04): the workflow's chain step (shard 0
    # only, so one dispatch per run, never one per shard) reads this
    # and dispatches the next bridge run only when this run queued
    # > 0. A missing file means no chain; the schedule stays backstop.
    try:
        with open("bridge_queued.txt", "w") as fh:
            fh.write(str(queued))
    except Exception:
        pass
    print(f"bridge tick done: shard={args.shard}/{args.shards} queued={queued} skipped={skipped} errors={errors}")


if __name__ == "__main__":
    main()
