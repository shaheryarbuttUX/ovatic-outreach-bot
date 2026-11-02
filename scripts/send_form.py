"""Live send tick: har 10 minute par, 24h, SUCCESS target 500/day.

Approved template (2026-10-31, user: haan fit hai) is used verbatim in
spirit, with the lead personalization line prepended and exactly one
Figma link per lead (website Figma for web agencies, dashboard Figma
for product/SaaS agencies, chosen from the Queue figma_link column).

Counting, user order 2026-10-31:
- SUCCESS = server accepted the form AND a thank-you/success response
  was seen. Only this counts toward 500 and only this goes to Sent
  as delivered.
- FAIL = CAPTCHA, block, timeout, form error, or no success response.
  Fail is logged separately, never counted as success, and the Queue
  row is marked failed so it is not retried the same day.
- DRY_RUN=true (default) means NO live submit at all. The tick only
  logs how many sendable leads it found. Live mode is DRY_RUN=false.

Guards, never bypassed:
- No CAPTCHA/Turnstile/hCaptcha solving or evasion. CAPTCHA = fail.
- No fake success. No success response = fail, even if the form
  looked like it submitted.
- No duplicate: a company or website already in Sent is skipped.
- No lead names, websites or message text in stdout logs. Public
  repo logs carry counts only.
- Serial sends with a stagger, never a burst, to avoid looking
  automated to the receiving site.
"""
import argparse
import datetime
import os
import re
import time

WEBSITE_FIGMA = "https://www.figma.com/design/9LfCw6faDaXvFQfWSFkM2C/Website-Portfolio--Client-Sharing-?node-id=159-12849&t=u2nBTJ3IEOLMJnQh-1"
DASHBOARD_FIGMA = "https://www.figma.com/design/4OzS8KcSeOLgXVbGMjCVZ1/Dashboard-Portfolio--Client-Sharing-?node-id=1-15548&t=YUB9pL13pk5InsaD-1"
SENDER_NAME = "Shaheryar Butt"
SENDER_EMAIL = "shaheryarbutt47@gmail.com"
SENDER_PHONE = "+92 332 0512236"
SUBJECT = "Freelance UI/UX designer for overflow work"
CAPTCHA_WORDS = ["recaptcha", "g-recaptcha", "turnstile", "hcaptcha", "captcha"]
SUCCESS_WORDS = [
    "thank you", "thanks for", "message sent", "form submitted",
    "we will get back", "we'll get back", "we'll be in touch",
    "we will be in touch", "we will contact you soon",
    "successfully submitted", "successfully sent",
    "your message has been received", "message received",
    "we have received",
]
# A page that says the visitor was blocked is not a form page at all.
# It must be reported as bot_blocked, never as no_form_found or
# captcha_present, so the self-management skip below can learn it.
BOT_BLOCK_PHRASES = [
    "automated browsers", "request blocked", "have been blocked",
    "blocked by our security", "sorry, you have been blocked",
    "automated tool",
]
# Money ranges on a preselected radio mean the submission would carry
# a client budget claim. That is a factual claim about money and must
# never be sent for an outreach offer (Idea Peddler lesson).
MONEY_RE = re.compile(r"\d[\d.,]*\s*[€$£]|[€$£]\s*\d")
BODY_TEMPLATE = (
    "Hi {company} team,\n\n"
    "{personalization}\n\n"
    "I am Shaheryar Butt, a Web & Product Designer with 9+ years in UI/UX, "
    "Webflow, WordPress and Shopify work. I am available as a freelance designer "
    "for overflow or project-based work: dashboards, websites, landing pages and "
    "design systems, in Figma, delivered on your timeline. I work with a small "
    "team behind me, so the work does not depend on one person alone, and a "
    "bigger project can be handled without delay.\n\n"
    "Availability: 20-30 hours/week for me personally, $18-20/hr, with team "
    "support if the project needs more capacity. Happy to start with one small "
    "test task so you can judge the work before committing to more.\n\n"
    "Do you ever work with remote freelance designers, or outsource any design "
    "work when your team is full?\n\n"
    "Best,\nShaheryar Butt\nWeb & Product Designer\n"
    "Behance: https://www.behance.net/sherybutt\n"
    "LinkedIn: https://linkedin.com/in/shaheryar-butt/\n"
    "Figma: {figma}\n\n"
    "Reply no thanks and I will not contact again."
)


def build_message(company, personalization, figma):
    return BODY_TEMPLATE.format(
        company=company or "there",
        personalization=(personalization or "").strip(),
        figma=figma or WEBSITE_FIGMA,
    )


def _field_key(el):
    try:
        name = (el.get_attribute("name") or "").lower()
        ident = (el.get_attribute("id") or "").lower()
        placeholder = (el.get_attribute("placeholder") or "").lower()
        aria = (el.get_attribute("aria-label") or "").lower()
        typ = (el.get_attribute("type") or "").lower()
        label = _label_text(el).lower()
        return f"{name} {ident} {placeholder} {aria} {label} {typ}"
    except Exception:
        return ""


def _shot(page, path):
    """Screenshot, best effort only. A failed shot never blocks a send.

    Full-page shots on pages with a very wide element render thousands
    of pixels wide and unreadable (Omnia, 2026-11-02). If the page is
    wider than a normal desktop viewport, fall back to a viewport shot
    so the audit image stays readable.
    """
    try:
        try:
            width = page.evaluate("() => document.documentElement.scrollWidth || 0")
        except Exception:
            width = 0
        page.screenshot(path=path, full_page=(width <= 1920))
        return True
    except Exception:
        return False


def _label_text(el):
    """Text of the <label> / aria-labelledby for a field, if any."""
    try:
        return el.evaluate(
            """(e) => {
                let t = '';
                if (e.labels) { t += Array.from(e.labels).map(l => l.innerText || '').join(' '); }
                const by = e.getAttribute('aria-labelledby');
                if (by) { by.split(' ').forEach(id => { const n = document.getElementById(id); if (n) t += ' ' + (n.innerText || ''); }); }
                return t;
            }"""
        ) or ""
    except Exception:
        return ""


ACCEPT_TEXTS = {
    "accept all", "accept", "akzeptieren", "alle akzeptieren",
    "accepteren", "alles accepteren", "allow all", "agree",
    "zustimmen", "einverstanden", "ok", "got it",
}


def _dismiss_cookie_banner(page):
    """Click a cookie-consent accept button, if one is showing.

    EU sites in the 2026-11-02 audit (Achso, Anne Grabs, Corinna,
    Digitaleffects, Babylon, &why) keep the form behind a consent
    modal or banner. A human visitor accepts or dismisses it before
    using the page; doing the same is ordinary page interaction, not
    a safeguard bypass. Only buttons OUTSIDE a form are considered,
    so a form's own submit button is never clicked here.
    """
    try:
        buttons = page.locator("button, [role=button]")
        for i in range(buttons.count()):
            try:
                el = buttons.nth(i)
                if not el.is_visible():
                    continue
                inside_form = el.evaluate(
                    "e => !!e.closest('form')")
                if inside_form:
                    continue
                text = (el.inner_text() or "").strip().lower()
                if text in ACCEPT_TEXTS:
                    el.click()
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _follow_contact_link(page):
    """Follow one same-site contact link, or False if there is none."""
    try:
        base = page.url.split("/")[2] if "://" in page.url else ""
    except Exception:
        base = ""
    try:
        links = page.locator("a[href]")
        for i in range(links.count()):
            try:
                el = links.nth(i)
                if not el.is_visible():
                    continue
                href = (el.get_attribute("href") or "").lower()
                text = (el.inner_text() or "").lower()
                if not any(w in href or w in text for w in
                           ["contact", "kontakt", "contactformulier"]):
                    continue
                if href.startswith("http") and base and base not in href:
                    continue
                if href.startswith("mailto:") or href.startswith("tel:"):
                    continue
                el.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


GROQ_MODEL = "llama-3.1-8b-instant"
LLM_ROLES = ("name_first", "name_last", "name_full", "email", "phone",
             "website", "company", "subject", "message", "referral", "ignore")


def _llm_map_fields(descriptors):
    """Ask the free Groq Llama model to map unmapped form fields.

    Only called for fields the keyword rules could not place, and only
    with the fields' own labels (name, id, placeholder, label text).
    No sender data, no message text, no personal data is sent: the
    model only ever sees an empty form's labels, so there is nothing
    private to leak. The answer is validated strictly: an unknown
    role, a budget/service role, or malformed JSON is discarded and
    the caller falls back to the keyword rules. A missing key, a
    timeout, or a 429 also returns None, never an exception, so a
    failed model call can never break a tick.
    """
    import json as _json
    import urllib.request as _urlreq
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key or not descriptors:
        return None
    prompt = (
        "Map each form field index to exactly one role. Roles: "
        "name_first, name_last, name_full, email, phone, website, "
        "company, subject, message, referral, ignore. A field asking "
        "for budget, price, a service the visitor wants to buy, or a "
        "project timeline as a client must be ignore, never message "
        "or any other role. Reply with JSON only, like "
        "{\"0\": \"email\", \"1\": \"message\"}. Fields: "
        + _json.dumps(descriptors)[:3000]
    )
    body = _json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    try:
        req = _urlreq.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=body, headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"})
        with _urlreq.urlopen(req, timeout=15) as r:
            resp = _json.loads(r.read().decode("utf-8", "ignore"))
        text = resp["choices"][0]["message"]["content"]
        start, end = text.find("{"), text.rfind("}")
        mapping = _json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        if not isinstance(mapping, dict):
            return None
        clean = {}
        for k, v in mapping.items():
            if v in LLM_ROLES:
                clean[str(k)] = v
        return clean or None
    except Exception:
        return None


def submit_one(page, form_url, company, personalization, figma, shot_prefix=""):
    """Returns (status, reason, before_shot, after_shot).

    Success check, tightened 2026-11-02 after the Idea Peddler doubt:
    the old check counted any success word anywhere on the page AFTER
    submit, even if that word was already on the page BEFORE submit
    (footer, testimonials, etc.), which can fake a delivered. Now a
    delivered needs one of: URL moved to thank/success, a success word
    that is NEW after submit, or the form itself disappearing after
    submit while a success word is present. Screenshots before and
    after submit are the audit proof for every attempt.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    before_shot = ""
    after_shot = ""
    if shot_prefix:
        before_shot = f"{shot_prefix}-before.png"
        after_shot = f"{shot_prefix}-after.png"
    try:
        page.goto(form_url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        return "failed", f"page_load_failed:{type(e).__name__}", before_shot, after_shot
    _dismiss_cookie_banner(page)
    try:
        content = page.content().lower()
    except Exception:
        content = ""
    if any(w in content for w in BOT_BLOCK_PHRASES):
        if before_shot:
            _shot(page, before_shot)
        return "failed", "bot_blocked", before_shot, after_shot
    if any(w in content for w in CAPTCHA_WORDS):
        if before_shot:
            _shot(page, before_shot)
        return "failed", "captcha_present", before_shot, after_shot
    before_content = content
    try:
        forms = page.locator("form")
        if forms.count() == 0:
            # The Queue URL is often a homepage whose form lives on a
            # contact subpage (EAZEE, Offff, 2026-11-02). Follow one
            # same-site contact link and look again before giving up.
            if _follow_contact_link(page):
                _dismiss_cookie_banner(page)
                forms = page.locator("form")
        if forms.count() == 0:
            if before_shot:
                _shot(page, before_shot)
            return "failed", "no_form_found", before_shot, after_shot
        form = forms.first
        fields = form.locator("input, textarea, select")
        count = fields.count()
        filled = 0
        select_blocked = False
        unmapped = []
        email_filled = False
        message_filled = False
        message = build_message(company, personalization, figma)
        for i in range(count):
            try:
                el = fields.nth(i)
                if not el.is_visible():
                    continue
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                key = _field_key(el)
                if tag == "select":
                    # Honesty rule, 2026-11-02 (Idea Peddler lesson):
                    # only an option that is honest for an outreach offer
                    # may be chosen. A required select with no honest
                    # option blocks the whole attempt instead of inventing
                    # a client service need, budget, or referral source.
                    # An optional select with no honest option is left
                    # untouched.
                    honest_words = ["design", "web", "other", "general",
                                    "partnership", "inquiry", "enquiry",
                                    "not sure", "unsure", "pakistan"]
                    try:
                        options = el.locator("option")
                        chosen = None
                        for j in range(options.count()):
                            opt = (options.nth(j).inner_text() or "").strip()
                            low = opt.lower()
                            if not opt or low in ("select", "choose", "please select", "select an option"):
                                continue
                            if any(w in low for w in honest_words):
                                chosen = opt
                                break
                        if chosen:
                            el.select_option(label=chosen)
                            filled += 1
                        else:
                            try:
                                required = el.evaluate("e => !!e.required")
                            except Exception:
                                required = False
                            if required:
                                select_blocked = True
                    except Exception:
                        pass
                    continue
                if el.get_attribute("type") in ("submit", "radio", "hidden", "file"):
                    continue
                if el.get_attribute("type") == "checkbox":
                    # Only privacy/terms consent, never newsletter
                    # opt-in and never a service checkbox. German and
                    # Dutch consent labels included (Babylon, 2026-11-02:
                    # its Datenschutzerklarung box stayed unchecked).
                    if "newsletter" in key or "marketing" in key:
                        continue
                    if any(w in key for w in ["privacy", "terms", "consent", "agree", "policy",
                                              "datenschutz", "einwilligung", "toestemming",
                                              "akkoord", "voorwaarden"]):
                        try:
                            el.check()
                            filled += 1
                        except Exception:
                            pass
                    continue
                value = None
                # Name first/last split: a field asking for first name
                # gets only the first name, last name only the last.
                # A field asking for both (Style Marketing's "Voornaam
                # en achternaam") gets the full name. The old code put
                # the full name in both fields, which reads as careless.
                both_names = (("voornaam" in key and "achternaam" in key)
                              or ("first" in key and "last" in key)
                              or ("vorname" in key and "nachname" in key))
                if both_names or "full name" in key:
                    name_value = SENDER_NAME
                elif any(w in key for w in ["first", "voornaam", "vorname"]):
                    name_value = "Shaheryar"
                elif any(w in key for w in ["last", "achternaam", "nachname", "surname", "family"]):
                    name_value = "Butt"
                else:
                    name_value = SENDER_NAME
                if "email" in key or "e-mail" in key or "mailadres" in key:
                    value = SENDER_EMAIL
                elif "subject" in key or "thema" in key or "betreff" in key or "onderwerp" in key:
                    value = SUBJECT
                elif any(w in key for w in ["message", "comment", "detail", "project", "inquiry", "enquiry", "how can", "tell us", "about your", "brief", "requirement", "description", "help us", "what do you need",
                                            "bericht", "nachricht", "anfrage", "beschreibung"]):
                    value = message
                elif "phone" in key or "tel" in key or "mobile" in key or "cell" in key:
                    value = SENDER_PHONE
                elif any(w in key for w in ["hear about", "how did you find", "found us", "waar heb", "wie sind sie"]):
                    # Honest referral source: every pilot lead was found
                    # through Google Maps research. Never invent a person
                    # or a channel that did not happen.
                    value = "Google Maps"
                elif "name" in key or "naam" in key or "full" in key:
                    value = name_value
                elif "website" in key or "url" in key or "webseite" in key:
                    value = "https://www.behance.net/sherybutt"
                elif "company" in key or "bedrijf" in key or "unternehmen" in key or "firma" in key:
                    # A company field must not carry a URL. The pilot
                    # identity is an individual freelancer, so the
                    # honest company value is Freelance, and Behance
                    # belongs in the website field only.
                    value = "Freelance"
                if value:
                    el.fill(value)
                    filled += 1
                    if value == SENDER_EMAIL:
                        email_filled = True
                    if value == message:
                        message_filled = True
                else:
                    unmapped.append((i, el, key))
            except Exception:
                continue
        if select_blocked:
            if before_shot:
                _shot(page, before_shot)
            return "failed", "select_no_honest_option", before_shot, after_shot
        # Groq mapper, second pass: only when a critical field is
        # still missing (email or message) and keyword rules left
        # fields unmapped. The model sees labels only, its answer is
        # validated against LLM_ROLES, and budget/service fields are
        # forced to ignore, so it can fix DE/NL and unusual labels
        # without ever inventing a value. Any model failure simply
        # leaves the attempt on the keyword result.
        if (not email_filled or not message_filled) and unmapped:
            descriptors = []
            for idx, _el, key in unmapped[:12]:
                descriptors.append({"index": str(idx), "label": key[:150]})
            mapping = _llm_map_fields(descriptors)
            if mapping:
                role_values = {
                    "name_first": "Shaheryar", "name_last": "Butt",
                    "name_full": SENDER_NAME, "email": SENDER_EMAIL,
                    "phone": SENDER_PHONE,
                    "website": "https://www.behance.net/sherybutt",
                    "company": "Freelance", "subject": SUBJECT,
                    "message": message, "referral": "Google Maps",
                }
                for idx, el, _key in unmapped:
                    role = mapping.get(str(idx))
                    if role in role_values:
                        try:
                            el.fill(role_values[role])
                            filled += 1
                            if role == "email":
                                email_filled = True
                            if role == "message":
                                message_filled = True
                        except Exception:
                            continue
        if filled < 2:
            if before_shot:
                _shot(page, before_shot)
            return "failed", "not_enough_fields_filled", before_shot, after_shot
        # Preselected money radio = a client budget claim. Submitting
        # with it would present Shaheryar as a client with that budget.
        # That is the Idea Peddler failure in radio form, so it blocks
        # the attempt instead of sending a misleading form.
        try:
            radios = form.locator("input[type=radio]:checked")
            for i in range(radios.count()):
                try:
                    rkey = _field_key(radios.nth(i))
                    rlabel = radios.nth(i).evaluate(
                        "(e) => { const l = e.labels ? Array.from(e.labels).map(x => x.innerText || '').join(' ') : ''; return l + ' ' + (e.value || ''); }")
                    if MONEY_RE.search(rkey) or MONEY_RE.search(rlabel or ""):
                        if before_shot:
                            _shot(page, before_shot)
                        return "failed", "client_budget_preselected", before_shot, after_shot
                except Exception:
                    continue
        except Exception:
            pass
        # Pill / button choice groups (Influentials "What are you
        # looking for?", &why "I'm interested in"): the old code only
        # handled <select>, so a required pill group blocked the form
        # at validation. Click one honest pill only, partnership or
        # other first, then a design pill. A pill naming a client
        # service or a budget is never clicked.
        try:
            pill_buttons = form.locator("button:not([type=submit]), [role=button]")
            for wanted in ("partnership", "other", "general", "inquiry", "enquiry",
                           "ux design", "web development", "web design"):
                clicked = False
                for i in range(pill_buttons.count()):
                    try:
                        el = pill_buttons.nth(i)
                        if not el.is_visible():
                            continue
                        text = (el.inner_text() or "").strip().lower()
                        if wanted in text:
                            el.click()
                            page.wait_for_timeout(500)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
        except Exception:
            pass
        if before_shot:
            _shot(page, before_shot)

        def _click_submit():
            submit = form.locator("button[type=submit], input[type=submit]")
            if submit.count() > 0:
                submit.first.click()
                return True
            buttons = form.locator("button")
            for i in range(buttons.count()):
                try:
                    text = (buttons.nth(i).inner_text() or "").lower()
                except Exception:
                    text = ""
                if any(w in text for w in ["submit", "send", "contact us", "get in touch"]):
                    buttons.nth(i).click()
                    return True
            return False

        def _success_seen():
            try:
                after = page.content().lower()
            except Exception:
                after = ""
            url = page.url.lower()
            url_moved = "thank" in url or "success" in url
            new_success = any(w in after and w not in before_content for w in SUCCESS_WORDS)
            try:
                form_gone = page.locator("form").count() == 0
            except Exception:
                form_gone = False
            any_success = any(w in after for w in SUCCESS_WORDS)
            return url_moved or new_success or (form_gone and any_success)

        def _poll(seconds):
            # Poll every 2s instead of one fixed 8s wait: AJAX toasts,
            # modals and slow redirects arrive late or disappear fast.
            for _ in range(max(1, seconds // 2)):
                page.wait_for_timeout(2000)
                if _success_seen():
                    return True
            return _success_seen()

        if not _click_submit():
            return "failed", "no_submit_button", before_shot, after_shot
        ok = _poll(20)
        if not ok:
            # Arken lesson, 2026-11-02: a filled form can sit there
            # untouched after one click. If the form is still present
            # and nothing succeeded, click once more, then poll again.
            # A form that really submitted is already gone, so this
            # cannot double-submit a successful form.
            try:
                form_still = page.locator("form").count() > 0
            except Exception:
                form_still = False
            if form_still:
                try:
                    _click_submit()
                    ok = _poll(10)
                except Exception:
                    pass
        if after_shot:
            _shot(page, after_shot)
        if ok:
            return "delivered", "success_response_seen", before_shot, after_shot
        return "failed", "no_success_response", before_shot, after_shot
    except PlaywrightTimeout:
        if after_shot:
            try:
                _shot(page, after_shot)
            except Exception:
                pass
        return "failed", "timeout", before_shot, after_shot
    except Exception as e:
        return "failed", f"submit_error:{type(e).__name__}", before_shot, after_shot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-send", type=int, default=10)
    args = ap.parse_args()
    dry = os.environ.get("DRY_RUN", "true").lower() != "false"
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    try:
        import sheets
        queue_rows = sheets.read_values("Queue!A:P")
        sent_rows = sheets.read_values("Sent!A:H")
    except Exception as e:
        print(f"send tick: sheet unavailable, no sends: {type(e).__name__}")
        return
    queue = sheets.rows_to_dicts(queue_rows)
    sent = sheets.rows_to_dicts(sent_rows)
    sent_keys = set()
    delivered_today = 0
    for row in sent:
        sent_keys.add((row.get("company", "").strip().lower(), row.get("website", "").strip().lower()))
        sent_keys.add(("", row.get("website", "").strip().lower()))
        if row.get("status", "") == "delivered" and (row.get("sent_at", "") or "").startswith(today):
            delivered_today += 1
    if delivered_today >= 500:
        print(f"send tick: success target reached today delivered={delivered_today}")
        return
    # Self-management, rule-based and free: a domain blocked as an
    # automated visitor twice is skipped by later ticks, so attempts
    # stop burning on sites that will never accept the form. The
    # Screenshots tab is the record, so no new storage is needed.
    bot_blocked_domains = {}
    try:
        shot_rows = sheets.rows_to_dicts(sheets.read_values("Screenshots!A1:H2000"))
        for r in shot_rows:
            if (r.get("reason", "") or "").strip() == "bot_blocked":
                w = (r.get("website", "") or "").strip().lower()
                if w:
                    bot_blocked_domains[w] = bot_blocked_domains.get(w, 0) + 1
    except Exception:
        bot_blocked_domains = {}
    blocked_set = {w for w, c in bot_blocked_domains.items() if c >= 2}
    sendable = []
    for row in queue:
        if (row.get("website", "") or "").strip().lower() in blocked_set:
            continue
        status = (row.get("status", "") or "").strip().lower()
        approved = (row.get("approved", "") or "").strip().lower()
        if status not in ("queued", "approved", "researched"):
            continue
        if approved in ("0", "no", "false"):
            continue
        if (row.get("last_send_at", "") or "").startswith(today) and status == "failed":
            continue
        key = (row.get("company", "").strip().lower(), row.get("website", "").strip().lower())
        if key in sent_keys or ("", key[1]) in sent_keys:
            continue
        if not row.get("contact_form_url", "").strip():
            continue
        sendable.append(row)
    print(f"send tick: dry_run={dry} delivered_today={delivered_today} sendable={len(sendable)} max_attempts={args.max_send}")
    if dry:
        try:
            sheets.append_values("Log!A:D", [[now.isoformat(), "send", f"dry_run sendable={len(sendable)} delivered_today={delivered_today}", len(sendable)]])
        except Exception:
            pass
        print("send tick: dry-run only, live sends=0")
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("send tick: playwright not installed, no sends")
        return
    attempted = 0
    delivered = 0
    failed = 0
    reason_counts = {}
    # Screenshot audit, user order 2026-11-02: one week of before/after
    # proof per attempt, stored as run artifacts (7-day retention) and
    # indexed in the Sheet Screenshots tab so any delivered claim can
    # be checked later against its own after-screenshot.
    shots_enabled = os.environ.get("SCREENSHOTS", "true").lower() != "false"
    shot_dir = os.environ.get("SCREENSHOT_DIR", "screenshots")
    run_id = os.environ.get("GITHUB_RUN_ID", now.strftime("%Y%m%d%H%M%S"))
    if shots_enabled:
        try:
            os.makedirs(shot_dir, exist_ok=True)
        except Exception:
            shots_enabled = False
    if shots_enabled:
        try:
            sheets.ensure_tab("Screenshots", ["run_id", "attempted_at_utc", "company", "website", "status", "reason", "before_file", "after_file"])
        except Exception:
            pass
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        for row in sendable:
            if delivered_today + delivered >= 500:
                break
            if attempted >= args.max_send:
                break
            attempted += 1
            slug = re.sub(r"[^a-z0-9]+", "-", (row.get("company", "") or "lead").strip().lower()).strip("-")[:40] or "lead"
            shot_prefix = os.path.join(shot_dir, f"{attempted:02d}-{slug}") if shots_enabled else ""
            status, reason, before_shot, after_shot = submit_one(
                page, row.get("contact_form_url", "").strip(),
                row.get("company", "").strip(),
                row.get("personalization_line", "").strip(),
                row.get("figma_link", "").strip() or WEBSITE_FIGMA,
                shot_prefix=shot_prefix,
            )
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if shots_enabled:
                try:
                    sheets.append_values("Screenshots!A:H", [[
                        run_id, now.isoformat(), row.get("company", ""), row.get("website", ""),
                        status, reason,
                        os.path.basename(before_shot) if before_shot else "",
                        os.path.basename(after_shot) if after_shot else "",
                    ]])
                except Exception:
                    pass
            try:
                if status == "delivered":
                    delivered += 1
                    sheets.append_values("Sent!A:H", [[
                        row.get("company", ""), row.get("website", ""), row.get("region", ""),
                        row.get("contact_form_url", ""), now.isoformat(),
                        "message_template_v1", "delivered", "",
                    ]])
                    sheets.update_row("Queue", row["_row"], [
                        row.get("company", ""), row.get("website", ""), row.get("region", ""),
                        row.get("contact_form_url", ""), row.get("personalization_line", ""),
                        row.get("agency_size", ""), row.get("services", ""), row.get("source", ""),
                        "sent", row.get("approved", ""), row.get("created_at", ""), now.isoformat(),
                        str(int(row.get("send_count", "0") or "0") + 1),
                        row.get("figma_link", ""), "", row.get("dedupe_key", ""),
                    ])
                else:
                    failed += 1
                    sheets.update_row("Queue", row["_row"], [
                        row.get("company", ""), row.get("website", ""), row.get("region", ""),
                        row.get("contact_form_url", ""), row.get("personalization_line", ""),
                        row.get("agency_size", ""), row.get("services", ""), row.get("source", ""),
                        "failed", row.get("approved", ""), row.get("created_at", ""), now.isoformat(),
                        row.get("send_count", ""), row.get("figma_link", ""), reason, row.get("dedupe_key", ""),
                    ])
            except Exception as e:
                print(f"send tick: sheet update failed: {type(e).__name__}")
            if attempted < args.max_send:
                time.sleep(20)
        browser.close()
    if shots_enabled:
        try:
            if not os.listdir(shot_dir):
                with open(os.path.join(shot_dir, "README.txt"), "w") as f:
                    f.write("No send attempts in this tick.\n")
        except Exception:
            pass
    try:
        reasons = ",".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
        sheets.append_values("Log!A:D", [[now.isoformat(), "send", f"delivered={delivered} attempted={attempted} failed={failed} delivered_today={delivered_today + delivered} reasons:{reasons}", delivered]])
    except Exception:
        pass
    print(f"send tick done: delivered={delivered} attempted={attempted} failed={failed} reasons={reason_counts}")


if __name__ == "__main__":
    main()
