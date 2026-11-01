"""Google Maps research, no API key, Playwright only.

Method, same house rules as the mapscout approach the user pointed to:
- Search maps.google.com for "niche in locality", scroll the results feed,
  open each place panel, read name, category, rating, address, phone,
  website, hours. No Google Places API, no key, no billing.
- Slow by design: jittered pauses between actions. Speed is what gets
  scrapers blocked.
- Never solve a CAPTCHA. On a Google block page, stop cleanly, save
  progress, exit code 3. The workflow treats exit 3 as OK and the next
  scheduled run resumes from the Sheet Progress tab.
- Dedupe by the Maps feature id in the place URL, else by
  name|postcode|phone. A business found by five searches enters once.
- State lives in the Sheet, not on the runner: Directory tab holds one
  row per business, Progress tab holds one row per finished query.
  Counts only in logs, never lead names or websites.

Usage in GitHub Actions:
  python scripts/maps_scrape.py --max-queries 90 --delay 4
"""

import argparse
import csv
import datetime
import os
import random
import re
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]
PROGRESS_HEADERS = ["key", "locality", "niche", "query", "found", "added", "scraped_at"]
BLOCK_MARKERS = ("unusual traffic", "not a robot", "recaptcha", "our systems have detected", "/sorry/")
FEATURE_RE = re.compile(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
ICON_RE = re.compile(r"[\ue000-\uf8ff\u200b\ufeff]")


class BlockedError(Exception):
    pass


def clean(text):
    text = ICON_RE.sub("", text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(lines).strip()


def place_key(url, name, postcode, phone):
    m = FEATURE_RE.search(url or "")
    if m:
        return m.group(1).lower()
    base = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    digits = re.sub(r"[^0-9]+", "", phone or "")
    return f"{base}|{(postcode or '').upper()}|{digits}"


def ensure_tab(service, sheet_id, title, headers):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = [s.get("properties", {}).get("title") for s in meta.get("sheets", [])]
    if title not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
    got = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{title}!A1:Z1"
    ).execute()
    if not got.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{title}!A1",
            valueInputOption="USER_ENTERED", body={"values": [headers]},
        ).execute()


def load_queries():
    loc_path = os.path.join(BASE, "config", "maps_localities.csv")
    niche_path = os.path.join(BASE, "config", "maps_niches.txt")
    niches = []
    with open(niche_path, encoding="utf-8") as f:
        niches = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    queries = []
    with open(loc_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            locality = (row.get("locality") or "").strip()
            region = (row.get("region") or "").strip()
            if not locality:
                continue
            for niche in niches:
                queries.append({
                    "key": f"{niche}|{locality}",
                    "query": f"{niche} in {locality}",
                    "locality": locality,
                    "niche": niche,
                    "region": region,
                })
    return queries


def scrape_query(page, query, delay, max_results):
    from playwright.sync_api import TimeoutError as PWTimeout

    def pause(mult=1.0):
        time.sleep(max(0.2, delay * mult * random.uniform(0.7, 1.4)))

    def check_blocked():
        url = (page.url or "").lower()
        if "/sorry/" in url:
            raise BlockedError(f"Google block page: {url}")
        try:
            body = (page.inner_text("body", timeout=3000) or "").lower()
        except Exception:
            return
        if any(m in body for m in BLOCK_MARKERS):
            raise BlockedError("Google unusual-traffic / CAPTCHA page")

    def text(selector):
        try:
            loc = page.locator(selector)
            if loc.count():
                return clean(loc.first.inner_text(timeout=2500) or "")
        except Exception:
            pass
        return ""

    def attr(selector, name):
        try:
            loc = page.locator(selector)
            if loc.count():
                return (loc.first.get_attribute(name) or "").strip()
        except Exception:
            pass
        return ""

    from urllib.parse import quote_plus
    try:
        page.goto(f"https://www.google.com/maps/search/{quote_plus(query)}",
                  wait_until="domcontentloaded")
    except PWTimeout:
        pause(2)
        page.goto(f"https://www.google.com/maps/search/{quote_plus(query)}",
                  wait_until="domcontentloaded")
    pause()
    try:
        for label in ("Reject all", "Accept all"):
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                pause()
                break
    except Exception:
        pass
    check_blocked()
    try:
        page.wait_for_selector('div[role="feed"], h1', timeout=15000)
    except PWTimeout:
        pass
    check_blocked()

    feed = page.locator('div[role="feed"]')
    found = {}
    if not feed.count():
        if page.locator("h1").count():
            found[page.url] = ""
        return list(found.items())[:max_results]
    last_count, stale = -1, 0
    while len(found) < max_results and stale < 3:
        cards = page.locator('div[role="feed"] a[href*="/maps/place/"]')
        for i in range(cards.count()):
            try:
                href = cards.nth(i).get_attribute("href") or ""
                if href and href not in found:
                    try:
                        card = cards.nth(i).locator("xpath=ancestor::div[1]")
                        found[href] = card.inner_text(timeout=1500)
                    except Exception:
                        found[href] = ""
            except Exception:
                continue
            if len(found) >= max_results:
                break
        if len(found) == last_count:
            stale += 1
        else:
            stale = 0
        last_count = len(found)
        try:
            feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        except Exception:
            break
        pause(0.7)

    records = []
    for url, card_text in list(found.items())[:max_results]:
        pause(0.6)
        try:
            page.goto(url, wait_until="domcontentloaded")
            pause()
            check_blocked()
            try:
                page.wait_for_selector("h1", timeout=12000)
            except PWTimeout:
                pass
            name = text("h1")
            category = text('button[jsaction*="category"]') or text('span[jsaction*="category"]')
            rating_row = text('div[role="img"][aria-label*="stars"]')
            rating = ""
            m = re.search(r"([0-9.]+)\s*stars?", rating_row)
            if m:
                rating = m.group(1)
            aria = attr('div[role="img"][aria-label*="reviews"]', "aria-label")
            reviews = ""
            m = re.search(r"([\d,]+)\s*reviews?", aria)
            if m:
                reviews = m.group(1).replace(",", "")
            address = text('button[data-item-id="address"]')
            phone = text('button[data-item-id^="phone:tel:"]')
            website = attr('a[data-item-id="authority"]', "href") or text('a[data-item-id="authority"]')
            hours = re.sub(r"\s+", " ", clean(
                attr('button[data-item-id*="hours"]', "aria-label")
                or attr('div[data-item-id*="hours"]', "aria-label")
                or attr('button[aria-label*="Closes"]', "aria-label")
                or attr('button[aria-label*="Opens"]', "aria-label"))).strip()
            if card_text and not name:
                lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]
                if lines:
                    name = lines[0]
            postcode = ""
            m = POSTCODE_RE.search((address or "").upper())
            if m:
                postcode = m.group(1)
            maps_url = page.url.split("?")[0] if "?" in page.url else page.url
            records.append({
                "name": name, "category": category, "rating": rating,
                "reviews": reviews, "address": address, "postcode": postcode,
                "town": "", "phone": phone, "website": website, "hours": hours,
                "maps_url": maps_url,
                "place_key": place_key(url, name, postcode, phone),
            })
        except BlockedError:
            raise
        except Exception:
            continue
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-queries", type=int, default=90)
    ap.add_argument("--max-results", type=int, default=40)
    ap.add_argument("--delay", type=float, default=4.0)
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import sheets
        service, sheet_id = sheets._client()
        ensure_tab(service, sheet_id, "Directory", DIRECTORY_HEADERS)
        ensure_tab(service, sheet_id, "Progress", PROGRESS_HEADERS)
        dir_rows = sheets.read_values("Directory!A:P")
        prog_rows = sheets.read_values("Progress!A:G")
    except Exception as e:
        print(f"maps tick: sheet unavailable, no scraping: {type(e).__name__}")
        return
    known = set()
    for row in sheets.rows_to_dicts(dir_rows):
        if row.get("place_key"):
            known.add(row["place_key"].strip().lower())
    done = set()
    for row in sheets.rows_to_dicts(prog_rows):
        if row.get("key"):
            done.add(row["key"])
    queries = [q for q in load_queries() if q["key"] not in done]
    if args.max_queries:
        queries = queries[:args.max_queries]
    print(f"maps tick: total_queries={len(load_queries())} done={len(done)} todo={len(queries)}")
    if not queries:
        return
    from playwright.sync_api import sync_playwright
    summary = {"queries_done": 0, "found": 0, "added": 0, "blocked": False}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-GB",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        page.set_default_timeout(30000)
        for q in queries:
            try:
                records = scrape_query(page, q["query"], args.delay, args.max_results)
            except BlockedError as e:
                print(f"maps tick: BLOCKED during query, stopping cleanly: {type(e).__name__}")
                summary["blocked"] = True
                break
            except Exception as e:
                print(f"maps tick: query error, continuing: {type(e).__name__}")
                continue
            new_rows = []
            for rec in records:
                key = (rec["place_key"] or "").strip().lower()
                if not key or key in known:
                    continue
                known.add(key)
                new_rows.append([
                    rec["name"], rec["category"], rec["rating"], rec["reviews"],
                    rec["address"], rec["postcode"], rec["town"], rec["phone"],
                    rec["website"], rec["hours"], rec["maps_url"], rec["place_key"],
                    q["query"], q["locality"], q["niche"], stamp,
                ])
            if new_rows:
                sheets.append_values("Directory!A:P", new_rows)
            sheets.append_values("Progress!A:G", [[
                q["key"], q["locality"], q["niche"], q["query"],
                len(records), len(new_rows), stamp,
            ]])
            summary["queries_done"] += 1
            summary["found"] += len(records)
            summary["added"] += len(new_rows)
            print(f"maps tick: query done, found={len(records)} new={len(new_rows)}")
        browser.close()
    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "maps",
            f"queries={summary['queries_done']} found={summary['found']} added={summary['added']} blocked={summary['blocked']}",
            summary["added"]]])
    except Exception:
        pass
    print(f"maps tick done: {summary}")
    if summary["blocked"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
