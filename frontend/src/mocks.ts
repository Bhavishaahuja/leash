// Fake data for USE_MOCKS mode, shaped exactly like the real API responses (docs/api.md).
// The mix covers every status + review_state combo the dashboard has to handle: clean
// auto-captures, orders waiting on the human, approved and rejected ones (one approved
// suspiciously fast, for the rubber-stamp column), a failed hold, one still waiting on
// the buyer at PayPal, and one whose check result never got saved.

import type {
  Approval,
  ApprovalAction,
  Decision,
  DemoScenario,
  MandateSummary,
  OrderDetail,
  OrderItem,
  OrderStatus,
  PlacedOrder,
  ReviewState,
  RuleResult,
} from './types'

// timestamps are relative to page load so the demo always looks recent
function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
}

function daysFromNow(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString()
}

function mandate(
  id: number,
  buyerLabel: string,
  rawRequest: string,
  maxPrice: string | null,
  rules: Record<string, unknown>,
  createdMinutesAgo: number,
): MandateSummary {
  return {
    id,
    buyer_label: buyerLabel,
    raw_request: rawRequest,
    rules: { currency: 'USD', ...rules },
    max_price: maxPrice,
    max_price_inclusive: true,
    merchant: 'Fernwood',
    expires_at: daysFromNow(3),
    created_at: minutesAgo(createdMinutesAgo),
  }
}

// the four hard rules, all passing unless one is overridden
function hardRules(overrides: Partial<Record<string, string | false>> = {}): RuleResult[] {
  const defaults: Record<string, string> = {
    price: 'Order is within the price cap',
    merchant: 'Mandate store matches Fernwood Supply Co',
    expiry: 'Mandate is still valid',
    currency: 'Both are in USD',
  }
  const results: RuleResult[] = []
  for (const rule of Object.keys(defaults)) {
    const override = overrides[rule]
    if (typeof override === 'string') {
      // a string override means this rule failed, with that reason
      results.push({ rule, passed: false, reason: override })
    } else {
      results.push({ rule, passed: true, reason: defaults[rule] })
    }
  }
  return results
}

interface DecisionInput {
  id: number
  verdict: Decision['verdict']
  riskScore: number
  reason: string
  checkedBy: Decision['checked_by']
  mismatchedFields?: string[]
  ruleResults?: RuleResult[]
  latencyMs: number
  createdMinutesAgo: number
}

function decision(input: DecisionInput): Decision {
  return {
    id: input.id,
    verdict: input.verdict,
    risk_score: input.riskScore,
    reason: input.reason,
    mismatched_fields: input.mismatchedFields ?? [],
    rule_results: input.ruleResults ?? hardRules(),
    latency_ms: input.latencyMs,
    checked_by: input.checkedBy,
    created_at: minutesAgo(input.createdMinutesAgo),
  }
}

interface MockOrderInput {
  id: number
  mandate: MandateSummary
  items: OrderItem[]
  shipping: string
  total: string
  status: OrderStatus
  reviewState: ReviewState | null
  createdMinutesAgo: number
  check: Decision | null
  // set when the human opened the confirm page
  approvalAction?: ApprovalAction | null
  decisionMs?: number
}

function order(input: MockOrderInput): OrderDetail {
  const hasHold = input.status !== 'created' && !(input.status === 'failed' && input.reviewState === null)

  const approvals: Approval[] = []
  if (input.approvalAction !== undefined) {
    const openedMinutesAgo = input.createdMinutesAgo - 2
    const decisionMs = input.decisionMs ?? null
    let decidedAt: string | null = null
    if (input.approvalAction !== null && decisionMs !== null) {
      decidedAt = new Date(Date.now() - openedMinutesAgo * 60_000 + decisionMs).toISOString()
    }
    approvals.push({
      id: input.id * 10,
      action: input.approvalAction,
      decided_by: null,
      opened_at: minutesAgo(openedMinutesAgo),
      decided_at: decidedAt,
      decision_ms: input.approvalAction === null ? null : decisionMs,
      client_decision_ms: input.approvalAction === null ? null : decisionMs,
    })
  }

  const decisions: Decision[] = []
  if (input.check) {
    decisions.push(input.check)
  }

  return {
    id: input.id,
    mandate_id: input.mandate.id,
    paypal_order_id: 'MOCK' + String(input.id).padStart(13, '0'),
    authorization_id: hasHold ? 'MOCKAUTH' + input.id : null,
    capture_id: input.status === 'captured' ? 'MOCKCAP' + input.id : null,
    void_result: input.status === 'voided' ? 'VOIDED' : null,
    vault_id: input.status === 'created' ? null : 'MOCKVAULT',
    // null is our own store, like the API. mockPlacedOrder sets it for a wrong_store order
    merchant: null,
    // the two seed agents take turns, like seed_demo.py
    agent_name: input.id % 2 === 1 ? 'Wren' : 'Thistle',
    items: input.items,
    total: input.total,
    shipping_amount: input.shipping,
    currency: 'USD',
    status: input.status,
    review_state: input.reviewState,
    authorization_expires_at: input.status === 'authorized' ? daysFromNow(29) : null,
    watch_mode: 'normal',
    created_at: minutesAgo(input.createdMinutesAgo),
    mandate: input.mandate,
    intent_check: input.check,
    approval: approvals.at(-1) ?? null,
    decisions,
    approvals,
  }
}

const shoesForMaya = mandate(
  1,
  'Maya R.',
  "Trail running shoes for my sister, women's size 8, up to $120. Any color but pink.",
  '120.00',
  { item: 'trail running shoes', size: "women's 8", excluded_colors: ['pink'] },
  240,
)

const cabinBlanket = mandate(
  2,
  'Jordan T.',
  'A warm blanket for the cabin, nothing synthetic, up to $90.',
  '90.00',
  { item: 'blanket', materials_excluded: ['synthetic'] },
  200,
)

const campMugs = mandate(
  3,
  'Priya S.',
  'Four enamel camp mugs, under $15 each.',
  '60.00',
  { item: 'enamel camp mug', quantity: 4, max_unit_price: '15.00' },
  180,
)

const fieldTote = mandate(
  4,
  'Sam K.',
  'A canvas tote for the farmers market, up to $60.',
  '60.00',
  { item: 'canvas tote', material: 'canvas' },
  150,
)

const headlamp = mandate(
  5,
  'Alex M.',
  'A rechargeable headlamp for night hikes, up to $45.',
  '45.00',
  { item: 'headlamp', power: 'rechargeable' },
  60,
)

const rainShell = mandate(
  6,
  'Dana L.',
  "Black rain shell, men's medium, up to $150.",
  '150.00',
  { item: 'rain jacket', color: 'black', size: "men's M" },
  120,
)

const stove = mandate(
  7,
  'Chris P.',
  'A small backpacking stove, up to $70.',
  '70.00',
  { item: 'backpacking stove' },
  100,
)

export const mockOrders: OrderDetail[] = [
  order({
    id: 101,
    mandate: shoesForMaya,
    items: [{ name: 'Ridgeline trail runner', color: 'slate', size: "women's 8", quantity: 1, unit_price: '104.00' }],
    shipping: '5.00',
    total: '109.00',
    status: 'captured',
    reviewState: 'auto_approved',
    createdMinutesAgo: 230,
    check: decision({
      id: 1,
      verdict: 'match',
      riskScore: 6,
      reason: 'Size, color and price all match the mandate',
      checkedBy: 'ai',
      latencyMs: 1720,
      createdMinutesAgo: 230,
    }),
  }),
  order({
    id: 102,
    mandate: shoesForMaya,
    items: [{ name: 'Ridgeline trail runner', color: 'slate', size: "women's 9", quantity: 1, unit_price: '104.00' }],
    shipping: '5.00',
    total: '109.00',
    status: 'authorized',
    reviewState: 'needs_review',
    createdMinutesAgo: 42,
    check: decision({
      id: 2,
      verdict: 'mismatch',
      riskScore: 74,
      reason: "Size women's 9 ordered, mandate says women's 8",
      checkedBy: 'ai',
      mismatchedFields: ['size'],
      latencyMs: 2010,
      createdMinutesAgo: 42,
    }),
  }),
  order({
    id: 103,
    mandate: cabinBlanket,
    items: [{ name: 'Hearthside camp blanket', color: 'charcoal', size: null, quantity: 1, unit_price: '82.00' }],
    shipping: '6.00',
    total: '88.00',
    status: 'authorized',
    reviewState: 'needs_review',
    createdMinutesAgo: 18,
    check: decision({
      id: 3,
      verdict: 'unsure',
      riskScore: 48,
      reason: 'Listed as an 80% wool blend, mandate asked for nothing synthetic',
      checkedBy: 'ai',
      mismatchedFields: ['item'],
      latencyMs: 2380,
      createdMinutesAgo: 18,
    }),
  }),
  order({
    id: 104,
    mandate: campMugs,
    items: [{ name: 'Enamel camp mug', color: 'speckled blue', size: null, quantity: 4, unit_price: '12.00' }],
    shipping: '0.00',
    total: '48.00',
    status: 'captured',
    reviewState: 'auto_approved',
    createdMinutesAgo: 170,
    check: decision({
      id: 4,
      verdict: 'match',
      riskScore: 4,
      reason: 'Four mugs at $12.00 each, inside the per-mug limit',
      checkedBy: 'ai',
      latencyMs: 1540,
      createdMinutesAgo: 170,
    }),
  }),
  order({
    id: 105,
    mandate: fieldTote,
    items: [{ name: 'Waxed canvas field tote', color: 'olive', size: null, quantity: 1, unit_price: '68.50' }],
    shipping: '6.00',
    total: '74.50',
    status: 'voided',
    reviewState: 'rejected',
    createdMinutesAgo: 140,
    check: decision({
      id: 5,
      verdict: 'mismatch',
      riskScore: 92,
      reason: 'Order is $74.50, over the $60.00 cap',
      checkedBy: 'rules',
      mismatchedFields: ['price'],
      ruleResults: hardRules({ price: 'Order is $74.50 ($68.50 item + $6.00 shipping), over the $60.00 cap' }),
      latencyMs: 12,
      createdMinutesAgo: 140,
    }),
    approvalAction: 'reject',
    decisionMs: 6400,
  }),
  order({
    id: 106,
    mandate: rainShell,
    items: [{ name: 'Stormline rain shell', color: 'navy', size: "men's M", quantity: 1, unit_price: '138.00' }],
    shipping: '0.00',
    total: '138.00',
    status: 'captured',
    reviewState: 'approved',
    createdMinutesAgo: 110,
    check: decision({
      id: 6,
      verdict: 'mismatch',
      riskScore: 66,
      reason: 'Ordered navy, mandate says black',
      checkedBy: 'ai',
      mismatchedFields: ['color'],
      latencyMs: 1890,
      createdMinutesAgo: 110,
    }),
    // approved in just over a second: the kind of tap the rubber-stamp watch flags
    approvalAction: 'approve',
    decisionMs: 1150,
  }),
  order({
    id: 107,
    mandate: stove,
    items: [
      { name: 'Ember mini stove', color: null, size: null, quantity: 1, unit_price: '54.00' },
      { name: 'Fuel canister, 230g', color: null, size: null, quantity: 2, unit_price: '7.00' },
    ],
    shipping: '0.00',
    total: '68.00',
    status: 'captured',
    reviewState: 'approved',
    createdMinutesAgo: 90,
    check: decision({
      id: 7,
      verdict: 'unsure',
      riskScore: 38,
      reason: 'Two fuel canisters added that the mandate did not mention',
      checkedBy: 'ai',
      mismatchedFields: ['extra_item'],
      latencyMs: 2240,
      createdMinutesAgo: 90,
    }),
    approvalAction: 'approve',
    decisionMs: 8300,
  }),
  order({
    id: 108,
    mandate: headlamp,
    items: [{ name: 'Lumen 300 headlamp', color: 'black', size: null, quantity: 1, unit_price: '39.00' }],
    shipping: '0.00',
    total: '39.00',
    status: 'authorized',
    reviewState: 'needs_review',
    createdMinutesAgo: 6,
    // saving the check result failed, so there's no reason to show
    check: null,
    // the human opened the link but hasn't tapped yet
    approvalAction: null,
  }),
  order({
    id: 109,
    mandate: headlamp,
    items: [{ name: 'Lumen 300 headlamp', color: 'black', size: null, quantity: 1, unit_price: '39.00' }],
    shipping: '0.00',
    total: '39.00',
    status: 'created',
    reviewState: null,
    createdMinutesAgo: 3,
    // no hold yet, so nothing has been checked
    check: null,
  }),
  order({
    id: 110,
    mandate: campMugs,
    items: [{ name: 'Enamel camp mug', color: 'forest', size: null, quantity: 4, unit_price: '12.00' }],
    shipping: '0.00',
    total: '48.00',
    status: 'failed',
    reviewState: null,
    createdMinutesAgo: 75,
    // PayPal wouldn't place the hold
    check: null,
  }),
]

// for /place-order. rules have the shape the real parser produces, since the page reads them.
// the mug names no color, so its wrong color button has something to show being disabled
export const mockMandates: MandateSummary[] = [
  mandate(
    21,
    'Sam L.',
    "Black trail runners, women's size 9, up to $120, from Fernwood Supply Co.",
    '120.00',
    { item: 'Trail runners', color: 'black', size: "women's 9", quantity: 1, currency: 'USD' },
    90,
  ),
  mandate(
    22,
    'Riley P.',
    'A navy rain jacket, size M, under $90, from Fernwood Supply Co.',
    '90.00',
    { item: 'Rain jacket', color: 'navy', size: 'M', quantity: 1, currency: 'USD' },
    75,
  ),
  mandate(
    23,
    'Dana W.',
    'One stainless steel camping mug, up to $40, from Fernwood Supply Co.',
    '40.00',
    { item: 'Stainless steel camping mug', color: null, size: null, quantity: 1, currency: 'USD' },
    30,
  ),
]

// what the backend's build_demo_order and the real check would come up with for each scenario
export function mockPlacedOrder(id: number, source: MandateSummary, scenario: DemoScenario): PlacedOrder {
  const cap = Number(source.max_price)
  const rules = source.rules
  const wantedColor = rules.color as string | null
  const isClean = scenario === 'clean'
  const total = (cap * (scenario === 'over_cap' ? 1.15 : 0.8)).toFixed(2)

  let merchant = source.merchant
  let color = wantedColor
  let reason = 'Item, color and size all match the mandate.'
  let riskScore = 5
  let checkedBy: Decision['checked_by'] = 'ai'
  if (scenario === 'wrong_color') {
    color = 'red'
    reason = 'Ordered red, mandate says ' + wantedColor
    riskScore = 90
  } else if (scenario === 'over_cap') {
    reason = 'Order is $' + total + ', mandate cap is $' + source.max_price
    riskScore = 100
    checkedBy = 'rules'
  } else if (scenario === 'wrong_store') {
    merchant = 'Ridgeline Outdoors'
    reason = 'Mandate says ' + source.merchant + ', order is at Ridgeline Outdoors'
    riskScore = 100
    checkedBy = 'rules'
  }

  // which field the check would name, so the dashboard panel highlights the right row
  const mismatchedFields: string[] = []
  let ruleResults: RuleResult[] = hardRules()
  if (scenario === 'wrong_color') {
    mismatchedFields.push('color')
  } else if (scenario === 'over_cap') {
    mismatchedFields.push('price')
    ruleResults = hardRules({ price: reason })
  } else if (scenario === 'wrong_store') {
    mismatchedFields.push('merchant')
    ruleResults = hardRules({ merchant: reason })
  }

  const check = decision({
    id: id * 10,
    verdict: isClean ? 'match' : 'mismatch',
    riskScore,
    reason,
    checkedBy,
    mismatchedFields,
    ruleResults,
    latencyMs: checkedBy === 'rules' ? 15 : 1900,
    createdMinutesAgo: 0,
  })
  const placed = order({
    id,
    mandate: source,
    items: [
      {
        name: rules.item as string,
        color,
        size: rules.size as string | null,
        quantity: 1,
        unit_price: total,
      },
    ],
    shipping: '0.00',
    total,
    status: isClean ? 'captured' : 'authorized',
    reviewState: isClean ? 'auto_approved' : 'needs_review',
    createdMinutesAgo: 0,
    check,
  })

  return {
    ...placed,
    merchant,
    // like DEMO_AGENT_NAME in backend/app/demo.py
    agent_name: 'Demo page',
    review_state: isClean ? 'auto_approved' : 'needs_review',
    intent_check: check,
  }
}
