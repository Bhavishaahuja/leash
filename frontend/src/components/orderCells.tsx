// Custom AG Grid cell renderers for the orders grid. Each one gets the whole order as
// `data`, so they can read both state fields and the nested check/approval objects.

import type { CustomCellRendererProps } from 'ag-grid-react'
import { buyerName, formatDuration, formatMoney, summarizeItems } from '../lib/format'
import { DECISION_LABELS, decisionKind, decisionMs, displayStatus, isRubberStamp, type HeldRate } from '../lib/orderState'
import type { Order } from '../types'
import { RiskBar } from './RiskBar'
import { StatusChip } from './StatusChip'

type CellProps = CustomCellRendererProps<Order>

export function StatusCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  return <StatusChip status={displayStatus(data)} />
}

export function RiskCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  if (!data.intent_check) {
    return <span className="muted">Not checked</span>
  }
  return <RiskBar score={data.intent_check.risk_score} />
}

const VERDICT_LABELS = {
  match: 'Match',
  mismatch: 'Mismatch',
  unsure: 'Unsure',
}

export function ReasonCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  const check = data.intent_check
  if (!check) {
    // needs_review with no check means saving the result failed, which the human should know
    if (data.review_state === 'needs_review') {
      return <span className="reason-cell reason-cell--missing">No reason saved, check by hand</span>
    }
    return <span className="muted">Not checked yet</span>
  }
  return (
    <span className="reason-cell" title={check.reason}>
      <span className={'verdict verdict--' + check.verdict}>{VERDICT_LABELS[check.verdict]}</span>
      <span className="reason-cell__text">{check.reason}</span>
    </span>
  )
}

export function OrderCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  return (
    <span className="order-cell">
      <span className="order-cell__item">{summarizeItems(data.items)}</span>
      <span className="order-cell__buyer">
        <span className="mono">#{data.id}</span> for {buyerName(data.mandate.buyer_label)}
      </span>
    </span>
  )
}

export function TotalCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  return <span className="num-cell">{formatMoney(data.total, data.currency)}</span>
}

export function DecisionCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  const kind = decisionKind(data)
  return <span className={'decision decision--' + kind}>{DECISION_LABELS[kind]}</span>
}

export function DecideTimeCell({ data }: CellProps) {
  if (!data) {
    return null
  }
  const ms = decisionMs(data)
  if (ms === null) {
    return <span className="muted">None</span>
  }
  if (ms === 0) {
    // the confirm page never reported it opened, so the server couldn't time it
    return <span className="muted">Not timed</span>
  }
  const fast = isRubberStamp(data)
  return (
    <span className={'decide-time' + (fast ? ' decide-time--fast' : '')}>
      <span className="num-cell">{formatDuration(ms)}</span>
      {fast && (
        <span className="stamp-flag" title="Approved in under 2 seconds. Did they really look?">
          Fast
        </span>
      )}
    </span>
  )
}

// group rows only: what share of this agent's orders Leash held for a human. leaf rows stay blank,
// the Status and Decision columns already say it per order
export function HeldRateCell({ value, node }: CustomCellRendererProps<Order, HeldRate | number>) {
  if (!node.group || !value || typeof value !== 'object' || value.total === 0) {
    return null
  }
  const percent = Math.round((value.held / value.total) * 100)
  return (
    <span className="held-rate">
      <span className="held-rate__percent">{percent}% held</span>
      <span className="held-rate__count">
        {value.held} of {value.total} orders
      </span>
    </span>
  )
}
