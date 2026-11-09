# Demo recording prep

Notes for recording the demo video. Everything runs in the PayPal sandbox against the live Render
deploy: https://leash-frontend.onrender.com (dashboard) and https://leash-api-q3rw.onrender.com (API).

All commands are Windows cmd, run from the repo root.

## Before you record

1. **Reseed the demo orders** so the dashboard isn't empty. Point the script at Render first:

   ```
   set LEASH_API_URL=https://leash-api-q3rw.onrender.com
   .venv\Scripts\python.exe backend\scripts\seed_demo.py
   ```

   Pass: the last line is `11 of 11 as expected`. A reset first (`--reset`) voids the open seeded
   holds and deletes the seeded rows. It doesn't touch the `week1-e2e` orders.
2. **Open one review link in incognito on a real phone.** The confirm page should load with no
   login. This was never checked in a private tab.
3. **The demo password stays off the login screen.** Judges get it in Devpost's testing notes.

## The dispute take needs a fresh dispute

PayPal accepts evidence on a dispute **once**. The first sandbox dispute,
`PP-R-JLU-10197621` on order #29, was already filed on 2026-11-09 while building, so it can't be
filed again on camera. Set up a new one before recording.

**Which order:** a captured order that isn't seeded. Order #1 (week1-e2e) is the safe choice.
Disputes on seeded orders are fine too since #41 (a reset clears them), but a reset then forgets
the dispute in Leash while it stays open at PayPal.

**Reason:** "not as described" (SNAD). PayPal requests `OTHER` evidence for it, which is what
Leash files. Don't use "item not received": PayPal only asks for tracking or a refund there, and
Leash has neither.

Allow an hour or more for setup: each PayPal step below takes about 10 to 15 minutes to settle.

1. **Buyer opens it.** In a private window, log in at https://www.sandbox.paypal.com as
   `SANDBOX_BUYER_EMAIL`. Open the payment for the order you picked, click **Report a problem**,
   and choose the "significantly not as described" option. Note the case ID (`PP-R-...`).
2. **Wait, then check it.** Run this until the status is `WAITING_FOR_SELLER_RESPONSE` and the
   actions include `escalate`:

   ```
   .venv\Scripts\python.exe spikes\spike3_disputes.py show PP-R-XXXX-XXXXXXXX
   ```

3. **Escalate to a claim:**

   ```
   .venv\Scripts\python.exe -c "from backend.app.config import get_settings as g; from backend.app.paypal_client import client_from_settings as c; print(c(g())._post('/v1/customer/disputes/PP-R-XXXX-XXXXXXXX/escalate', {'note': 'Escalating to test the evidence flow'}))"
   ```

4. **Wait, then check again** with the same `show` command. Ready when the stage is
   `CHARGEBACK`, the status is `WAITING_FOR_SELLER_RESPONSE` and the actions include
   `provide_evidence`. If it stops at `UNDER_REVIEW` with `require_evidence` instead, run:

   ```
   .venv\Scripts\python.exe -c "from backend.app.config import get_settings as g; from backend.app.paypal_client import client_from_settings as c; print(c(g())._post('/v1/customer/disputes/PP-R-XXXX-XXXXXXXX/require-evidence', {'action': 'SELLER_EVIDENCE'}))"
   ```

**Don't:**

- Click `accept_claim` or `make_offer` anywhere. Both refund the buyer and end the dispute.
- Run `spikes\spike3_disputes.py evidence` on it. That uses up the one filing.
- Press **File with PayPal** while rehearsing. Syncing and opening the evidence are safe to repeat.

## On camera: the dispute

1. Log in, open **Disputes**.
2. **Check PayPal for disputes**. Expect "found 1 dispute (1 new)" for the new case.
3. Open it: reason, case ID, order number, "Waiting on you" and the days left.
4. Walk through the four parts (what the buyer asked for, what the agent ordered, the check,
   who released the money), then Claude's summary. It takes a few seconds the first time. Read
   it before filing: it goes to PayPal word for word.
5. **File with PayPal**, confirm. Expect "Filed with PayPal as OTHER on ...".

Afterwards, `show` should list a new `OTHER | SUBMITTED_BY_SELLER` entry with the PDF under
`documents`, and the status `UNDER_REVIEW`.

**Optional ending:** once `show` lists `adjudicate`, this sandbox-only call settles it for the
seller. Sync again and the page shows the closed status.

```
.venv\Scripts\python.exe -c "from backend.app.config import get_settings as g; from backend.app.paypal_client import client_from_settings as c; print(c(g())._post('/v1/customer/disputes/PP-R-XXXX-XXXXXXXX/adjudicate', {'adjudication_outcome': 'SELLER_FAVOR'}))"
```
