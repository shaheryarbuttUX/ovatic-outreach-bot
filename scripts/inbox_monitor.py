"""Inbox monitor design (Muse side, not GitHub Actions).

Replies to contact forms come back to the email used in the form, so
GitHub cannot see them. Is liye inbox monitor Muse ka kaam hai, user ka nahi.

Kaam:
1. Gmail me Sent sheet wali companies ke domains se naye mails dhoondna.
2. Har reply ko khud parhna, phir classify karna (qualify_inbound_reply skill):
   - Auto-reply: seconds/minutes me aaya, generic wording, tumhare message ka
     koi reference nahi, pehle wale auto text se byte-identical.
   - Human: tumhare message ka specific reference, follow-up sawal, price/call/
     details ki demand, personal tone.
   - Warm sirf tab jab human reply me interest ho (details, pricing, call).
   - Hot sirf tab jab next step ho (call book, proposal mangna).
3. Auto-reply ko warm kabhi nahi kehna, Sheet me auto_reply likhna.
4. Warm/Hot mile to user ko foran batana, reply ka key line quote karke,
   aur draft jawab tayar rakhna. Neutral/cold par user ko disturb nahi karna.
5. Frequency: har 6 ghante par check, US/EU din me zyada nazar rakhna.
   Pehle sends shuru hone ke baad hi monitor on hoga, us se pehle check
   karne ka koi faida nahi.

Expected volume (user ki baat sahi hai):
600-700 forms/day par bhi 100 conversations nahi hongi. Realistic:
total replies 2-5% (12-35/day), un me se zyada tar no thanks ya auto-reply,
human warm sirf 2-5/day. Is liye monitor karna mushkil nahi, aur user ko
sirf warm wale hi dikhaye jayenge, baqi Sheet me log honge.
"""
