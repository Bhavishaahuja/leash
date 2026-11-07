# Leash

Proof of intent for AI agent orders, built for merchants. PayPal AI Hackathon entry (Devpost, deadline Nov 12 2026).

## What it does

An AI shopping agent places an order. The merchant AUTHORIZES the PayPal payment but does not capture it yet. Claude checks the order against the human's mandate:

- Clean match: auto-capture.
- Anything off: goes to the human with a one-line reason. Approve captures, reject voids.
- Disputes: the mandate plus the approval record get filed as evidence through PayPal's Disputes API.
- Rubber-stamp watch: times approvals, and when the human stops really looking, it asks less often but more pointedly.

## Stack

- Python 3.11 + FastAPI
- Postgres (SQLAlchemy 2 + Alembic)
- React (Vite) + AG Grid dashboard in `/frontend`
- Claude API, model `claude-sonnet-5-5`
- Hosted on Render
- PayPal sandbox only: Orders v2 (intent AUTHORIZE), Payments v2 capture/void, Vault payment tokens, Agent Toolkit, Disputes API, webhooks

Team: Bhavi (backend) and Shachi (frontend). Both on Windows.

## Folder layout

```
backend/app/      FastAPI app
backend/tests/    pytest tests
spikes/           throwaway proof scripts (spike1_authorize.py etc.)
frontend/         Vite + React app, Shachi owns it
docs/             design notes, diagrams, demo script
check_setup.py    sanity check that PayPal + Anthropic keys work
```

## Coding rules

- Python 3 for everything except the frontend.
- Natural variable names (`order_total`, not `ot`). Casual inline comments that explain the why.
- Explicit for-loops over clever one-liners or heavy comprehensions.
- Never guess PayPal endpoint shapes. Before writing integration code, check developer.paypal.com. The OpenAPI schemas at `developer.paypal.com/api/<api>/<version>/schema.yaml` are the source of truth.
- Every PayPal POST sends a fresh `PayPal-Request-Id` (`str(uuid.uuid4())`). Authorize, capture and void are the exception: theirs are derived (`authorize_request_id` and `payment_request_id` in `orders.py`) so a retry can't move money twice.
- Never commit `.env` or any secret. Config comes from environment variables (see `.env.example`).
- The demo store is fictional: "Fernwood Supply Co". No real brands anywhere.
- No em dashes in any docs or UI copy. Use a colon, comma, or period instead.

## Git workflow

1. One branch per issue, named by type: `feat/`, `fix/`, `spike/`, `chore/`, `docs/` (e.g. `feat/12-mandate-endpoint`).
2. Conventional commit messages: `feat: add mandate endpoint`, `fix: void on reject`, `chore: bump deps`.
3. Open a PR whose description includes `Closes #N`.
4. CI must pass (ruff + pytest, plus the frontend build once it exists).
5. PR + green CI required, self-merge allowed. A teammate review is welcome but not required.
6. Squash and merge. Delete the branch.
