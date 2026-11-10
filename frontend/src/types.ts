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

// the rubber-stamp watch: sharp while the human has been approving too fast
export type WatchMode = 'normal' | 'sharp'

export type DemoScenario =
  | 'clean'
  | 'wrong_color'
  | 'over_cap'
  | 'wrong_store'
  | 'wrong_size'
  | 'substitution'
  | 'wrong_model'
  | 'wrong_date'
  | 'wrong_quantity'

export interface OrderItem {
  name: string
  color: string | null
  size: string | null
  // optional because orders saved before these existed don't have them at all
  model?: string | null
  // the day of the event, "2026-12-05"
  event_date?: string | null
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

// what the agent should have bought, worked out from the catalog (never by Claude). only on orders
// that went to a human, and only when something in stock fits the whole mandate
export interface SuggestedFix {
  sku: string
  item: string
  // the catalog's price for one unit, a decimal string like "64.00"
  price: string
  quantity: number
  // one line for the human, like "Agent picked size 8.5, mandate says 9. Size 9 is in stock at
  // the same price."
  sentence: string
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
  suggested_fix: SuggestedFix | null
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
  // the store the agent actually ordered from. null means Fernwood Supply Co, only a demo
  // order names another one
  merchant: string | null
  // which shopping agent placed it, worked out by the API from the agent's key. "Demo page" for
  // an order from /place-order, null on orders from before agents had names
  agent_name: string | null
  items: OrderItem[]
  total: string
  // null on orders from before it was saved
  shipping_amount: string | null
  currency: string
  status: OrderStatus
  review_state: ReviewState | null
  authorization_expires_at: string | null
  // sharp means the rubber-stamp watch was on when this order was checked
  watch_mode: WatchMode
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

// POST /orders/{id}/review-link. the only time the token is ever returned
export interface ReviewLink {
  review_token: string
  // also when the link stops working: it's the PayPal hold's expiry
  expires_at: string
}

// GET /review/{token}: all the confirm page gets. no ids, no PayPal fields, no mandate rules
export interface ReviewView {
  id: number
  items: OrderItem[]
  total: string
  shipping_amount: string | null
  currency: string
  status: OrderStatus
  review_state: ReviewState | null
  authorization_expires_at: string | null
  mandate: {
    buyer_label: string
    raw_request: string
  }
  // null if saving the check failed, so the page has to cope with no reason
  intent_check: {
    verdict: Verdict
    risk_score: number | null
    reason: string
    mismatched_fields: string[] | null
    suggested_fix: SuggestedFix | null
  } | null
}

// ---- disputes. mirrors backend/app/disputes.py ----

// GET /disputes. reason and status are PayPal's own strings, kept open since PayPal can add values
export interface Dispute {
  // Leash's id, what the /disputes/{id}/... routes take
  id: number
  // PayPal's case id, e.g. PP-R-ABC-10001234
  paypal_dispute_id: string
  order_id: number
  reason: string | null
  status: string | null
  evidence_submitted: boolean
  evidence_submitted_at: string | null
  // what it was filed as, e.g. OTHER. null until submitted
  evidence_type: string | null
  // a summary is drafted, so it can be filed
  has_summary: boolean
  // as of the last sync. null when PayPal isn't waiting on the merchant
  seller_response_due_at: string | null
  created_at: string
}

// POST /disputes/sync
export interface DisputeSyncResult {
  orders_checked: number
  disputes_found: number
  created: number
  updated: number
  // PayPal failing on one order doesn't stop the rest
  errors: { order_id: number; message: string }[]
}

// the packet: only Leash's own records, the same four parts as the PDF it files
export interface EvidencePacket {
  dispute: { paypal_dispute_id: string; reason: string | null; status: string | null }
  mandate: {
    id: number
    raw_request: string
    rules: Record<string, unknown>
    max_price: string | null
    max_price_inclusive: boolean
    merchant: string | null
    expires_at: string | null
    created_at: string | null
  }
  order: {
    id: number
    store: string
    items: OrderItem[]
    total: string | null
    shipping_amount: string | null
    currency: string
    status: string
    paypal_order_id: string | null
    capture_id: string | null
    created_at: string | null
  }
  intent_check: {
    verdict: Verdict
    risk_score: number | null
    reason: string
    mismatched_fields: string[] | null
    checked_by: CheckedBy
    checked_at: string | null
    watch_mode: WatchMode
  } | null
  decision: {
    // "automatic capture" or "human review". null if nobody decided
    released_by: string | null
    review_state: string | null
    action: string | null
    decision_ms: number | null
    opened_at?: string | null
    decided_at: string | null
  }
}

// GET /disputes/{id}/evidence and POST /disputes/{id}/redraft
export interface DisputeEvidence {
  dispute: Dispute
  packet: EvidencePacket
  // Claude's draft for the PayPal reviewer. exactly what submit sends as the notes
  summary: string
}

// GET /stats: dashboard counts plus where the rubber-stamp watch stands
export interface Stats {
  total_orders: number
  // captured by Leash on its own, no human involved
  auto_captured: number
  // holds waiting on a human right now
  needs_review: number
  approved: number
  // every order whose hold was released
  voided: number
  // over every human decision the server timed. null until there is one
  median_decision_ms: number | null
  // fast approvals in a row, newest first
  fast_streak: number
  rubber_stamp_active: boolean
  // an approval under this counts as fast
  fast_threshold_ms: number
  // the last 10 human decisions, newest first. null on old rows with no timing
  recent_decision_ms: (number | null)[]
}
