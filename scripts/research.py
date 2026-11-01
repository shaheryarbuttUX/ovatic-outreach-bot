"""Autonomous research run: koi manual seed list nahi chahiye.

Har hourly run ye karta hai:
1. config/queries.txt se US + EU agency queries uthata hai (rotate hoti hain)
2. DuckDuckGo HTML search se nayi agency websites nikalta hai (koi API key nahi)
3. Har website ka homepage + /contact page kholta hai, form URL nikalta hai
4. CAPTCHA signal ho to skip, personalization line site ki apni description se
   banata hai (invent nahi karta), warna lead queue me nahi jata
5. Google Sheet Queue tab me append karta hai, dedupe Queue + Sent se hota hai

Rules: US + EU only, Pakistan/India nahi. Personal emails scrape nahi.
Data Sheet me hi rehta hai, repo me lead data kabhi nahi jata, logs me
sirf counts aate hain, lead names ya websites nahi.
"""
import argparse
import datetime
import os
import re
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTCHA_WORDS = ["recaptcha", "g-recaptcha", "turnstile", "hcaptcha", "captcha"]
SKIP_DOMAINS = ["facebook.com", "instagram.com", "linkedin.com", "twitter.com",
                "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com"]
WEBSITE_FIGMA = "https://www.figma.com/design/9LfCw6faDaXvFQfWSFkM2C/Website-Portfolio--Client-Sharing-?node-id=159-12849&t=u2nBTJ3IEOLMJnQh-1"
DASHBOARD_FIGMA = "https://www.figma.com/design/4OzS8KcSeOLgXVbGMjCVZ1/Dashboard-Portfolio--Client-Sharing-?node-id=1-15548&t=YUB9pL13pk5InsaD-1"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160]


def find_contact(html, base):
    for m in re.finditer(r'href="([^"]*(contact|get-in-touch|lets-talk)[^"]*)"', html, re.I):
        link = m.group(1)
        if link.startswith("http"):
            return link
        return base.rstrip("/") + "/" + link.lstrip("/")
    return base.rstrip("/") + "/contact"


def site_title_and_desc(html):
    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if m:
        title = clean(m.group(1))
    desc = ""
    m = re.search(r'name="description" content="([^"]+)"', html, re.I)
    if m:
        desc = clean(m.group(1))
    return title, desc


def discover_from_queries(max_sites):
    queries_path = os.path.join(BASE, "config", "queries.txt")
    sites = []
    if not os.path.exists(queries_path):
        return sites
    with open(queries_path, encoding="utf-8") as f:
        queries = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    offset = datetime.date.today().toordinal() % max(1, len(queries))
    queries = queries[offset:] + queries[:offset]
    for line in queries[:3]:
        if "|" not in line:
            continue
        region, query = [p.strip() for p in line.split("|", 1)]
        try:
            url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
            html = fetch(url)
            for m in re.finditer(r'result__a[^>]*href="([^"]+)"', html):
                link = m.group(1)
                if "duckduckgo.com" in link:
                    continue
                parsed = urllib.parse.urlparse(link)
                domain = parsed.netloc.lower().replace("www.", "")
                if not domain or any(d in domain for d in SKIP_DOMAINS):
                    continue
                site = f"https://{domain}"
                if site not in [s["website"] for s in sites]:
                    sites.append({"website": site, "region": region, "source": "search:" + query})
                if len(sites) >= max_sites:
                    return sites
        except Exception:
            continue
    return sites


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=50)
    args = ap.parse_args()
    if datetime.datetime.now(datetime.timezone.utc).minute >= 10:
        print("research tick: not due this tick, research runs hourly")
        return
    try:
        import sheets
        queue_rows = sheets.read_values("Queue!A:P")
        sent_rows = sheets.read_values("Sent!A:H")
    except Exception as e:
        print(f"research tick: sheet unavailable, no research: {type(e).__name__}")
        return
    queue = sheets.rows_to_dicts(queue_rows)
    sent = sheets.rows_to_dicts(sent_rows)
    seen = set()
    for row in queue + sent:
        seen.add((row.get("company", "").strip().lower(), row.get("website", "").strip().lower()))
        seen.add(row.get("website", "").strip().lower())
        seen.add(row.get("dedupe_key", "").strip().lower())
    seeds = discover_from_queries(args.max_new * 3)
    rows_out = []
    for s in seeds:
        if len(rows_out) >= args.max_new:
            break
        website = (s.get("website") or "").strip()
        if not website or website.lower() in seen:
            continue
        try:
            html = fetch(website)
            title, desc = site_title_and_desc(html)
            contact = find_contact(html, website)
            contact_html = fetch(contact) if contact != website else html
            if any(w in contact_html.lower() for w in CAPTCHA_WORDS):
                continue
            line = desc
            if not line:
                continue
            company = title.split("|")[0].split("-")[0].strip() or website
            if (company.lower(), website.lower()) in seen:
                continue
            lower = (title + " " + desc).lower()
            figma = DASHBOARD_FIGMA if any(w in lower for w in ["saas", "dashboard", "product"]) else WEBSITE_FIGMA
            rows_out.append([
                company, website, s.get("region", ""), contact, line, "",
                "", s.get("source", ""), "queued", "yes",
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "", "0", figma, "", website.lower(),
            ])
        except Exception:
            continue
    if rows_out:
        try:
            sheets.append_values("Queue!A:P", rows_out)
        except Exception as e:
            print(f"research tick: sheet append failed: {type(e).__name__}")
            return
    try:
        sheets.append_values("Log!A:D", [[
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "research", f"discovered={len(seeds)} queued={len(rows_out)}", len(rows_out),
        ]])
    except Exception:
        pass
    print(f"research tick done: discovered={len(seeds)} queued={len(rows_out)}")


if __name__ == "__main__":
    main()
