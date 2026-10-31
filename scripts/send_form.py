"""Send tick: har 10 minute par, 24h, SUCCESS target 500/day.

User order (2026-10-31, saaf): kam se kam 500 forms SUCCESS hone chahiye
rozana. Fail wale is me count NAHI honge.

Is liye counting ye hai:
- SUCCESS = form server ne accept kiya aur thank-you / success response mila.
  Sirf yehi Sent tab me delivered likha jayega aur 500 ke target me gina jayega.
- FAIL = CAPTCHA, block, timeout, form error, ya koi success response nahi mila.
  Ye Fail log me jayega, success count me NAHI gina jayega, aur us lead ko
  us din dobara try nahi kiya jayega (transient network error ke ilawa, ek retry max).
- Daily loop: jab tak delivered_success < 500 hai, aur din khatam nahi hua,
  aur sendable queue me leads hain, system agla lead bhejta rahega.
- Attempts ka koi 700 wala cap nahi hai, kyunki fail count nahi hote.
  Har tick par max 10 attempts (parallel workers se, ek second me burst nahi),
  taake fail rate zyada ho to bhi 500 success tak pahuncha ja sake.
  Example: agar success rate 50% ho to 1000 attempts se 500 success milenge.

Jo cheezain AB BHI nahi hongi:
- CAPTCHA bypass nahi, wo fail hai.
- Fake success log nahi. Agar success response nahi mila to delivered nahi likha jayega.
- Queue khatam ho jaye to fake leads nahi banenge. Shortfall Log me saaf
  likha jayega: delivered X, attempted Y, failed Z, queue exhausted.

Is skeleton me live Playwright submit abhi deliberately nahi hai, taake
template approve hone se pehle koi accidental send na ho.
"""
import argparse, datetime, os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-send", type=int, default=10)
    args = ap.parse_args()
    dry = os.environ.get("DRY_RUN", "true").lower() != "false"
    now = datetime.datetime.now(datetime.timezone.utc)
    if not os.environ.get("SHEET_ID"):
        print("send tick: SHEET_ID secret missing, live send blocked, dry-run only")
        return
    print(f"send tick UTC={now.isoformat()} dry_run={dry} max_attempts_per_tick={args.max_send}")
    print("success target: 500 delivered/day, fails do NOT count toward 500")
    print("guards to check from Sheet: delivered_success <500, approved form-only lead, no captcha")
    print("live sends = 0 in this skeleton (template approval pending)")


if __name__ == "__main__":
    main()
