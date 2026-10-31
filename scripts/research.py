"""Autonomous research run: koi manual seed list nahi chahiye.

Har run khud ye karta hai:
1. config/queries.txt se US + EU agency queries uthata hai (rotate hoti hain)
2. DuckDuckGo HTML search se nayi agency websites nikalta hai (koi API key nahi)
3. Har website ka homepage + /contact page kholta hai, form URL nikalta hai
4. CAPTCHA signal ho to skip, personalization line site ki apni description se
   banata hai (invent nahi karta), warna lead queue me nahi jata
5. data/queue.csv me append karta hai, dedupe data/sent.csv + queue se hota hai

Input:  config/queries.txt (ek query per line, region prefix ke sath)
        data/seeds.csv (optional extra, manual nahi lazmi)
Output: data/queue.csv
Rules: US + EU only, Pakistan/India nahi. Personal emails scrape nahi.
"""
import argparse, csv, os, re, urllib.parse, urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTCHA_WORDS = ["recaptcha", "g-recaptcha", "turnstile", "hcaptcha", "captcha"]
SKIP_DOMAINS = ["facebook.com", "instagram.com", "linkedin.com", "twitter.com",
                "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com"]


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
    """Queries file se search karke nayi sites nikalna. Search engine ke
    result page se sirf domain nikalte hain, koi personal data nahi."""
    queries_path = os.path.join(BASE, "config", "queries.txt")
    sites = []
    if not os.path.exists(queries_path):
        return sites
    with open(queries_path, encoding="utf-8") as f:
        queries = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    # Har run me sirf pehli 3 queries, taake quota aur time dono bachen.
    # Agli run me file ka order rotate karna workflow ka kaam nahi, is liye
    # queries list ko run date se offset karte hain (simple, deterministic).
    import datetime
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
                    sites.append({"company": "", "website": site, "region": region,
                                  "agency_size": "", "services": "", "source": "search:" + query,
                                  "personalization_seed": ""})
                if len(sites) >= max_sites:
                    return sites
        except Exception as e:
            print(f"search failed: {query} {e}")
    return sites


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=10)
    args = ap.parse_args()
    # Tick har 10 minute par hai, lekin research har 10 minute par nahi.
    # Sirf ghante ke pehle tick (minute 0-9) par research hoti hai, warna
    # search engine ko har 10 minute par hit karna rate limit khayega.
    import datetime as _dt
    if _dt.datetime.now(_dt.timezone.utc).minute >= 10:
        print("research tick: not due this tick, research runs hourly")
        return
    queue_path = os.path.join(BASE, "data", "queue.csv")
    sent_path = os.path.join(BASE, "data", "sent.csv")
    seen = set()
    for p in (queue_path, sent_path):
        if os.path.exists(p):
            with open(p, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    seen.add((row.get("company", "") + row.get("website", "")).lower())
                    seen.add(row.get("website", "").lower())
    seeds = discover_from_queries(args.max_new * 3)
    seeds_path = os.path.join(BASE, "data", "seeds.csv")
    if os.path.exists(seeds_path):
        with open(seeds_path, newline="", encoding="utf-8") as f:
            seeds += [r for r in csv.DictReader(f) if r.get("website")]
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
                print(f"SKIP captcha: {website}")
                continue
            line = (s.get("personalization_seed") or "").strip() or desc
            if not line:
                print(f"SKIP (no personalization from site): {website}")
                continue
            company = (s.get("company") or "").strip() or title.split("|")[0].split("-")[0].strip() or website
            rows_out.append({
                "company": company, "website": website,
                "region": s.get("region", ""),
                "contact_form_url": contact, "contact_email": "",
                "channel": "form_only", "email_or_form": "form",
                "personalization_line": line, "agency_size": s.get("agency_size", ""),
                "services": s.get("services", ""), "source": s.get("source", ""),
                "status": "researched", "approved": "0", "created_at": "", "notes": "",
            })
        except Exception as e:
            print(f"SKIP (fetch failed): {website} {e}")
    if rows_out:
        exists = os.path.exists(queue_path) and os.path.getsize(queue_path) > 0
        with open(queue_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            if not exists:
                w.writeheader()
            w.writerows(rows_out)
    print(f"discovered_sites={len(seeds)} researched_new={len(rows_out)}")


if __name__ == "__main__":
    main()
