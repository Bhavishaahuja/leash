// Shapes the backend sends back. These mirror the Pydantic models in
// backend/app/orders.py (OrderOut, OrderDetail, MandateSummary, DecisionOut, ApprovalOut).
// FastAPI serializes Decimal as a string ("109.00") and datetimes as ISO strings,
// so money and times stay strings here and get formatted at the edge.

export type OrderStatus = 'created' | 'authorized' | 'captured' | 'voided' | 'failed'

export type Verdict = 'match' | 'mismatch' | 'unsure'

export type CheckedBy = 'rules' | 'ai'

export type ApprovalAction = 'approve' | 'reject'

// who decided. status is where the money is, this is a separate field, see docs/api.md
export type ReviewState = 'needs_review' | 'auto_approved' | 'approved' | 'rejected'

export type DemoScenario = 'clean' | 'wrong_color' | 'over_cap' | 'wrong_store'

export interface OrderItem {
  name: string
  color: string | null
  size: string | null
  quantity: number
  unit_price: string
}

export interface Order {
  id: number
  mandate_id: number
  paypal_order_id: string | null
  authorization_id: string | null
  capture_id: string | null
  vault_id: string | null
  items: OrderItem[]
  total: string
  currency: string
  status: OrderStatus
  authorization_expires_at: string | null
  created_at: string
}

export interface MandateSummary {
  id: number
  buyer_label: string
  // the human's own words, verbatim
  raw_request: string
  rules: Record<string, unknown>
  max_price: string | null
  merchant: string | null
  expires_at: string | null
  created_at: string
}

export interface Decision {
  id: number
  verdict: Verdict
  // 0 to 100, higher means more likely the human didn't want this
  risk_score: number | null
  // the one-line reason the human sees
  reason: string
  checked_by: CheckedBy
  created_at: string
}

export interface Approval {
  id: number
  action: ApprovalAction | null
  decided_by: string | null
  opened_at: string | null
  decided_at: string | null
  decision_ms: number | null
}

export interface OrderDetail extends Order {
  mandate: MandateSummary
  decisions: Decision[]
  approvals: Approval[]
}

// what POST /demo/orders sends back: the detail shape plus the fields the result card reads
export interface PlacedOrder extends OrderDetail {
  // null means Fernwood Supply Co
  merchant: string | null
  review_state: ReviewState | null
  // null if the order never got a hold, or saving the check failed
  intent_check: Decision | null
}
