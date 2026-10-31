# Ovatic Outreach Bot (GitHub Actions pilot)

Status: TEMPLATE + FILTERS APPROVED 2026-10-31 (user: "haan fit hai").
Send run abhi bhi DRY-RUN par hai, kyunki live Playwright submitter aur
Sheet service account secret abhi banne hain. Approval mil gayi hai, is liye
repo banane ka step shuru hai, live sends us ke baad wale step me on honge.

## Repo + data decision (user, 2026-10-31)
- Repo: PUBLIC (minutes unlimited, standard Linux runners)
- Data: SIRF Google Sheet "Form Outreach Queue - GitHub", tabs Queue / Sent / Log
- Repo me lead data, queue.csv ya sent.csv kabhi commit nahi hoga (.gitignore me data/ hai)
- Public logs me lead ka naam, website ya message kabhi print nahi karna, sirf counts
- Secrets (SHEET_ID, service account) GitHub Secrets me, code me nahi

## Tick har 10 minute, 24 hours, SUCCESS target 500/day (user order, 2026-10-31)
Ab samajh gaya: 500 ka matlab 500 SUCCESS hai, attempts nahi. Fail wale
is me count NAHI honge. Is liye:
- Dono workflows ka cron '*/10 * * * *' hai, 24h, koi business hours guard nahi.
  Contact form async hai, raat ka form subah inbox me parha jata hai.
- SUCCESS ki definition: form server ne accept kiya aur thank-you / success
  response mila. Sirf yehi delivered gina jayega.
- FAIL ki definition: CAPTCHA, block, timeout, form error, ya koi success
  response nahi. Ye Fail log me jayega, success count me nahi, aur us lead
  ko us din dobara try nahi kiya jayega.
- Daily loop: jab tak delivered < 500 hai, aur din khatam nahi hua, aur
  sendable queue me leads hain, system agla lead bhejta rahega.
- Har send tick par max 10 attempts (parallel workers, burst nahi).
  Attempts ka 700 wala cap khatam hai, kyunki fail count nahi hote.
  Example: success rate 50% ho to 1000 attempts se 500 success milenge.
- Research ghante ke pehle tick par max 50 naye leads, taake roz 1000+ ki
  supply rahe. Research sirf sendable leads queue me dalega: form fields
  milen, form action ho, CAPTCHA na ho. Baqi skip.
- Agar queue khatam ho jaye ya din khatam ho jaye to system fake success
  nahi likhega. Log me saaf hoga: delivered X, attempted Y, failed Z,
  aur shortfall ki wajah (queue khatam / fail rate zyada).

## Ek dafa setup, phir roz ka kaam nahi (user, 2026-10-31)
Ye system is liye ban raha hai taake assistant ka roz time na lage aur user
ka bhi na lage. Setup ek dafa hoga, us ke baad:
- Research run khud queries se nayi agencies dhoondta hai, koi manual seeds list nahi.
- Send run khud queue se form-only leads ko bhejta hai, koi roz ki list nahi.
- Log khud queue me update hota hai, bounce/block par system khud rukta hai.
- Assistant ka kaam sirf ye reh jata hai: jab koi human reply aaye to us ko
  handle karna, aur hafte me ek dafa report dena. Roz seeds dhoondna,
  roz list banana, roz send karna, ye sab GitHub karega.
- Ek dafa approval ke baad har lead par alag approval nahi hoga. Approval
  sirf template + filters + caps par hogi: US+EU, chhoti agencies, form-only,
  max 5/day, CAPTCHA skip, duplicate nahi.

## Niche decision (2026-10-31)
Pilot niche: US + EU ki chhoti web design aur marketing agencies (2-15 logon ki team).
Geography (user, 2026-10-31): sirf US nahi, EU bhi list me. Pakistan/India target nahi.
Pitch angle (user, 2026-10-31): PILOT ME OVATIC NAHI, individual freelancer.
Sender: Shaheryar Butt, individual freelancer, "I" voice, lekin sath team hai.
Title is pilot me: Web & Product Designer (LinkedIn/CV wala Sr Product Designer nahi badal raha).
Links har form me: Behance + LinkedIn + SIRF ek Figma (website ya dashboard, agency ke kaam ke hisab se).
Ye pilot-specific exception hai, Ovatic ka standing agency rule khatam nahi hua.
Team ka matlab: kaam sirf ek bande par depend nahi, bara project ho to team
support kar sakti hai. Ovatic ka naam is pilot me nahi aayega.
- Angle: freelance designer for agency overflow / freelancer bench, white-label agency partner pitch nahi
- Capacity: $18-20/hr, 20-30 hrs/week (pehle bhi North Street / Another Colour ko isi tarah bheja tha)
- Portfolio: role-relevant Figma link (agencies ko Figma theek hai)
- Channel: SIRF contact form, email bilkul nahi (user clarification)

SMB track (med spa, boutique e-commerce, interior designer) is pilot me NAHI hai.
Wo existing IG DM routine (3 SMB/day) se chalta rahega. GitHub me SMB baad me
tab add hoga jab agency pilot ka reply rate pata chal jaye. Sirf agencies par
hamesha rukna galat hoga: agencies rate neeche le jati hain aur unke forms par
anti-spam zyada hota hai.

## Do runs, do kaam
1. Research run (.github/workflows/research.yml)
   - Har roz 2 dafa, max 10 naye leads har dafa
   - Queries se khud website nikalta hai, /contact page dhoondta hai, manual seeds lazmi nahi
   - Form URL, CAPTCHA signal, aur 1 personalization line queue me likhta hai
   - Pehle se contacted leads ko dedupe karta hai (data/sent.csv + data/queue.csv)
   - Koi message NAHI bhejta

2. Send run (.github/workflows/send.yml)
   - Sirf US + EU working hours me: 8am-11pm UTC (1pm-4am PKT, EU subah + US shaam cover)
   - Max 5 sends/day, 1 run me max 2, runs ke darmiyan gap
   - SIRF form-only leads ko form bhejta hai. Email channel is pilot me band hai,
     is liye email wali lead ko bhi form nahi bhejna jab tak user email on na kare.
   - CAPTCHA / Turnstile / reCAPTCHA mila to SKIP, kabhi bypass nahi.
   - Default DRY_RUN=true. Live send ke liye approved queue file + APPROVED=1 chahiye.

## Caps (minutes ka masla nahi, platform ka hai)
User (2026-10-31): repo public, tick har 10 minute par. Public repo par
minutes unlimited hain, is liye minutes ki wajah se schedule tang nahi karna.
Phir bhi send har 10 minute par nahi hoga, kyunki asal limit platform ki hai
(spam/block/CAPTCHA), minutes ki nahi.
Tick count: har 10 minute par ~144 ticks/day. Har send tick par max 10 attempts.
Success target 500 delivered/day, fails count nahi hote, 24h.

## Inbox monitor (Muse ka kaam, user ka nahi)
- Replies form wali email par aayengi, GitHub un ko nahi dekh sakta.
- Muse har 6 ghante par Gmail check karega, Sent sheet ke domains se match karega.
- Har reply khud parh kar classify karega: auto-reply vs human, warm vs hot.
  Auto-reply ko warm kabhi nahi kahega.
- User ko sirf warm/hot wale dikhaye jayenge, key line quote ke sath aur draft
  jawab ke sath. Baqi Sheet me log honge, user ko disturb nahi karenge.
- User ki baat sahi hai: 600-700 forms par bhi 100 conversations nahi hongi.
  Realistic total replies 2-5%, warm sirf 2-5/day, is liye monitor manageable hai.

## Queue format (Google Sheet, Queue tab, repo me nahi)
company,website,region,contact_form_url,personalization_line,agency_size,
services,source,status,approved,created_at,last_send_at,send_count,figma_link,
notes,dedupe_key
region = US | EU (country bhi likhna hai, masalan US-TX, DE, NL, UK)
channel = form_only (is pilot me email_only band hai).
Sent tab me har send ka record, Log tab me har run ka count (naam nahi).
status = researched | approved | sent | skipped_captcha | replied | bounced
approved sirf user ke haan ke baad 1 hota hai.

## Secrets (kabhi repo me nahi)
- Gmail / Sheets keys GitHub Secrets me, code me hardcode nahi.
- Lead list private repo me hi rahegi, public repo me kabhi nahi.
- Sender identity (pilot): Shaheryar Butt, individual freelancer ("I"), with a small team behind him.
  Ovatic name, logo ya agency "we" is pilot ke forms me nahi aayega.
