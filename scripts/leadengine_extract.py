#!/usr/bin/env python3
"""LeadEngine agency fuel extraction for the outreach bot.

Pulls UK agency leads from the LeadEngine MCP server (Companies House
database, 5.7M companies) and appends the verified ones to the Sheet
Directory tab, where bridge.py picks them up unchanged.

Why LeadEngine: the agencies category holds 122,348 active UK agencies
by SIC code. The MCP search returns newest incorporations first, and
new companies mostly have no website yet, so the pipeline reads deep
offsets (each shard starts at 30000 + shard*10000, about Aug 2025 and
older) where the website rate is far higher.

Verified 2026-11-13, batches of 30 companies each:
- name search, newest companies: 13 websites "found", 2 genuine (7%)
- agencies category, newest companies: 5 found, 2 genuine (7%)
- agencies category, offset 30000 (Aug 2025): 15 found, 4 genuine (13%)
  under the strict domain gate (6 under the loose name-prefix rule,
  of which 2 were weak matches removed by hand)

Two gates make a website genuine, because the LeadEngine website
finder overstates its "found" count about 2.5x (it returns directory
pages, news pages, ICO pages and other companies' sites):
  1. Domain gate: every distinctive word of the company name must
     appear in the domain core, and after removing those words plus
     generic filler, at most 4 characters may remain. That rejects
     near-miss names (a stitchers site for a studio company, a house
     site for a strategy company) that share a long prefix with the
     company name but leave a long unexplained tail.
  2. Homepage gate: the homepage must load HTTP 200 and mention a
     distinctive word from the company name in its visible text.

Design:
- One MCP session per run (Streamable HTTP, protocol 2025-11-25 with
  fallback to 2025-06-18 / 2025-03-26, session header, no redirects).
  The token comes only from the LEADENGINE_TOKEN environment variable
  (a GitHub repo secret). It is never printed, never logged, and never
  stored anywhere by this script.
- Per-shard cursor in the LEState tab (shard, next_offset, updated_at).
  A dry run never advances the cursor, so it is repeatable.
- Counts only in stdout. No company names, domains or messages are
  ever printed: the repo is public and its logs are public.

Exit codes: 0 ok, 2 LEADENGINE_TOKEN missing/empty or rejected
(401/403), 1 any other fatal error.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402  (repo helper: Sheet access with 429 retry)

ENDPOINT = "https://leads.workjenny.agency/mcp"
PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26"}
AGENCY_SICS = {"73110", "73120", "74100", "70210"}
PAGE = 50
FIND_CHUNK = 100
IMPORT_CHUNK = 50
APPEND_CHUNK = 100
POLL_SECONDS = 30
POLL_MAX = 30  # 30 x 30s = 15 min per find_websites job
HOMEPAGE_TIMEOUT = 15
HOMEPAGE_WORKERS = 10
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
STATE_TAB = "LEState"
STATE_HEADERS = ["shard", "next_offset", "updated_at"]
BASE_OFFSET = 30000
SHARD_SPACING = 10000
TOTAL_GUARD = 122000  # wrap the cursor to 0 once it passes the list


class McpError(RuntimeError):
    """A transport or protocol failure with no automatic replay."""


class McpAuthError(McpError):
    """The token was rejected (401/403) or is missing."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise McpError("MCP endpoint redirected; verify the endpoint before continuing")


class LeadEngineClient:
    """Minimal Streamable-HTTP MCP client, Bearer token from env only."""

    def __init__(self, token):
        endpoint = urllib.parse.urlsplit(ENDPOINT)
        if (endpoint.scheme != "https" or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query
                or endpoint.fragment or endpoint.port not in (None, 443)):
            raise McpError("LeadEngine endpoint is not a plain HTTPS URL")
        self.host = endpoint.hostname
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())
        self.session = None
        self.protocol = "2025-11-25"
        self.sequence = 0

    def send(self, method, params, *, notification=False):
        self.sequence += 1
        request_id = None if notification else self.sequence
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
            "Authorization": "Bearer " + self.token,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(body).encode(),
            headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=45) as response:
                if session := response.headers.get("Mcp-Session-Id"):
                    if not all(33 <= ord(c) <= 126 for c in session):
                        raise McpError("MCP server returned an invalid session header")
                    self.session = session
                if notification and response.status == 202:
                    return None
                if notification:
                    raise McpError("MCP server did not accept the initialization notification")
                return self.read_result(response, request_id)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise McpAuthError(
                    f"LeadEngine rejected the request (HTTP {error.code}); "
                    "the LEADENGINE_TOKEN repo secret is missing, wrong, "
                    "or expired") from None
            raise McpError(
                f"MCP returned HTTP {error.code}; the operation was not replayed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise McpError("MCP request did not complete; check any write before retrying") from None

    @staticmethod
    def decode(payload, request_id):
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpError("MCP returned an invalid JSON-RPC message")
        if type(message.get("id")) is not int or message["id"] != request_id:
            return None
        if "error" in message:
            raise McpError("MCP returned a JSON-RPC error; the operation was not replayed")
        if not isinstance(message.get("result"), dict):
            raise McpError("MCP returned no result object")
        return message["result"]

    @classmethod
    def read_result(cls, response, request_id):
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            result = cls.decode(response.read(), request_id)
            if result is None:
                raise McpError("MCP returned the wrong request ID")
            return result
        if content_type != "text/event-stream":
            raise McpError("MCP returned an unsupported content type")
        data = []
        while True:
            line = response.readline()
            if not line:
                raise McpError("MCP event stream ended before the matching result")
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
            elif not line and data:
                result = cls.decode(b"\n".join(data), request_id)
                data.clear()
                if result is not None:
                    return result

    def initialize(self):
        result = self.send("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "leadengine-extract", "version": "1"},
        })
        protocol = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        if (not isinstance(protocol, str) or protocol not in PROTOCOLS
                or not isinstance(capabilities, dict)
                or not isinstance(capabilities.get("tools"), dict)):
            raise McpError("MCP server did not negotiate a supported tool protocol")
        self.protocol = protocol
        self.send("notifications/initialized", {}, notification=True)

    def call_tool(self, name, arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise McpError(f"LeadEngine tool {name} returned an error result")
        content = result.get("content") or []
        if not content or content[0].get("type") != "text":
            raise McpError(f"LeadEngine tool {name} returned no text content")
        try:
            return json.loads(content[0]["text"])
        except ValueError:
            raise McpError(f"LeadEngine tool {name} returned non-JSON text") from None


# --------------------------------------------------------------------------
# Company / domain matching. Verified against the 2026-11-13 test batches:
# exactly the 8 hand-verified genuine websites pass and all 25 wrong ones
# (directory pages, news pages, ICO pages, other companies) fail.
# --------------------------------------------------------------------------

def norm_company(name):
    """Lowercase, strip legal suffixes and non-alphanumerics."""
    text = (name or "").lower()
    text = re.sub(r"\b(ltd|limited|llp|plc|co|company|group|the)\b", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def domain_of(website):
    """Bare lowercase domain from a URL, or ''."""
    try:
        host = urllib.parse.urlsplit(website).netloc.lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0].strip()


def domain_core(domain):
    """Domain minus www and one trailing public suffix, non-alnum stripped."""
    host = (domain or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(
        r"\.(co\.uk|org\.uk|ac\.uk|com|net|org|io|ai|dev|app|online|site"
        r"|store|website|studio|agency|media|design|co|uk)$", "", host)
    return re.sub(r"[^a-z0-9]", "", host)


def _lcp(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def domain_matches_company(domain, company):
    """True when the domain plausibly belongs to the company.

    Passes when the domain core is a prefix of the normalised company
    name (or vice versa, at least 4 chars), or when the two share a
    prefix of 10+ chars and the shorter tail is at most 5 chars. The
    tail rule is what rejects near-miss names such as a stitchers site
    for a studio company, or a house site for a strategy company.
    """
    core = domain_core(domain)
    cname = norm_company(company)
    if not core or not cname:
        return False
    if len(core) >= 4 and cname.startswith(core):
        return True
    if len(cname) >= 4 and core.startswith(cname):
        return True
    shared = _lcp(core, cname)
    if shared >= 10 and min(len(core) - shared, len(cname) - shared) <= 5:
        return True
    return False


GENERIC_WORDS = {
    "design", "designs", "studio", "studios", "media", "digital",
    "creative", "agency", "agencies", "group", "solutions", "solution",
    "marketing", "advertising", "web", "website", "websites", "labs",
    "lab", "global", "partners", "partner", "collective", "works",
    "work", "house", "brands", "brand", "consulting", "consultants",
    "consultancy", "ventures", "venture", "holdings", "company", "ltd",
    "limited", "the", "and", "co", "acquisition", "acquisitions",
    "capital", "investments", "investment", "advisory", "services",
    "service", "international", "ecommerce", "online",
}
LEGAL_WORDS = {"ltd", "limited", "llp", "plc"}


def company_words(company):
    """Lowercase alpha words of the company name, in order."""
    return re.findall(r"[a-z]+", (company or "").lower())


def distinctive_words(company):
    """Words that identify the company: len>=4, not generic/legal."""
    return [w for w in company_words(company)
            if len(w) >= 4 and w not in GENERIC_WORDS
            and w not in LEGAL_WORDS]


def _name_prefix_match(core, cname):
    """Fallback whole-name rule for companies with no distinctive word."""
    if len(core) >= 4 and cname.startswith(core):
        return True
    if len(cname) >= 4 and core.startswith(cname):
        return True
    shared = _lcp(core, cname)
    if shared >= 10 and min(len(core) - shared, len(cname) - shared) <= 5:
        return True
    return False


def domain_matches_company(domain, company):
    """True when the domain plausibly belongs to the company.

    Every distinctive word of the company name must appear in the
    domain core, and after removing those words plus generic/legal
    filler, at most 4 characters may remain. The leftover rule is
    what rejects near-miss names: a stitchers site for a studio
    company, or a house site for a strategy company, both share a
    long prefix with the company name but leave a long unexplained
    tail. Companies with no distinctive word fall back to the
    whole-name prefix rule.
    """
    core = domain_core(domain)
    cname = norm_company(company)
    if not core or not cname:
        return False
    words = distinctive_words(company)
    if not words:
        return _name_prefix_match(core, cname)
    for word in words:
        if word not in core:
            return False
    rest = core
    for word in words:
        rest = rest.replace(word, "", 1)
    for filler in sorted(GENERIC_WORDS | LEGAL_WORDS, key=len, reverse=True):
        rest = rest.replace(filler, "", 1)
    return len(rest) <= 4


def distinctive_token(company):
    """First distinctive word of the company name, for the homepage check."""
    words = distinctive_words(company)
    if words:
        return words[0]
    for word in company_words(company):
        if len(word) >= 4:
            return word
    return ""


def _visible_text(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _fetch_text(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA}, method="GET")
    with urllib.request.urlopen(request, timeout=HOMEPAGE_TIMEOUT) as response:
        if response.status != 200:
            return None
        raw = response.read(300_000)
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return None


def homepage_ok(domain, website, company):
    """True when a homepage loads HTTP 200 and names the company.

    Tries the domain root first, then the URL LeadEngine reported.
    When the company name has no distinctive word, a 200 alone passes.
    """
    token = distinctive_token(company)
    for url in (f"https://{domain}", website):
        try:
            html = _fetch_text(url)
        except Exception:
            continue
        if html is None:
            continue
        if not token:
            return True
        if token in _visible_text(html):
            return True
    return False


# --------------------------------------------------------------------------
# Sheet state: per-shard cursor in the LEState tab.
# --------------------------------------------------------------------------

def read_cursor(shard):
    """Return (row_number_or_None, next_offset) for this shard."""
    try:
        sheets.ensure_tab(STATE_TAB, STATE_HEADERS)
    except Exception:
        # First run races six shards on tab creation; one wins, the
        # rest retry the read once the tab exists.
        time.sleep(10)
    rows = sheets.rows_to_dicts(sheets.read_values(f"{STATE_TAB}!A:C"))
    for row in rows:
        if str(row.get("shard", "")).strip() == str(shard):
            try:
                return row["_row"], int(row.get("next_offset") or 0)
            except (TypeError, ValueError):
                pass
    return None, BASE_OFFSET + shard * SHARD_SPACING


def write_cursor(shard, row_number, next_offset, now):
    values = [str(shard), str(next_offset), now]
    if row_number:
        sheets.update_row(STATE_TAB, row_number, values)
    else:
        sheets.append_values(f"{STATE_TAB}!A:C", [values])


def read_seen_domains():
    """Domains already in Directory or Queue, from their website columns."""
    seen = set()
    for tab_range in ("Directory!I:I", "Queue!B:B"):
        for row in sheets.read_values(tab_range)[1:]:
            if row:
                domain = domain_of(row[0])
                if domain:
                    seen.add(domain)
    return seen


# --------------------------------------------------------------------------
# Pipeline phases.
# --------------------------------------------------------------------------

def search_window(client, cursor, window):
    """Read up to ~window agency rows from the cursor, pages of 50.

    Returns (rows, next_offset, total). The cursor is the offset where
    the next search should start. When the cursor runs past the end of
    the list it wraps to 0 once, so a shard keeps working through the
    newest companies instead of stalling forever.
    """
    rows = []
    offset = cursor
    total = None
    wrapped = False
    while len(rows) < window:
        page = client.call_tool("search_company_database", {
            "category": "agencies", "limit": PAGE,
            "offset": offset, "status": "active"})
        if total is None:
            total = page.get("total") or 0
        got = page.get("rows") or []
        if not got:
            if total and offset >= total and not wrapped:
                print("leadengine: cursor past list end, wrapping to 0",
                      flush=True)
                offset = 0
                wrapped = True
                continue
            break
        rows.extend(got)
        offset += len(got)
        if len(got) < PAGE:
            break
    return rows, offset, total


def import_companies(client, numbers):
    """Import company numbers into the LeadEngine pool, return lead ids."""
    lead_ids = []
    for i in range(0, len(numbers), IMPORT_CHUNK):
        chunk = numbers[i:i + IMPORT_CHUNK]
        result = client.call_tool("import_companies", {"numbers": chunk})
        lead_ids.extend(result.get("leadIds") or [])
    return lead_ids


def find_websites(client, lead_ids):
    """Start find_websites jobs in chunks of 100, poll to completion.

    Returns the total "found" count the jobs report. A job that does
    not finish within the poll budget is left running server-side; the
    leads are still read with get_lead afterwards, so nothing is lost.
    """
    found_total = 0
    for i in range(0, len(lead_ids), FIND_CHUNK):
        chunk = lead_ids[i:i + FIND_CHUNK]
        job = client.call_tool("find_websites", {"lead_ids": chunk})
        job_id = job.get("jobId")
        if not job_id:
            continue
        status = {}
        for _ in range(POLL_MAX):
            time.sleep(POLL_SECONDS)
            status = client.call_tool(
                "check_website_job", {"job_id": job_id})
            if status.get("status") == "completed":
                break
        found_total += status.get("found") or 0
        if status.get("status") != "completed":
            print("leadengine: website job did not complete in "
                  f"{POLL_MAX * POLL_SECONDS // 60} min; continuing with "
                  "whatever get_lead reports", flush=True)
    return found_total


def fetch_leads(client, lead_ids):
    """get_lead for each id. One bad lead never kills the shard."""
    leads = []
    for lead_id in lead_ids:
        try:
            leads.append(client.call_tool("get_lead", {"lead_id": lead_id}))
        except Exception:
            continue
    return leads


def verify_leads(leads):
    """Apply the domain gate, then the homepage gate (threaded).

    Returns a list of (domain, lead) pairs that passed both gates.
    """
    candidates = []
    for lead in leads:
        website = (lead.get("website") or "").strip()
        if not website:
            continue
        domain = domain_of(website)
        if not domain:
            continue
        if not domain_matches_company(domain, lead.get("company") or ""):
            continue
        candidates.append((domain, lead))

    def _check(item):
        domain, lead = item
        try:
            ok = homepage_ok(domain, lead.get("website") or "",
                             lead.get("company") or "")
        except Exception:
            ok = False
        return item if ok else None

    verified = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=HOMEPAGE_WORKERS) as pool:
        for result in pool.map(_check, candidates):
            if result is not None:
                verified.append(result)
    return verified


def build_rows(verified, search_by_number, seen_domains, max_new, today):
    """Build Directory rows for verified leads, deduped, capped."""
    rows = []
    batch_seen = set()
    skipped_dup = 0
    for domain, lead in verified:
        if domain in seen_domains or domain in batch_seen:
            skipped_dup += 1
            continue
        if len(rows) >= max_new:
            break
        batch_seen.add(domain)
        number = str((lead.get("registry") or {}).get("number")
                     or lead.get("lakeNumber") or "")
        search_row = search_by_number.get(number, {})
        sic = ((search_row.get("sic1") or "")[:5]
               or (lead.get("niche") or ""))
        category = "web design" if sic == "74100" else "advertising"
        town = search_row.get("town", "") or ""
        postcode = search_row.get("postcode", "") or ""
        rows.append([
            lead.get("company") or "",
            category,
            "",
            "",
            lead.get("address") or "",
            postcode,
            town,
            lead.get("phone") or "",
            "https://" + domain,
            "",
            "",
            "leadengine:" + domain,
            "leadengine agencies",
            (town + ", UK") if town else "UK",
            category,
            today,
        ])
    return rows, skipped_dup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--max-new", type=int, default=50,
                    help="max Directory rows this shard appends (default 50)")
    ap.add_argument("--window", type=int, default=1000,
                    help="search rows read per run per shard (default 1000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and verify but append nothing and do not "
                    "advance the cursor")
    ap.add_argument("--start-offset", default="",
                    help="override the LEState cursor for this run only")
    args = ap.parse_args()

    if not 0 <= args.shard < args.shards:
        print(f"leadengine: shard {args.shard} out of range for "
              f"{args.shards} shards", file=sys.stderr)
        return 1
    token = os.environ.get("LEADENGINE_TOKEN", "").strip()
    if not token:
        print("leadengine: LEADENGINE_TOKEN is missing or empty; add it "
              "as a repo secret named LEADENGINE_TOKEN", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"leadengine: shard={args.shard}/{args.shards} "
          f"max_new={args.max_new} window={args.window} "
          f"dry_run={args.dry_run}", flush=True)

    try:
        client = LeadEngineClient(token)
        client.initialize()
    except McpAuthError as error:
        print(f"leadengine: {error}", file=sys.stderr)
        return 2
    except McpError as error:
        print(f"leadengine: {error}", file=sys.stderr)
        return 1

    try:
        row_number, cursor = read_cursor(args.shard)
        if args.start_offset.strip():
            try:
                cursor = int(args.start_offset.strip())
            except ValueError:
                print("leadengine: --start-offset must be an integer",
                      file=sys.stderr)
                return 1
        print(f"leadengine: starting offset={cursor}", flush=True)

        rows, next_offset, total = search_window(client, cursor, args.window)
        print(f"leadengine: searched={len(rows)} total={total}",
              flush=True)
        search_by_number = {str(r.get("number")): r for r in rows}
        filtered = [r for r in rows
                    if (r.get("sic1") or "")[:5] in AGENCY_SICS]
        print(f"leadengine: sic_filtered={len(filtered)}", flush=True)

        lead_ids = import_companies(
            client, [str(r.get("number")) for r in filtered])
        print(f"leadengine: imported={len(lead_ids)}", flush=True)

        found = find_websites(client, lead_ids) if lead_ids else 0
        print(f"leadengine: websites_found={found}", flush=True)

        leads = fetch_leads(client, lead_ids) if lead_ids else []
        verified = verify_leads(leads)
        print(f"leadengine: genuine={len(verified)}", flush=True)

        seen_domains = read_seen_domains()
        print(f"leadengine: seen domains in Sheet: {len(seen_domains)}",
              flush=True)
        new_rows, skipped_dup = build_rows(
            verified, search_by_number, seen_domains, args.max_new, today)
        print(f"leadengine: dup_in_batch_or_sheet={skipped_dup}",
              flush=True)

        appended = 0
        if new_rows and not args.dry_run:
            for i in range(0, len(new_rows), APPEND_CHUNK):
                sheets.append_values(
                    "Directory!A:P", new_rows[i:i + APPEND_CHUNK])
            appended = len(new_rows)
        elif new_rows:
            print(f"leadengine: DRY RUN - would append {len(new_rows)} "
                  "rows, counts only (no lead data in logs)", flush=True)

        if not args.dry_run:
            write_cursor(args.shard, row_number, next_offset, now)
        print(f"leadengine tick done: shard={args.shard}/{args.shards} "
              f"searched={len(rows)} sic_filtered={len(filtered)} "
              f"imported={len(lead_ids)} websites_found={found} "
              f"genuine={len(verified)} appended={appended} "
              f"new_cursor={next_offset} dry_run={args.dry_run}",
              flush=True)
        return 0
    except McpAuthError as error:
        print(f"leadengine: {error}", file=sys.stderr)
        return 2
    except McpError as error:
        print(f"leadengine: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"leadengine: unexpected error: {error!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
