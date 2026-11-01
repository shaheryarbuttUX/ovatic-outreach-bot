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
    "we will get back", "we'll be in touch", "we will be in touch",
    "successfully submitted", "your message has been received",
    "we have received",
]
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
        return f"{name} {ident} {placeholder} {aria} {typ}"
    except Exception:
        return ""


def submit_one(page, form_url, company, personalization, figma):
    """Returns (status, reason). status is delivered or failed."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        page.goto(form_url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        return "failed", f"page_load_failed:{type(e).__name__}"
    try:
        content = page.content().lower()
    except Exception:
        content = ""
    if any(w in content for w in CAPTCHA_WORDS):
        return "failed", "captcha_present"
    try:
        forms = page.locator("form")
        if forms.count() == 0:
            return "failed", "no_form_found"
        form = forms.first
        fields = form.locator("input, textarea, select")
        count = fields.count()
        filled = 0
        message = build_message(company, personalization, figma)
        for i in range(count):
            try:
                el = fields.nth(i)
                if not el.is_visible():
                    continue
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                key = _field_key(el)
                if tag == "select":
                    # Many forms require a service/budget select; pick a
                    # design/web/other option, never a marketing opt-in.
                    try:
                        options = el.locator("option")
                        chosen = None
                        for j in range(options.count()):
                            opt = (options.nth(j).inner_text() or "").strip()
                            low = opt.lower()
                            if not opt or low in ("select", "choose", "please select"):
                                continue
                            if chosen is None:
                                chosen = opt
                            if any(w in low for w in ["design", "web", "other", "general"]):
                                chosen = opt
                                break
                        if chosen:
                            el.select_option(label=chosen)
                            filled += 1
                    except Exception:
                        pass
                    continue
                if el.get_attribute("type") in ("submit", "radio", "hidden", "file"):
                    continue
                if el.get_attribute("type") == "checkbox":
                    # Only privacy/terms consent, never newsletter opt-in.
                    if any(w in key for w in ["privacy", "terms", "consent", "agree", "policy"]):
                        try:
                            el.check()
                            filled += 1
                        except Exception:
                            pass
                    continue
                value = None
                if "email" in key:
                    value = SENDER_EMAIL
                elif "subject" in key:
                    value = SUBJECT
                elif "message" in key or "comment" in key or "detail" in key or "project" in key or "inquiry" in key or "enquiry" in key:
                    value = message
                elif "phone" in key or "tel" in key:
                    value = SENDER_PHONE
                elif "name" in key or "full" in key:
                    value = SENDER_NAME
                elif "website" in key or "url" in key or "company" in key:
                    value = "https://www.behance.net/sherybutt"
                if value:
                    el.fill(value)
                    filled += 1
            except Exception:
                continue
        if filled < 2:
            return "failed", "not_enough_fields_filled"
        submit = form.locator("button[type=submit], input[type=submit]")
        if submit.count() > 0:
            submit.first.click()
        else:
            buttons = form.locator("button")
            clicked = False
            for i in range(buttons.count()):
                try:
                    text = (buttons.nth(i).inner_text() or "").lower()
                except Exception:
                    text = ""
                if any(w in text for w in ["submit", "send", "contact us", "get in touch"]):
                    buttons.nth(i).click()
                    clicked = True
                    break
            if not clicked:
                return "failed", "no_submit_button"
        page.wait_for_timeout(8000)
        try:
            after = page.content().lower()
        except Exception:
            after = ""
        url = page.url.lower()
        if any(w in after for w in SUCCESS_WORDS) or "thank" in url or "success" in url:
            return "delivered", "success_response_seen"
        return "failed", "no_success_response"
    except PlaywrightTimeout:
        return "failed", "timeout"
    except Exception as e:
        return "failed", f"submit_error:{type(e).__name__}"


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
    sendable = []
    for row in queue:
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
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        for row in sendable:
            if delivered_today + delivered >= 500:
                break
            if attempted >= args.max_send:
                break
            attempted += 1
            status, reason = submit_one(
                page, row.get("contact_form_url", "").strip(),
                row.get("company", "").strip(),
                row.get("personalization_line", "").strip(),
                row.get("figma_link", "").strip() or WEBSITE_FIGMA,
            )
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
    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "send", f"delivered={delivered} attempted={attempted} failed={failed} delivered_today={delivered_today + delivered}", delivered]])
    except Exception:
        pass
    print(f"send tick done: delivered={delivered} attempted={attempted} failed={failed}")


if __name__ == "__main__":
    main()
