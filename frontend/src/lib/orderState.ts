// Turns the two backend state fields (status: where's the money, review_state: who decided)
// into what the dashboard shows. See the combinations table in docs/api.md.

import type { DisplayStatus } from '../components/StatusChip'
import type { Order } from '../types'

// approvals faster than this look like a rubber stamp, not a real look at the order
export const RUBBER_STAMP_MS = 2000

export function displayStatus(order: Order): DisplayStatus {
  if (order.status === 'authorized' && order.review_state === 'needs_review') {
    return 'needs_review'
  }
  return order.status
}

export type DecisionKind = 'auto' | 'approved' | 'rejected' | 'opened' | 'waiting' | 'none'

export function decisionKind(order: Order): DecisionKind {
  if (order.review_state === 'auto_approved') {
    return 'auto'
  }
  if (order.review_state === 'approved') {
    return 'approved'
  }
  if (order.review_state === 'rejected') {
    return 'rejected'
  }
  if (order.review_state === 'needs_review') {
    // an approval row with no action yet means the human has the page open
    if (order.approval && order.approval.action === null) {
      return 'opened'
    }
    return 'waiting'
  }
  return 'none'
}

export const DECISION_LABELS: Record<DecisionKind, string> = {
  auto: 'Auto-captured',
  approved: 'Approved',
  rejected: 'Rejected',
  opened: 'Opened, deciding',
  waiting: 'Waiting on human',
  none: 'Not needed',
}

// the server's number, only once they've actually decided. 0 means the page never reported
// it opened, so there's nothing honest to show
export function decisionMs(order: Order): number | null {
  const approval = order.approval
  if (!approval || approval.action === null || approval.decision_ms === null) {
    return null
  }
  return approval.decision_ms
}

export function isRubberStamp(order: Order): boolean {
  const ms = decisionMs(order)
  return order.review_state === 'approved' && ms !== null && ms > 0 && ms < RUBBER_STAMP_MS
}

// money in the mandate can be in another currency, and null there means USD
export function mandateCurrency(order: Order): string {
  const currency = order.mandate.rules.currency
  return typeof currency === 'string' && currency ? currency : 'USD'
}

// "held" means Leash didn't let the order through on its own: it went to a human, whatever they
// decided. per agent, that share is the trust score: an agent that gets held a lot buys the wrong thing
export function wasHeld(order: Order): boolean {
  return order.review_state === 'needs_review' || order.review_state === 'approved' || order.review_state === 'rejected'
}

export interface HeldRate {
  held: number
  total: number
  // what AG Grid sorts by, and what a plain text export shows
  toString(): string
}

// AG Grid aggFunc for the "Held by Leash" column. each leaf value is 1 (held) or 0
export function heldRateAgg(params: { values: unknown[] }): HeldRate {
  let held = 0
  let total = 0
  for (const value of params.values) {
    // a nested group passes up its own HeldRate, a leaf passes 1 or 0
    if (value && typeof value === 'object' && 'held' in value) {
      const inner = value as HeldRate
      held += inner.held
      total += inner.total
    } else if (typeof value === 'number') {
      held += value
      total += 1
    }
  }
  return {
    held,
    total,
    toString: () => (total === 0 ? '' : Math.round((held / total) * 100) + '%'),
  }
}
