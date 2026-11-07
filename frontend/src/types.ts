// Shapes the backend sends back. These mirror the Pydantic models in
// backend/app/orders.py (OrderOut, OrderDetail, MandateSummary, DecisionOut, ApprovalOut)
// and docs/api.md. FastAPI serializes Decimal as a string ("109.00") and datetimes as
// ISO strings, so money and times stay strings here and get formatted at the edge.

// where the money is
export type OrderStatus = 'created' | 'authorized' | 'captured' | 'voided' | 'failed'

// who decided. null until there's a hold to decide about
export type ReviewState = 'needs_review' | 'auto_approved' | 'approved' | 'rejected'

export type Verdict = 'match' | 'mismatch' | 'unsure'

// this used to be 'claude', the backend renamed it
export type CheckedBy = 'rules' | 'ai'

export type ApprovalAction = 'approve' | 'reject'

export type DemoScenario = 'clean' | 'wrong_color' | 'over_cap' | 'wrong_store'

export interface OrderItem {
  name: string
  color: string | null
  size: string | null
  quantity: number
  unit_price: string
}

export interface MandateSummary {
  id: number
  buyer_label: string
  // the human's own words, verbatim
  raw_request: string
  rules: Record<string, unknown>
  max_price: string | null
  // true means "up to", false means "under"
  max_price_inclusive: boolean
  merchant: string | null
  expires_at: string | null
  created_at: string
}

export interface RuleResult {
  rule: string
  passed: boolean
  reason: string
}

export interface Decision {
  id: number
  verdict: Verdict
  // 0 to 100, higher means more likely the human didn't want this
  risk_score: number | null
  // the one-line reason the human sees
  reason: string
  // the next three are null on decisions saved before the check was wired in
  mismatched_fields: string[] | null
  rule_results: RuleResult[] | null
  latency_ms: number | null
  checked_by: CheckedBy
  created_at: string
}

export interface Approval {
  id: number
  // null means the page was opened but they haven't decided yet
  action: ApprovalAction | null
  decided_by: string | null
  opened_at: string | null
  decided_at: string | null
  // server-measured, decided_at minus opened_at. this is the attention number
  decision_ms: number | null
  // what the client claimed. not trusted, don't show it as the real number
  client_decision_ms: number | null
}

export interface Order {
  id: number
  mandate_id: number
  paypal_order_id: string | null
  authorization_id: string | null
  capture_id: string | null
  void_result: string | null
  vault_id: string | null
  items: OrderItem[]
  total: string
  // null on orders from before it was saved
  shipping_amount: string | null
  currency: string
  status: OrderStatus
  review_state: ReviewState | null
  authorization_expires_at: string | null
  created_at: string
  mandate: MandateSummary
  // newest check result. null before the check, or if saving it failed
  intent_check: Decision | null
  // null until the confirm page reports it rendered. auto-approved orders never have one
  approval: Approval | null
}

export interface OrderDetail extends Order {
  // full history, for GET /orders/{id}
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
