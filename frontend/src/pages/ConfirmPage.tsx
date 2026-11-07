import { useCallback, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { getOrder } from '../api'
import { LeashMark } from '../components/LeashMark'
import { describeItem, formatMoney } from '../lib/format'
import { useAsync } from '../lib/useAsync'

// The human's mobile page, opened from a link on their phone. Scaffold version:
// shows the order and the mandate side by side. Approve/Reject and timing come next.
export function ConfirmPage() {
  const { orderId } = useParams()
  const id = Number(orderId)

  if (!Number.isInteger(id) || id <= 0) {
    return (
      <ConfirmFrame>
        <p className="state state--error">That link doesn't point to an order.</p>
      </ConfirmFrame>
    )
  }

  // key resets the loader state if the id in the URL changes
  return <ConfirmOrder key={id} orderId={id} />
}

function ConfirmOrder({ orderId }: { orderId: number }) {
  const load = useCallback(() => getOrder(orderId), [orderId])
  const order = useAsync(load)

  if (order.status === 'loading') {
    return (
      <ConfirmFrame>
        <p className="state">Loading order...</p>
      </ConfirmFrame>
    )
  }

  if (order.status === 'error') {
    return (
      <ConfirmFrame>
        <p className="state state--error">Couldn't load this order: {order.error.message}</p>
      </ConfirmFrame>
    )
  }

  const detail = order.data
  const latestDecision = detail.intent_check

  return (
    <ConfirmFrame>
      <p className="eyebrow">Your agent wants to buy</p>
      <ul className="item-list">
        {detail.items.map((item, index) => (
          <li key={index}>
            {item.quantity > 1 && <span className="mono">{item.quantity} x </span>}
            {describeItem(item)}
          </li>
        ))}
      </ul>
      <p className="confirm__total">{formatMoney(detail.total, detail.currency)}</p>

      <p className="eyebrow">You asked for</p>
      <blockquote className="quote">{detail.mandate.raw_request}</blockquote>

      {latestDecision && (
        <>
          <p className="eyebrow">Why we're checking</p>
          <p className="reason">{latestDecision.reason}</p>
        </>
      )}
    </ConfirmFrame>
  )
}

function ConfirmFrame({ children }: { children: ReactNode }) {
  return (
    <div className="confirm">
      <header className="confirm__head">
        <LeashMark size={20} />
        <span>Fernwood Supply Co</span>
      </header>
      <main className="confirm__body">{children}</main>
    </div>
  )
}
