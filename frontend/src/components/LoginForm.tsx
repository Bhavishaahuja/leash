// Merchant password login. Moved here from PlaceOrderPage so MerchantLayout can put it in front
// of every merchant page. The password goes to POST /auth/session and is never kept, only the
// session token is (see api.ts).

import { useState, type FormEvent } from 'react'
import { logIn } from '../api'

interface LoginFormProps {
  onLoggedIn: () => void
  // set when we got here because a session ran out, not on a first visit
  expired?: boolean
}

export function LoginForm({ onLoggedIn, expired = false }: LoginFormProps) {
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
    <div className="login">
      <h1 className="page__title">Log in</h1>
      <p className="page__lede">
        {expired
          ? 'Your session ran out. Log in again to keep going.'
          : 'Fernwood Supply Co staff only. Agent orders and review links live behind this.'}
      </p>
      <form className="card place-order__panel login__form" onSubmit={submit}>
        <label className="field-label" htmlFor="demo-password">
          Merchant password
        </label>
        <input
          id="demo-password"
          className="field"
          type="password"
          autoComplete="current-password"
          required
          autoFocus
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
    </div>
  )
}
