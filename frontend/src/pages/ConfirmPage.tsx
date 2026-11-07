import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { ApiError, approveReview, getReview, markReviewOpened, rejectReview } from '../api'
import { LeashMark } from '../components/LeashMark'
import { describeItem, formatDuration, formatMoney } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import type { ApprovalAction, ReviewView } from '../types'

// The human's mobile page, opened from a one-time review link on their phone. Shows what the
// agent ordered next to what they asked for, and two big buttons: approve captures the money,
// reject releases the hold. Designed at 390px wide.
export function ConfirmPage() {
  const { token } = useParams()

  if (!token) {
    return (
      <ConfirmFrame>
        <Notice tone="error" title="This link is incomplete">
          Open the link the store sent you again, all the way to the end.
        </Notice>
      </ConfirmFrame>
    )
  }

  // key resets everything if the token in the URL changes
  return <ConfirmReview key={token} token={token} />
}

function ConfirmReview({ token }: { token: string }) {
  const load = useCallback(() => getReview(token), [token])
  const review = useAsync(load)

  if (review.status === 'loading') {
    return (
      <ConfirmFrame>
        <p className="state">Loading your order...</p>
      </ConfirmFrame>
    )
  }

  if (review.status === 'error') {
    return (
      <ConfirmFrame>
        <LoadError error={review.error} />
      </ConfirmFrame>
    )
  }

  return <Decide token={token} order={review.data} />
}

type Phase =
  | { kind: 'deciding' }
  | { kind: 'sending'; action: ApprovalAction }
  | { kind: 'done'; action: ApprovalAction; order: ReviewView; decisionMs: number }
  | { kind: 'failed'; action: ApprovalAction; title: string; message: string; canRetry: boolean }

function Decide({ token, order }: { token: string; order: ReviewView }) {
  const [phase, setPhase] = useState<Phase>({ kind: 'deciding' })
  // when the order was really on screen. the server keeps its own clock from the /opened call,
  // this one is only what we send along and show back to the human
  const shownAt = useRef<number | null>(null)
  const openedSent = useRef(false)

  useEffect(() => {
    shownAt.current = performance.now()
    // start the server's attention clock now that the order is rendered. the ref stops React's
    // dev double-run from sending it twice (the server ignores repeats anyway). a failure here
    // must not block the buttons: the decision still works, it just records 0
    if (!openedSent.current) {
      openedSent.current = true
      markReviewOpened(token).catch(() => {})
    }
  }, [token])

  async function decide(action: ApprovalAction) {
    // the buttons are disabled while sending, this is a second guard against a fast double tap
    if (phase.kind === 'sending') {
      return
    }
    const decisionMs = shownAt.current === null ? 0 : performance.now() - shownAt.current
    setPhase({ kind: 'sending', action })
    try {
      const send = action === 'approve' ? approveReview : rejectReview
      const updated = await send(token, decisionMs)
      setPhase({ kind: 'done', action, order: updated, decisionMs })
    } catch (error) {
      setPhase({ kind: 'failed', action, ...describeDecideError(error) })
    }
  }

  if (phase.kind === 'done') {
    return (
      <ConfirmFrame>
        <Done action={phase.action} order={phase.order} decisionMs={phase.decisionMs} />
      </ConfirmFrame>
    )
  }

  const sending = phase.kind === 'sending' ? phase.action : null
  const total = formatMoney(order.total, order.currency)
  const check = order.intent_check

  return (
    <ConfirmFrame
      footer={
        <div className="decide-bar">
          {phase.kind === 'failed' && (
            <div className="decide-bar__error" role="alert">
              <strong>{phase.title}</strong> {phase.message}
            </div>
          )}
          {!(phase.kind === 'failed' && !phase.canRetry) && (
            <div className="decide-bar__buttons">
              <button
                type="button"
                className="decide-button decide-button--reject"
                onClick={() => decide('reject')}
                disabled={sending !== null}
              >
                {sending === 'reject' ? 'Rejecting...' : 'Reject'}
              </button>
              <button
                type="button"
                className="decide-button decide-button--approve"
                onClick={() => decide('approve')}
                disabled={sending !== null}
              >
                {sending === 'approve' ? 'Approving...' : 'Approve ' + total}
              </button>
            </div>
          )}
        </div>
      }
    >
      <p className="eyebrow">Your agent wants to buy</p>
      <ul className="item-list">
        {order.items.map((item, index) => (
          <li key={index}>
            {item.quantity > 1 && <span className="mono">{item.quantity} x </span>}
            {describeItem(item)}
          </li>
        ))}
      </ul>
      <p className="confirm__total">{total}</p>
      {order.shipping_amount !== null && Number(order.shipping_amount) > 0 && (
        <p className="confirm__shipping">Includes {formatMoney(order.shipping_amount, order.currency)} shipping</p>
      )}

      <p className="eyebrow">You asked for</p>
      <blockquote className="quote">{order.mandate.raw_request}</blockquote>

      <div className={'why why--' + (check ? check.verdict : 'unknown')}>
        <p className="why__label">Why we're asking you</p>
        <p className="why__reason">
          {check
            ? check.reason
            : "We couldn't save why this order was flagged. Compare the two above before you decide."}
        </p>
      </div>

      <p className="confirm__hint">
        Approve pays for the order. Reject cancels it and you won't be charged.
      </p>
    </ConfirmFrame>
  )
}

function Done({ action, order, decisionMs }: { action: ApprovalAction; order: ReviewView; decisionMs: number }) {
  const approved = action === 'approve'
  return (
    <div className="done" role="status">
      <span className={'done__icon done__icon--' + (approved ? 'approved' : 'rejected')} aria-hidden="true">
        {approved ? (
          <svg width="28" height="28" viewBox="0 0 28 28">
            <path d="M7 14.5l4.5 4.5L21 9.5" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <svg width="28" height="28" viewBox="0 0 28 28">
            <path d="M9 9l10 10M19 9L9 19" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
          </svg>
        )}
      </span>
      <h1 className="done__title">{approved ? 'Approved' : 'Rejected'}</h1>
      <p className="done__text">
        {approved
          ? 'You paid ' + formatMoney(order.total, order.currency) + '. Your order is on its way.'
          : "The order is cancelled and the hold is released. You won't be charged."}
      </p>
      <p className="done__time">You decided in {formatDuration(Math.round(decisionMs))}.</p>
      <p className="muted">You can close this page.</p>
    </div>
  )
}

function readDetail(error: ApiError): { message: string | null; status: string | null } {
  // review errors carry {"detail": {"message": ..., "status": ..., "review_state": ...}}
  const detail = error.detail
  if (detail && typeof detail === 'object') {
    const fields = detail as { message?: unknown; status?: unknown }
    return {
      message: typeof fields.message === 'string' ? fields.message : null,
      status: typeof fields.status === 'string' ? fields.status : null,
    }
  }
  return { message: typeof detail === 'string' ? detail : null, status: null }
}

function describeDecideError(error: unknown): { title: string; message: string; canRetry: boolean } {
  if (!(error instanceof ApiError)) {
    // fetch itself failed: no signal, or the server is waking up. nothing changed, so it's safe to go again
    return { title: "Couldn't reach the store.", message: 'Check your connection and try again.', canRetry: true }
  }

  const { message, status } = readDetail(error)
  if (error.status === 404) {
    return {
      title: "This link isn't valid anymore.",
      message: 'The store may have sent you a newer one.',
      canRetry: false,
    }
  }
  if (error.status === 410) {
    return { title: 'This link has expired.', message: 'Ask the store for a new one.', canRetry: false }
  }
  if (error.status === 409) {
    return { title: 'Already decided.', message: message ?? 'This order was already approved or rejected.', canRetry: false }
  }
  if (error.status === 502) {
    if (status === 'failed') {
      // PayPal declined the capture. going again just gets a 409
      return { title: 'PayPal declined the payment.', message: "You weren't charged.", canRetry: false }
    }
    if (status !== null) {
      // PayPal failed and the order is unchanged, the same link is safe to try again
      return { title: 'PayPal had a problem.', message: (message ?? 'Nothing changed.') + ' Try again.', canRetry: true }
    }
    // PayPal went through but saving failed. the link is used up, the store has to send a new one
    return {
      title: 'Almost done.',
      message: (message ?? 'Your choice went through but we could not save it.') + " Don't tap again.",
      canRetry: false,
    }
  }
  // a 422 means we sent a bad decision_ms, which is our bug, not theirs
  return { title: 'Something went wrong.', message: message ?? 'Try again in a moment.', canRetry: true }
}

function LoadError({ error }: { error: Error }) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <Notice tone="error" title="This link isn't valid">
        The store may have sent you a newer link. Check your messages for the latest one.
      </Notice>
    )
  }
  if (error instanceof ApiError && error.status === 410) {
    return (
      <Notice tone="muted" title="Nothing to decide here">
        This link has expired or was already used, so the order is no longer waiting on you.
      </Notice>
    )
  }
  return (
    <Notice tone="error" title="Couldn't load your order">
      {error instanceof ApiError ? error.message : 'Check your connection and reload the page.'}
    </Notice>
  )
}

function Notice({ tone, title, children }: { tone: 'error' | 'muted'; title: string; children: ReactNode }) {
  return (
    <div className={'notice notice--' + tone} role={tone === 'error' ? 'alert' : 'status'}>
      <h1 className="notice__title">{title}</h1>
      <p>{children}</p>
    </div>
  )
}

function ConfirmFrame({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="confirm">
      <header className="confirm__head">
        <LeashMark size={20} />
        <span>Fernwood Supply Co</span>
      </header>
      <main className="confirm__body">{children}</main>
      {footer}
    </div>
  )
}
