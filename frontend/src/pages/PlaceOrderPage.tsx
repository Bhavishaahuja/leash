import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { ApiError, hasSession, listMandates, logIn, placeDemoOrder } from '../api'
import { StatusChip, type DisplayStatus } from '../components/StatusChip'
import { SCENARIOS, scenarioBlocker } from '../lib/demoScenarios'
import { describeItem, formatMoney, formatTime } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import type { DemoScenario, MandateSummary, PlacedOrder } from '../types'

// stable loader identity so useAsync fetches once
const loadMandates = () => listMandates()

function isExpiredSession(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

// Demo stand-in for the shopping agent: pick a mandate, pick how the order should go wrong, and it
// runs through the same hold, check and auto-decide as a real agent order.
export function PlaceOrderPage() {
  const [loggedIn, setLoggedIn] = useState(hasSession)

  return (
    <section className="place-order">
      <div className="page__head">
        <div>
          <h1 className="page__title">Place a demo order</h1>
          <p className="page__lede">Stands in for the shopping agent. Every order is a real sandbox hold.</p>
        </div>
      </div>

      {loggedIn ? (
        <PlaceOrderForm onSessionExpired={() => setLoggedIn(false)} />
      ) : (
        <LoginForm onLoggedIn={() => setLoggedIn(true)} />
      )}
    </section>
  )
}

function LoginForm({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await logIn(password)
      onLoggedIn()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Login failed')
      setSubmitting(false)
    }
  }

  return (
    <form className="card place-order__panel" onSubmit={submit}>
      <label className="field-label" htmlFor="demo-password">
        Merchant password
      </label>
      <input
        id="demo-password"
        className="field"
        type="password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <button className="primary-button" type="submit" disabled={submitting}>
        {submitting ? 'Logging in...' : 'Log in'}
      </button>
    </form>
  )
}

function PlaceOrderForm({ onSessionExpired }: { onSessionExpired: () => void }) {
  const mandates = useAsync(loadMandates)

  const loadError = mandates.status === 'error' ? mandates.error : null
  useEffect(() => {
    if (isExpiredSession(loadError)) {
      onSessionExpired()
    }
  }, [loadError, onSessionExpired])

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

  return <ScenarioPicker mandates={mandates.data} onSessionExpired={onSessionExpired} />
}

interface ScenarioPickerProps {
  mandates: MandateSummary[]
  onSessionExpired: () => void
}

function ScenarioPicker({ mandates, onSessionExpired }: ScenarioPickerProps) {
  const [mandateId, setMandateId] = useState(mandates[0].id)
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
        onSessionExpired()
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

      <p className="reason result-card__reason">
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
        <Link to={'/confirm/' + order.id} className="primary-button">
          Open confirm page
        </Link>
      )}
    </div>
  )
}
