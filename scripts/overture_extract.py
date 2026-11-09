#!/usr/bin/env python3
"""Monthly Overture Maps fuel extraction for the outreach bot.

Pulls agency leads from the Overture Maps places theme (free, open,
monthly releases) and appends the new ones to the Sheet Directory tab,
where bridge.py picks them up unchanged.

Why Overture: a 2026-11-10 test (Chicago + Austin, 100 sampled rows)
found 90 genuinely new domains vs our Sheet and ~25-35 queueable per
100, vs ~10% net new queueable from Bing Maps top results. The pool
is far larger (4,633 matching rows in just 2 metros).

Design (measured 2026-11-10, release 2026-10-21.0):
- The whole places theme is only 16 Parquet files / 11.9 GB. The
  US+EU-overlapping subset is 11 files / 8.26 GB, so a per-shard
  download of ~2 files (~1.5 GB) fits a GitHub Actions runner easily.
  Files are selected at runtime from the STAC collections.parquet
  bbox data, then sharded by file index (sorted by id, shard = i % N),
  so each file is downloaded exactly once across shards.
- Files are downloaded with curl first and queried locally with
  DuckDB. Direct httpfs S3 reads FAIL in this VM (egress proxy /
  MITM CA issues); curl-download is the proven path. Do not rely on
  httpfs for the data reads. The default --work-dir is a
  repo-relative directory; do not point it at /tmp in this VM
  (/tmp is a 512 MB tmpfs and a single place file is ~800 MB).
- Rows are filtered by country (US + EU-27 + GB + CH + NO + IS) with
  a bbox fallback for rows with no address, and Pakistan/India are
  explicitly excluded (standing rule: never target Pakistan/India).
- Dedupe is against the live Sheet Directory + Queue websites, using
  the same domain normalisation as bridge.py, so a domain already
  known or already contacted is never re-appended.
- Rows use the Directory format bridge.py expects: place_key
  "overture:<domain>", category "design agency", niche mapped from
  the Overture taxonomy, query "overture import".

Usage (monthly workflow, 6 shards):
    python scripts/overture_extract.py --shard 0 --shards 6
    python scripts/overture_extract.py --release 2026-10-21.0 --max-new 5000
    python scripts/overture_extract.py --dry-run --parquet-dir ~/overture \
        --bbox "-88.0,-87.5,41.6,42.05" --bbox "-98.0,-97.5,30.1,30.6" --sample 50

Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (same as the other
scripts). --dry-run reads the Sheet (for dedupe) but writes nothing.
Dry-run prints the first 5 rows it would append, so run it manually
only; the scheduled run prints counts only, never lead data (public
repo logs are public).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402

STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
S3_BASE = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

# US + EU-27 + GB + CH + NO + IS (ISO country codes from addresses[].country)
IN_SCOPE_COUNTRIES = {
    "US",
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
    "GB", "CH", "NO", "IS",
}
EXCLUDED_COUNTRIES = {"PK", "IN"}  # standing rule: never target Pakistan/India

# Bounding boxes used for FILE selection (which Parquet files overlap
# the target regions) and for the no-address row fallback.
# US incl. Alaska + Hawaii; Europe incl. UK + Scandinavia.
US_BBOX = (-170.0, -66.0, 18.0, 72.0)
EU_BBOX = (-11.0, 32.0, 36.0, 71.0)

# Taxonomy.primary values that matched agencies in the 2026-11-10 test.
AGENCY_TAXONOMY = (
    "marketing_agency", "advertising_agency", "web_designer",
    "graphic_designer", "b2b_marketing_consultant",
    "internet_marketing_service", "broadcasting_media_production",
    "b2b_advertising_and_marketing_service", "public_relations",
    "media_agency", "product_design", "social_media_agency",
    "social_media_company", "b2b_direct_mail_advertising",
    "b2b_newspaper_advertising", "b2b_outdoor_advertising",
)

NAME_PATTERNS = (
    "%web design%", "%webdesign%", "%digital marketing%",
    "%marketing agency%", "%advertising agency%", "%design agency%",
    "%creative agency%", "%web development%", "%digital agency%",
    "%media agency%",
)

# Overture taxonomy.primary -> our 22 Directory niches (see
# scripts/niche_priority.json). Unknown / name-fallback rows get
# "web design agency".
NICHE_MAP = {
    "marketing_agency": "marketing agency",
    "advertising_agency": "advertising agency",
    "web_designer": "web design agency",
    "graphic_designer": "graphic design agency",
    "b2b_marketing_consultant": "marketing agency",
    "internet_marketing_service": "marketing agency",
    "broadcasting_media_production": "video production agency",
    "b2b_advertising_and_marketing_service": "advertising agency",
    "public_relations": "marketing agency",
    "media_agency": "digital agency",
    "product_design": "product design agency",
    "social_media_agency": "social media agency",
    "social_media_company": "social media agency",
    "b2b_direct_mail_advertising": "advertising agency",
    "b2b_newspaper_advertising": "advertising agency",
    "b2b_outdoor_advertising": "advertising agency",
}

# Same skip list as bridge.py: a "website" that is really a social /
# directory profile is not a business website.
SKIP_DOMAINS = [
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com",
]

APPEND_CHUNK = 500  # rows per Sheet append call (60 writes/min limit)


def domain_of(url):
    """Host, lowercased, no www. Same as bridge.py, except Overture
    often stores a bare domain (no scheme), which urlparse would read
    as a path, so a scheme is added first when missing."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    try:
        return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def full_url(url):
    """Website as a full URL (bridge fetches https://<domain>, and a
    schemeless value would give it an empty domain)."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    return u


def resolve_release(override):
    if override:
        return override
    with urllib.request.urlopen(STAC_CATALOG, timeout=30) as resp:
        catalog = json.loads(resp.read().decode("utf-8"))
    latest = catalog.get("latest", "")
    if not latest:
        raise RuntimeError("STAC catalog has no 'latest' release")
    return latest


def collections_url(release):
    return f"https://stac.overturemaps.org/{release}/collections.parquet"


def run_curl(url, dest):
    """Download url to dest with curl. Returns True on success.

    A failed or partial download is removed so a later call retries
    instead of reusing a truncated file.
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True
    r = subprocess.run(
        ["curl", "-sL", "--max-time", "1800", "-o", dest, url],
        capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0
    if not ok and os.path.exists(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
    return ok


def _bbox_intersects(b, box):
    xmin, xmax, ymin, ymax = box
    return (b["xmin"] <= xmax and b["xmax"] >= xmin
            and b["ymin"] <= ymax and b["ymax"] >= ymin)


def select_files(release, shard, shards, work_dir):
    """Download collections.parquet, return this shard's place files.

    Files whose bbox intersects US or EU, sorted by id, sharded by
    file index so each file is downloaded exactly once across shards.
    Returns [(file_id, https_url, size_bytes)].
    """
    import duckdb

    os.makedirs(work_dir, exist_ok=True)
    coll_path = os.path.join(work_dir, "collections.parquet")
    if not run_curl(collections_url(release), coll_path):
        raise RuntimeError(f"could not download {collections_url(release)}")
    con = duckdb.connect()
    rows = con.execute(
        "SELECT id, assets.aws.href AS href, assets.aws.\"file:size\" AS sz, bbox "
        "FROM read_parquet(?) WHERE collection = 'place'",
        [coll_path]).fetchall()
    con.close()
    selected = []
    for file_id, href, size, bbox in rows:
        if _bbox_intersects(bbox, US_BBOX) or _bbox_intersects(bbox, EU_BBOX):
            selected.append((str(file_id), href, int(size or 0)))
    selected.sort(key=lambda r: r[0])
    return selected[shard::shards]


def download_files(files, work_dir):
    """curl-download the shard's files. Returns local paths.

    A file is re-downloaded unless the local copy's size matches the
    size from collections.parquet exactly.
    """
    os.makedirs(work_dir, exist_ok=True)
    local = []
    for file_id, href, size in files:
        name = href.rsplit("/", 1)[-1]
        dest = os.path.join(work_dir, name)
        if (size and os.path.exists(dest)
                and os.path.getsize(dest) == size):
            print(f"overture: file {file_id} already downloaded, reusing", flush=True)
        else:
            if os.path.exists(dest):
                os.remove(dest)
            if not run_curl(href, dest):
                raise RuntimeError(f"could not download {href}")
            if size and os.path.getsize(dest) != size:
                raise RuntimeError(
                    f"download size mismatch for {href}: "
                    f"got {os.path.getsize(dest)}, expected {size}")
        local.append(dest)
    return local


def _country_sql():
    codes = ", ".join(f"'{c}'" for c in sorted(IN_SCOPE_COUNTRIES))
    us = US_BBOX
    eu = EU_BBOX
    return (
        f"(addresses[1].country IN ({codes}) OR "
        f"(addresses[1].country IS NULL AND ("
        f"(bbox.xmin >= {us[0]} AND bbox.xmax <= {us[1]} "
        f"AND bbox.ymin >= {us[2]} AND bbox.ymax <= {us[3]}) OR "
        f"(bbox.xmin >= {eu[0]} AND bbox.xmax <= {eu[1]} "
        f"AND bbox.ymin >= {eu[2]} AND bbox.ymax <= {eu[3]}))))"
    )


def extract_rows(parquet_paths, bboxes=None, sample=0):
    """Query local Parquet files for agency rows. Returns dicts."""
    import duckdb

    tax_list = ", ".join(f"'{t}'" for t in AGENCY_TAXONOMY)
    name_like = " OR ".join(
        f"lower(names.primary) LIKE '{p}'" for p in NAME_PATTERNS)
    agency_filter = f"(taxonomy.primary IN ({tax_list}) OR ({name_like}))"
    files_sql = ", ".join(f"'{p}'" for p in parquet_paths)
    base_where = (
        f"websites IS NOT NULL AND len(websites) > 0 "
        f"AND {agency_filter} AND {_country_sql()}"
    )
    select = (
        "SELECT names.primary AS name, taxonomy.primary AS cat, "
        "websites[1] AS website, addresses[1].country AS country, "
        "addresses[1].locality AS locality, addresses[1].region AS region, "
        "addresses[1].freeform AS freeform "
        f"FROM read_parquet([{files_sql}])"
    )
    con = duckdb.connect()
    out = []
    if bboxes:
        for box in bboxes:
            xmin, xmax, ymin, ymax = box
            cond = (
                f"bbox.xmin >= {xmin} AND bbox.xmax <= {xmax} "
                f"AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}"
            )
            q = f"{select} WHERE {base_where} AND {cond} ORDER BY id"
            if sample:
                q += f" LIMIT {sample}"
            for r in con.execute(q).fetchall():
                out.append(_row_to_dict(r))
    else:
        q = f"{select} WHERE {base_where} ORDER BY id"
        for r in con.execute(q).fetchall():
            out.append(_row_to_dict(r))
    con.close()
    return out


def _row_to_dict(r):
    name, cat, website, country, locality, region, freeform = r
    return {
        "name": name or "",
        "cat": cat or "",
        "website": website or "",
        "country": (country or "").upper(),
        "locality": locality or "",
        "region": region or "",
        "freeform": freeform or "",
    }


def build_directory_rows(matched, seen_domains, max_new, now):
    """Dedupe + filter matched rows into Directory rows.

    Returns (rows, stats). seen_domains are domain_of() values already
    in Directory or Queue.
    """
    rows = []
    stats = {"matched": len(matched), "no_domain": 0, "skip_domain": 0,
             "excluded_pk_in": 0, "already_known": 0, "dup_in_batch": 0,
             "new": 0}
    batch_domains = set()
    for m in matched:
        domain = domain_of(m["website"])
        if not domain:
            stats["no_domain"] += 1
            continue
        if any(d in domain for d in SKIP_DOMAINS):
            stats["skip_domain"] += 1
            continue
        if m["country"] in EXCLUDED_COUNTRIES:
            stats["excluded_pk_in"] += 1
            continue
        if domain in seen_domains:
            stats["already_known"] += 1
            continue
        if domain in batch_domains:
            stats["dup_in_batch"] += 1
            continue
        batch_domains.add(domain)
        locality = m["locality"] or m["region"] or m["country"]
        address = ", ".join(p for p in [m["freeform"], m["locality"],
                                        m["region"], m["country"]] if p)
        niche = NICHE_MAP.get(m["cat"], "web design agency")
        rows.append([
            m["name"], "design agency", "", "",
            address, "", "", "",
            full_url(m["website"]), "", "",
            f"overture:{domain}",
            "overture import", locality, niche, now,
        ])
        stats["new"] += 1
        if len(rows) >= max_new:
            break
    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default="",
                    help="Overture release, e.g. 2026-10-21.0 (default: latest from STAC)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--max-new", type=int, default=5000,
                    help="max rows this shard appends (default 5000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the Sheet for dedupe but append nothing")
    ap.add_argument("--work-dir", default="overture_work",
                    help="download + collections cache dir")
    ap.add_argument("--parquet-dir", default="",
                    help="test mode: use local *.parquet files, skip STAC/download")
    ap.add_argument("--bbox", action="append", default=[],
                    help="test mode: restrict to xmin,xmax,ymin,ymax (repeatable)")
    ap.add_argument("--sample", type=int, default=0,
                    help="test mode: ORDER BY id LIMIT N per --bbox")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write the Log tab line")
    ap.add_argument("--seen-domains-file", default="",
                    help="test mode: one domain per line to use as the "
                    "already-known set instead of reading the Sheet")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()
    os.makedirs(args.work_dir, exist_ok=True)
    bboxes = []
    for b in args.bbox:
        parts = [float(x) for x in b.split(",")]
        if len(parts) != 4:
            raise SystemExit(f"--bbox needs xmin,xmax,ymin,ymax, got {b!r}")
        bboxes.append(tuple(parts))

    if args.parquet_dir:
        import glob
        parquet_paths = sorted(
            p for p in glob.glob(os.path.join(args.parquet_dir, "*.parquet"))
            if os.path.basename(p) != "collections.parquet")
        if not parquet_paths:
            raise SystemExit(f"no *.parquet in {args.parquet_dir}")
        release = args.release or "local-test"
        files = [(os.path.basename(p), p, os.path.getsize(p)) for p in parquet_paths]
        print(f"overture: test mode, {len(parquet_paths)} local files", flush=True)
    else:
        release = resolve_release(args.release)
        files = select_files(release, args.shard, args.shards, args.work_dir)
        print(f"overture: release={release} shard={args.shard}/{args.shards} "
              f"files={len(files)}", flush=True)
        parquet_paths = download_files(files, args.work_dir)
    downloaded_mb = round(
        sum(os.path.getsize(p) for p in parquet_paths) / 1e6, 1)
    print(f"overture: downloaded={downloaded_mb} MB in "
          f"{len(parquet_paths)} files", flush=True)

    matched = extract_rows(parquet_paths, bboxes or None, args.sample)
    print(f"overture: matched={len(matched)} agency rows", flush=True)

    seen_domains = set()
    if args.seen_domains_file:
        with open(args.seen_domains_file, encoding="utf-8") as fh:
            for line in fh:
                d = line.strip().lower()
                if d:
                    seen_domains.add(d)
    else:
        for tab_range, col in (("Directory!I:I", 0), ("Queue!B:B", 0)):
            for row in sheets.read_values(tab_range)[1:]:
                if row:
                    d = domain_of(row[col])
                    if d:
                        seen_domains.add(d)
    print(f"overture: seen domains in Sheet: {len(seen_domains)}", flush=True)

    rows, stats = build_directory_rows(matched, seen_domains, args.max_new, now)
    stats["downloaded_mb"] = downloaded_mb
    stats["files"] = len(parquet_paths)
    print(f"overture: {stats}", flush=True)

    appended = 0
    if rows and not args.dry_run:
        for i in range(0, len(rows), APPEND_CHUNK):
            sheets.append_values("Directory!A:P", rows[i:i + APPEND_CHUNK])
        appended = len(rows)
    elif rows:
        print("overture: DRY RUN - would append these 5 sample rows "
              "(full row = name, category, ..., website, place_key, "
              "query, locality, niche, scraped_at):", flush=True)
        for r in rows[:5]:
            print(f"  {r}", flush=True)

    summary = (f"shard={args.shard}/{args.shards} release={release} "
               f"files={stats['files']} downloaded_mb={downloaded_mb} "
               f"matched={stats['matched']} new={stats['new']} "
               f"already_known={stats['already_known']} "
               f"excluded_pk_in={stats['excluded_pk_in']} "
               f"skip_domain={stats['skip_domain']} appended={appended}")
    print(f"overture tick done: {summary}", flush=True)
    if not args.dry_run and not args.no_log:
        try:
            sheets.append_values("Log!A:D", [[now, "overture", summary, appended]])
        except Exception:
            pass


if __name__ == "__main__":
    main()
