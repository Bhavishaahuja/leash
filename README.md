# Leash

AI shopping agents are starting to place real orders, and merchants have no way to know whether the human behind the agent actually wanted that order. Leash is proof of intent for agent orders. The merchant authorizes the PayPal payment but holds the capture while Claude checks the order against the human's mandate. Clean matches capture automatically. Anything off goes to the human with a one-line reason: approve captures, reject voids. If a dispute comes in later, the mandate and the approval are filed as evidence through PayPal's Disputes API. A rubber-stamp watch notices when the human stops really reading approvals and starts asking less often but more pointedly.

**Status: in progress.** Built for the PayPal AI Hackathon.

**Live API:** https://leash-api-q3rw.onrender.com (sandbox). Health check at [`/health`](https://leash-api-q3rw.onrender.com/health), interactive docs at [`/docs`](https://leash-api-q3rw.onrender.com/docs). It's a free instance, so the first request after 15 idle minutes takes about a minute.

## Local setup (Windows PowerShell)

```powershell
git clone https://github.com/Bhavishaahuja/leash.git
cd leash

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install -r requirements-dev.txt

Copy-Item .env.example .env
# now open .env and fill in your sandbox keys

python check_setup.py
```

### Database

Easiest on Windows: a free Render Postgres, nothing to install. (It expires after 30 days, fine for dev. Both of us can share one.)

1. dashboard.render.com > **New** > **Postgres**.
2. Name it `leash-db`, region **Oregon** (the API runs there too), plan **Free**, then **Create Database**. The free plan allows one database, so we share it.
3. When it's ready, copy the **External Database URL** into `DATABASE_URL` in your `.env`. Paste it as is, the app switches `postgres://` to the psycopg driver.
4. Create the tables:

   ```powershell
   alembic upgrade head
   ```

After changing `backend/app/models.py`, make a migration with `alembic revision --autogenerate -m "what changed"`, read it over, then `alembic upgrade head`.

### Run the API

```powershell
uvicorn backend.app.main:app --reload
```

Then open http://127.0.0.1:8000/health (should say `{"status": "ok"}`) or http://127.0.0.1:8000/docs.

### Check mandate parsing

`POST /mandates` uses Claude to turn the shopper's words into rules. The prompt lives in `backend/app/prompts/mandate.py`. After changing it, run the sample requests (6 real Claude calls, needs `ANTHROPIC_API_KEY`):

```powershell
python backend/scripts/try_mandates.py                              # calls Claude directly, no DB needed
python backend/scripts/try_mandates.py --api http://127.0.0.1:8000  # through the running API
```

Run the checks CI runs:

```powershell
ruff check .
pytest
```

## Deploy (Render)

`render.yaml` is a Render Blueprint for the API: a free Python web service in Oregon that deploys on every push to `main`. The database (`leash-db`) already exists and isn't in the Blueprint; its **Internal Database URL** goes in the service's `DATABASE_URL`.

- Build: `pip install -r requirements.txt` (Python version from `.python-version`)
- Start: `alembic upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT`. Migrations run at start because pre-deploy commands are paid-plan only.
- Secrets (`DATABASE_URL`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ANTHROPIC_API_KEY`) are set in the Render dashboard, never in the file.
- After each merge to `main`, CI waits for `/health` on Render (the repo's `RENDER_URL` secret is set).
- Live at https://leash-api-q3rw.onrender.com. The week 1 end-to-end script passed against it on 2026-10-22: mandate parsed, vault order authorized with no buyer action and captured, second order voided, PayPal confirmed `CAPTURED` / `COMPLETED` / `VOIDED`.

## Architecture

TODO

## Security model

This is a hackathon demo in the PayPal sandbox. The security is real but deliberately small: there are no user accounts.

| Who | Holds | Can do |
|---|---|---|
| Shopping agent | Its own key from `AGENT_API_KEYS` | Create mandates, create and authorize orders, read an order's status. Nothing else |
| Merchant dashboard | An 8 hour session token, from logging in with `DEMO_MERCHANT_PASSWORD` | Read orders and mandates, create review links, place the fixed demo orders |
| Merchant scripts | `MERCHANT_API_KEY` | The same as the dashboard |
| Human reviewing an order | One review link | See that one order, approve or reject it once |

What this protects:

- **An agent can't pass as another agent.** Every order records which agent placed it, and the API takes that from the agent's key. A name in the request body is ignored.
- **An agent can't approve its own order.** It can't read a review link or create one, and approving takes a link.
- **Review links are single-use and stored hashed.** Only the SHA-256 of a link's token is saved, so a leaked database can't approve anything. A link dies when its PayPal authorization expires.
- **No API key in the browser.** The dashboard logs in with a password and holds a session token, so the merchant key stays on the server.
- **Review tokens stay out of our logs.** The access log rewrites `/review/<token>` to `/review/[redacted]`.
- **An agent can't learn how to beat the check.** It only ever gets an order's `id`, `status`, `review_state` and `total` back, never the reason it was flagged.
- **Attention is measured on the server.** `decision_ms` runs from when the confirm page says it rendered to the tap, on the server's clock. A link preview fetching the page doesn't start it. The client's own number is stored separately and not trusted.
- **Money can't move twice.** A row lock plus a payment claim stops double taps, and capture and void use a derived `PayPal-Request-Id`.
- **Missing config fails closed.** An unset API key means a 401, and any failure in the check sends the order to a human.

What it doesn't protect, and what a production version would change:

- **Real merchant login.** A production version would use real merchant accounts with per-user sessions. See Known limits for how far the demo login goes.
- **Key rotation.** Rotating a key here means changing an environment variable and redeploying.
- **Who holds a review link.** Anyone with the link can decide that order, so a production version would tie it to the signed-in human.
- **Tokens in URLs.** A review token is in the path, so it can still land in browser history and in any proxy's own request logs, which our filter can't reach. A production version would exchange it for a session on first open.

## Known limits

- **A create timeout can double an order.** If `POST /orders` times out talking to PayPal, the order is marked `failed`, but PayPal may have placed the hold anyway. An agent that retries creates a second order and a second hold. The fix would be an `Idempotency-Key` header on `POST /orders`.
- **Session auth is demo-grade.** There's one shared password and no user accounts. Sessions can't be revoked one at a time: they run out after 8 hours, or changing `MERCHANT_API_KEY` ends all of them. The login limit is 10 tries per minute per address, held in memory, so it resets on restart and isn't shared between processes.
- **The attention clock trusts the page.** The confirm page reports when it rendered. A script holding a link could report that, wait, and then approve, which looks like attention. It can stop a preview bot or a lying number, not a determined link holder.

## Sandbox logins

TODO

## Demo

TODO: demo video and walkthrough.

### Seed demo data

`backend/scripts/seed_demo.py` fills a running API with real sandbox orders through its own endpoints: 5 clean ones that should auto-capture, and 6 sneaky ones that should stop for a human (wrong color, over the price cap, wrong store, expired mandate, odd quantity, and a correct item with injection text in its name). It prints what happened to each and the review links, and exits non-zero if any order landed somewhere unexpected.

Running it again doesn't make duplicates. Run these from the repo root, in cmd:

```
.venv\Scripts\python.exe backend\scripts\seed_demo.py
.venv\Scripts\python.exe backend\scripts\seed_demo.py --base-url https://leash-api-q3rw.onrender.com
.venv\Scripts\python.exe backend\scripts\seed_demo.py --reset
```

`--reset` voids any holds still open at PayPal, then deletes the seeded rows. It needs `AGENT_API_KEY` and `MERCHANT_API_KEY` in `.env` matching the API's, and `spikes/.vault_token.json`.

### Week 1 end-to-end check

`backend/scripts/week1_e2e.py` runs the whole money flow against a running API: creates a mandate (Claude parses it), places an order with the saved PayPal vault id (authorized with no buyer action) that should auto-capture, places a second one that should stop for review, checks the agent key can't approve it, rejects it through a review link, then reads both back from PayPal (authorization `CAPTURED` + capture `COMPLETED`, and authorization `VOIDED`).

It needs `spikes/.vault_token.json` (run `python spikes/spike2_vault.py` once) and the PayPal keys in your local `.env` for the read-back, plus `AGENT_API_KEY` and `MERCHANT_API_KEY` matching the API's.

```powershell
python backend/scripts/week1_e2e.py                                      # local API on :8000
python backend/scripts/week1_e2e.py https://leash-api-q3rw.onrender.com  # Render
```

It's sandbox, so no real money moves. Each run costs one small Claude call.

## Tools used

TODO

## License

MIT, see [LICENSE](LICENSE).
