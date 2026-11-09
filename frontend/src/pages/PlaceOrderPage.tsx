import { useState } from 'react'
import { ApiError, listMandates, placeDemoOrder } from '../api'
import { ReviewLinkBox } from '../components/ReviewLinkBox'
import { StatusChip, type DisplayStatus } from '../components/StatusChip'
import { SCENARIOS, scenarioBlocker } from '../lib/demoScenarios'
import { SEED_LABEL_PREFIX, describeItem, formatMoney, formatTime } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import type { DemoScenario, MandateSummary, PlacedOrder } from '../types'

// stable loader identity so useAsync fetches once
const loadMandates = () => listMandates()

function isExpiredSession(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

// Demo stand-in for the shopping agent: pick a mandate, pick how the order should go wrong, and it
// runs through the same hold, check and auto-decide as a real agent order.
//
// The login gate lives in MerchantLayout now, so this page can assume a session. If one runs out
// mid-page, api.ts tells the layout and it swaps this page for the login form.
export function PlaceOrderPage() {
  return (
    <section className="place-order">
      <div className="page__head">
        <div>
          <h1 className="page__title">Place a demo order</h1>
          <p className="page__lede">Stands in for the shopping agent. Every order is a real sandbox hold.</p>
        </div>
      </div>

      <PlaceOrderForm />
    </section>
  )
}

function PlaceOrderForm() {
  const mandates = useAsync(loadMandates)

  if (mandates.status === 'loading') {
    return (
      <div className="card">
        <p className="state">Loading mandates...</p>
      </div>
    )
  }
  if (mandates.status === 'error') {
    return (
      <div className="card">
        <p className="state state--error">Couldn't load mandates: {mandates.error.message}</p>
      </div>
    )
  }
  if (mandates.data.length === 0) {
    return (
      <div className="card">
        <p className="state">No mandates yet. Run the seed script or create one first.</p>
      </div>
    )
  }

  return <ScenarioPicker mandates={mandates.data} />
}

interface ScenarioPickerProps {
  mandates: MandateSummary[]
}

// seed_demo.py's trick buyers (injection in the name and so on) are labeled "seed-demo:sneaky: ...".
// the list is newest first and the seed makes those last, so without this a fresh demo opens on
// the injection test buyer
function defaultMandateId(mandates: MandateSummary[]): number {
  for (const mandate of mandates) {
    if (!mandate.buyer_label.startsWith(SEED_LABEL_PREFIX + 'sneaky:')) {
      return mandate.id
    }
  }
  return mandates[0].id
}

function ScenarioPicker({ mandates }: ScenarioPickerProps) {
  const [mandateId, setMandateId] = useState(() => defaultMandateId(mandates))
  const [placing, setPlacing] = useState<DemoScenario | null>(null)
  const [result, setResult] = useState<PlacedOrder | null>(null)
  const [error, setError] = useState<string | null>(null)

  const mandate = mandates.find((row) => row.id === mandateId) ?? mandates[0]

  function chooseMandate(nextId: number) {
    setMandateId(nextId)
    // the old result was for a different mandate, it shouldn't look like it belongs to this one
    setResult(null)
    setError(null)
  }

  async function place(scenario: DemoScenario) {
    setPlacing(scenario)
    setResult(null)
    setError(null)
    try {
      setResult(await placeDemoOrder(mandate.id, scenario))
    } catch (caught) {
      if (isExpiredSession(caught)) {
        // the layout is already swapping to the login form, no point flashing an error first
        return
      }
      setError(caught instanceof Error ? caught.message : 'The order failed')
    } finally {
      setPlacing(null)
    }
  }

  return (
    <div className="place-order__stack">
      <div className="card place-order__panel">
        <label className="field-label" htmlFor="demo-mandate">
          Mandate
        </label>
        <select
          id="demo-mandate"
          className="field"
          value={mandate.id}
          disabled={placing !== null}
          onChange={(event) => chooseMandate(Number(event.target.value))}
        >
          {mandates.map((row) => (
            <option key={row.id} value={row.id}>
              #{row.id}: {row.raw_request}
            </option>
          ))}
        </select>
        <p className="place-order__rules">{summarizeMandate(mandate)}</p>
      </div>

      <div className="scenario-grid">
        {SCENARIOS.map((scenario) => {
          const blocker = scenarioBlocker(mandate, scenario.id)
          return (
            <button
              key={scenario.id}
              type="button"
              className="scenario-button"
              disabled={placing !== null || blocker !== null}
              onClick={() => place(scenario.id)}
            >
              <span className="scenario-button__title">{scenario.title}</span>
              <span className="scenario-button__note">
                {placing === scenario.id ? 'Placing...' : (blocker ?? scenario.description)}
              </span>
            </button>
          )
        })}
      </div>

      <div aria-live="polite">
        {placing && (
          <div className="card">
            <p className="state">Placing the hold and checking the order. This can take around 30 seconds.</p>
          </div>
        )}
        {error && (
          <div className="card">
            <p className="state state--error">{error}</p>
          </div>
        )}
        {result && <ResultCard order={result} />}
      </div>
    </div>
  )
}

function summarizeMandate(mandate: MandateSummary): string {
  const parts: string[] = []
  // the cap can be in another currency, see docs/api.md
  const currency = typeof mandate.rules.currency === 'string' ? mandate.rules.currency : 'USD'
  parts.push(mandate.max_price === null ? 'No cap' : 'Cap ' + formatMoney(mandate.max_price, currency))
  parts.push(mandate.merchant ?? 'Any store')
  parts.push(mandate.expires_at === null ? 'No expiry' : 'Expires ' + formatTime(mandate.expires_at))
  return parts.join(' · ')
}

function displayStatus(order: PlacedOrder): DisplayStatus {
  // "needs review" lives in review_state. a declined capture is failed + needs_review, and that
  // one's over, so it has to read as failed
  if (order.status === 'authorized' && order.review_state === 'needs_review') {
    return 'needs_review'
  }
  return order.status
}

function ResultCard({ order }: { order: PlacedOrder }) {
  const status = displayStatus(order)
  const check = order.intent_check

  return (
    <div className="card place-order__panel result-card">
      <div className="result-card__head">
        <span className="mono">Order #{order.id}</span>
        <StatusChip status={status} />
      </div>

      {/* colored by verdict like the side panel, so a clean match doesn't read as a warning */}
      <p className={'result-card__reason ' + (check ? 'verdict-text--' + check.verdict : 'reason')}>
        {check ? check.reason : 'No check result on this order.'}
      </p>

      {check && (
        <dl className="result-card__facts">
          <div>
            <dt>Verdict</dt>
            <dd className="result-card__verdict">{check.verdict}</dd>
          </div>
          <div>
            <dt>Risk score</dt>
            <dd>{check.risk_score ?? 'None'}</dd>
          </div>
          <div>
            <dt>Total</dt>
            <dd>{formatMoney(order.total, order.currency)}</dd>
          </div>
        </dl>
      )}

      <ul className="result-card__items">
        {order.items.map((item, index) => (
          <li key={index}>{describeItem(item)}</li>
        ))}
      </ul>
      {order.merchant && <p className="muted">At {order.merchant}</p>}

      {status === 'needs_review' && (
        <ReviewLinkBox key={order.id} orderId={order.id} />
      )}
    </div>
  )
}
