// Slide-in panel for one order: the human's mandate next to what the agent actually
// ordered, then the intent check, then the human's decision. Opens on a grid row click.

import { useEffect, useRef } from 'react'
import { compareMandate } from '../lib/compare'
import { describeItem, formatDateTime, formatDuration, formatMoney } from '../lib/format'
import { DECISION_LABELS, RUBBER_STAMP_MS, decisionKind, displayStatus, isRubberStamp } from '../lib/orderState'
import type { Order } from '../types'
import { ReviewLinkBox } from './ReviewLinkBox'
import { RiskBar } from './RiskBar'
import { StatusChip } from './StatusChip'

interface OrderPanelProps {
  order: Order
  onClose: () => void
}

export function OrderPanel({ order, onClose }: OrderPanelProps) {
  const closeButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // move focus into the panel so keyboard users land on it, and Escape closes it
    closeButton.current?.focus()
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [order.id, onClose])

  const check = order.intent_check
  const rows = compareMandate(order)

  return (
    <>
      <div className="panel-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="panel" role="dialog" aria-modal="true" aria-labelledby="panel-title">
        <header className="panel__head">
          <div>
            <p className="panel__kicker">Order #{order.id}</p>
            <h2 id="panel-title" className="panel__title">
              {order.mandate.buyer_label}'s agent order
            </h2>
          </div>
          <button ref={closeButton} type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
              <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </header>

        <div className="panel__body">
          <div className="panel__status">
            <StatusChip status={displayStatus(order)} />
            <span className={'decision decision--' + decisionKind(order)}>{DECISION_LABELS[decisionKind(order)]}</span>
          </div>

          <section className="panel__section">
            <h3 className="eyebrow">They asked for</h3>
            <blockquote className="quote">{order.mandate.raw_request}</blockquote>
          </section>

          <section className="panel__section">
            <h3 className="eyebrow">Mandate vs order</h3>
            <table className="compare">
              <thead>
                <tr>
                  <th scope="col">
                    <span className="sr-only">Field</span>
                  </th>
                  <th scope="col">Mandate</th>
                  <th scope="col">Order</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.label} className={row.flagged ? 'compare__row--flagged' : undefined}>
                    <th scope="row">
                      {row.label}
                      {row.flagged && <span className="sr-only"> (flagged)</span>}
                    </th>
                    <td>{row.wanted}</td>
                    <td>{row.got}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul className="panel__items">
              {order.items.map((item, index) => (
                <li key={index}>
                  <span>
                    {item.quantity > 1 && <span className="mono">{item.quantity} x </span>}
                    {describeItem(item)}
                  </span>
                  <span className="num-cell">{formatMoney(item.unit_price, order.currency)}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel__section">
            <h3 className="eyebrow">Intent check</h3>
            {check ? (
              <>
                <p className={'panel__reason verdict-text--' + check.verdict}>{check.reason}</p>
                <dl className="facts">
                  <dt>Risk</dt>
                  <dd>
                    <RiskBar score={check.risk_score} />
                  </dd>
                  <dt>Checked by</dt>
                  <dd>{check.checked_by === 'rules' ? 'Hard rules' : 'AI check'}</dd>
                  {check.latency_ms !== null && (
                    <>
                      <dt>Took</dt>
                      <dd>{formatDuration(check.latency_ms)}</dd>
                    </>
                  )}
                </dl>
                {check.rule_results && check.rule_results.length > 0 && (
                  <ul className="rules">
                    {check.rule_results.map((result) => (
                      <li key={result.rule} className={result.passed ? 'rules__pass' : 'rules__fail'}>
                        <span className="rules__mark" aria-label={result.passed ? 'Passed' : 'Failed'}>
                          {result.passed ? '✓' : '!'}
                        </span>
                        {result.reason}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="muted">
                {order.review_state === 'needs_review'
                  ? 'The check result was not saved, so there is no reason on file. Compare by hand.'
                  : 'This order has not been checked yet.'}
              </p>
            )}
          </section>

          <DecisionSection order={order} />

          <section className="panel__section">
            <h3 className="eyebrow">PayPal</h3>
            <dl className="facts facts--mono">
              <dt>Order</dt>
              <dd>{order.paypal_order_id ?? 'None'}</dd>
              <dt>Authorization</dt>
              <dd>{order.authorization_id ?? 'None'}</dd>
              {order.capture_id && (
                <>
                  <dt>Capture</dt>
                  <dd>{order.capture_id}</dd>
                </>
              )}
              {order.void_result && (
                <>
                  <dt>Void</dt>
                  <dd>{order.void_result}</dd>
                </>
              )}
              {order.authorization_expires_at && (
                <>
                  <dt>Hold expires</dt>
                  <dd>{formatDateTime(order.authorization_expires_at)}</dd>
                </>
              )}
            </dl>
          </section>
        </div>

        {order.status === 'authorized' && order.review_state === 'needs_review' && (
          <footer className="panel__foot">
            <ReviewLinkBox key={order.id} orderId={order.id} />
          </footer>
        )}
      </aside>
    </>
  )
}

function DecisionSection({ order }: { order: Order }) {
  const approval = order.approval

  if (order.review_state === 'auto_approved') {
    return (
      <section className="panel__section">
        <h3 className="eyebrow">Human decision</h3>
        <p className="muted">Clean match, captured with no human involved.</p>
      </section>
    )
  }

  if (!approval) {
    return (
      <section className="panel__section">
        <h3 className="eyebrow">Human decision</h3>
        <p className="muted">
          {order.review_state === 'needs_review' ? 'The human has not opened the link yet.' : 'No review needed.'}
        </p>
      </section>
    )
  }

  const fast = isRubberStamp(order)
  return (
    <section className="panel__section">
      <h3 className="eyebrow">Human decision</h3>
      <dl className="facts">
        <dt>Opened</dt>
        <dd>{formatDateTime(approval.opened_at)}</dd>
        <dt>Decided</dt>
        <dd>{formatDateTime(approval.decided_at)}</dd>
        {approval.decision_ms !== null && (
          <>
            <dt>Time to decide</dt>
            <dd>{approval.decision_ms === 0 ? 'Not timed' : formatDuration(approval.decision_ms)}</dd>
          </>
        )}
      </dl>
      {fast && (
        <p className="stamp-note">
          Approved in under {RUBBER_STAMP_MS / 1000} seconds. That is faster than reading the reason, so this may
          have been a rubber stamp.
        </p>
      )}
    </section>
  )
}
