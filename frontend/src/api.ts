// Talks to the Leash API. Every function has a mock twin so the UI can be built
// before the backend is live: set VITE_USE_MOCKS=false in .env.local to hit the real API.

import { scenarioBlocker } from './lib/demoScenarios'
import { SEED_LABEL_PREFIX } from './lib/format'
import { mockMandates, mockOrders, mockPlacedOrder } from './mocks'
import type {
  ApprovalAction,
  DemoScenario,
  Dispute,
  DisputeEvidence,
  DisputeSyncResult,
  EvidencePacket,
  MandateSummary,
  Order,
  OrderDetail,
  PlacedOrder,
  ReviewLink,
  ResetSummary,
  ReviewView,
  Stats,
} from './types'

const API_URL = (import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '')

// mocks are on unless explicitly turned off, so a fresh clone works with no setup
export const USE_MOCKS = import.meta.env.VITE_USE_MOCKS !== 'false'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(describeDetail(status, detail))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function describeDetail(status: number, detail: unknown): string {
  // FastAPI sends {"detail": "..."} for our 404/409s and {"detail": {...}} for PayPal 502s
  if (typeof detail === 'string') {
    return detail
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message)
  }
  return 'Request failed with status ' + status
}

// ---- login ----

// the session token from POST /auth/session. sessionStorage per docs/api.md, so it goes away with
// the tab. the try/catch is for browsers that block storage: login just won't stick there
const SESSION_KEY = 'leash_session_token'

function readSessionToken(): string | null {
  try {
    return sessionStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
}

function writeSessionToken(token: string | null) {
  try {
    if (token === null) {
      sessionStorage.removeItem(SESSION_KEY)
    } else {
      sessionStorage.setItem(SESSION_KEY, token)
    }
  } catch {
    // nothing to do, see above
  }
}

// fired on window when a merchant call comes back 401: the session expired or was ended.
// MerchantLayout listens and swaps the page for the login form
export const SESSION_EXPIRED_EVENT = 'leash:session-expired'

export function hasSession(): boolean {
  return USE_MOCKS || readSessionToken() !== null
}

interface RequestOptions {
  // the review routes take the token in the URL and nothing else. the human's phone has no
  // session, and a merchant testing the link on their own laptop shouldn't send theirs either
  withSession?: boolean
}

async function throwApiError(response: Response, withSession: boolean): Promise<never> {
  // an expired session is no use to the next request either. a wrong password is a 401 too, and
  // dropping nothing is harmless there
  if (response.status === 401 && withSession) {
    writeSessionToken(null)
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  }

  let detail: unknown = null
  try {
    const body = await response.json()
    detail = body?.detail ?? body
  } catch {
    // body wasn't JSON, the status code will have to do
  }
  throw new ApiError(response.status, detail)
}

async function request<T>(path: string, init?: RequestInit, options: RequestOptions = {}): Promise<T> {
  const withSession = options.withSession ?? true
  const token = withSession ? readSessionToken() : null
  const response = await fetch(API_URL + path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    await throwApiError(response, withSession)
  }

  // POST /review/{token}/opened is a 204 with no body
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

// ---- mock plumbing ----

// copy so mock captures/voids during a session don't leak into the source array
const mockStore: OrderDetail[] = structuredClone(mockOrders)

function mockDelay<T>(value: T): Promise<T> {
  // a little latency so loading states show up in the UI
  return new Promise((resolve) => setTimeout(() => resolve(structuredClone(value)), 250))
}

function findMockOrder(orderId: number): OrderDetail {
  const found = mockStore.find((order) => order.id === orderId)
  if (!found) {
    throw new ApiError(404, 'Order ' + orderId + ' not found')
  }
  return found
}

function stripDetail(order: OrderDetail): Order {
  // GET /orders returns OrderOut: mandate, intent_check and approval inline, but no full history
  const { decisions, approvals, ...plain } = order
  void decisions
  void approvals
  return plain
}

// review tokens handed out by the mock review-link call, token to order id. same rules as the
// backend: a new link kills the old one, and a link works once
const mockReviewTokens = new Map<string, number>()
const usedMockTokens = new Set<string>()

// the order id is in the token so a mock link still works in a fresh tab, where this
// in-memory map starts empty. real tokens are opaque, never parse them
function newMockToken(orderId: number): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  let token = 'mock' + orderId + '-'
  for (const byte of bytes) {
    token += byte.toString(16).padStart(2, '0')
  }
  return token
}

function isWaitingForReview(order: Order): boolean {
  return order.status === 'authorized' && order.review_state === 'needs_review'
}

function createMockReviewLink(orderId: number): ReviewLink {
  const order = findMockOrder(orderId)
  if (!isWaitingForReview(order)) {
    throw new ApiError(409, 'Order ' + orderId + " isn't waiting for review")
  }
  // calling this again replaces the link, so the old one stops working
  for (const [token, id] of mockReviewTokens) {
    if (id === orderId) {
      mockReviewTokens.delete(token)
    }
  }
  const token = newMockToken(orderId)
  mockReviewTokens.set(token, orderId)
  return { review_token: token, expires_at: order.authorization_expires_at ?? new Date().toISOString() }
}

function findMockReviewOrder(token: string): OrderDetail {
  let orderId = mockReviewTokens.get(token)
  if (orderId === undefined && mockReviewTokens.size === 0) {
    // fresh tab: trust the id in the token, see newMockToken
    const match = /^mock(\d+)-/.exec(token)
    if (match && mockStore.some((order) => order.id === Number(match[1]))) {
      orderId = Number(match[1])
      mockReviewTokens.set(token, orderId)
    }
  }
  if (orderId === undefined) {
    throw new ApiError(404, { message: "This link isn't valid. The store may have sent a newer one." })
  }
  return findMockOrder(orderId)
}

function toReviewView(order: OrderDetail): ReviewView {
  // only what the real GET /review/{token} sends: nothing the human doesn't need
  const check = order.intent_check
  return {
    id: order.id,
    items: order.items,
    total: order.total,
    shipping_amount: order.shipping_amount,
    currency: order.currency,
    status: order.status,
    review_state: order.review_state,
    authorization_expires_at: order.authorization_expires_at,
    mandate: { buyer_label: order.mandate.buyer_label, raw_request: order.mandate.raw_request },
    intent_check: check
      ? {
          verdict: check.verdict,
          risk_score: check.risk_score,
          reason: check.reason,
          mismatched_fields: check.mismatched_fields,
          suggested_fix: check.suggested_fix,
        }
      : null,
    replaced_by_order_id: order.replaced_by_order_id,
  }
}

function getMockReview(token: string): ReviewView {
  const order = findMockReviewOrder(token)
  if (usedMockTokens.has(token) || !isWaitingForReview(order)) {
    throw new ApiError(410, { message: "This link was already used, or the order isn't waiting for review anymore." })
  }
  return toReviewView(order)
}

function openMockReview(token: string): void {
  getMockReview(token)
  const order = findMockReviewOrder(token)
  // only the first call counts, so a refresh doesn't reset the clock
  if (order.approval) {
    return
  }
  const approval = {
    id: order.id * 10 + order.approvals.length,
    action: null,
    decided_by: null,
    opened_at: new Date().toISOString(),
    decided_at: null,
    decision_ms: null,
    client_decision_ms: null,
  }
  order.approvals.push(approval)
  order.approval = approval
}

function decideMockReview(token: string, action: ApprovalAction, clientDecisionMs: number): ReviewView {
  const order = findMockReviewOrder(token)
  if (usedMockTokens.has(token) || !isWaitingForReview(order)) {
    throw new ApiError(409, {
      message: 'This order was already decided.',
      status: order.status,
      review_state: order.review_state,
    })
  }

  const now = new Date()
  let approval = order.approval
  if (!approval) {
    // the page never reported it opened, so the server can't time it: decision_ms is 0
    approval = {
      id: order.id * 10 + order.approvals.length,
      action: null,
      decided_by: null,
      opened_at: now.toISOString(),
      decided_at: null,
      decision_ms: null,
      client_decision_ms: null,
    }
    order.approvals.push(approval)
    order.approval = approval
  }
  approval.action = action
  approval.decided_at = now.toISOString()
  approval.decision_ms = now.getTime() - new Date(approval.opened_at ?? now.toISOString()).getTime()
  approval.client_decision_ms = clientDecisionMs

  if (action === 'approve') {
    order.status = 'captured'
    order.capture_id = 'MOCKCAP' + order.id
    order.review_state = 'approved'
  } else {
    order.status = 'voided'
    order.void_result = 'VOIDED'
    order.review_state = 'rejected'
  }
  order.authorization_expires_at = null
  usedMockTokens.add(token)
  return toReviewView(order)
}

function placeMockDemoOrder(mandateId: number, scenario: DemoScenario): PlacedOrder {
  const mandate = mockMandates.find((row) => row.id === mandateId)
  if (!mandate) {
    throw new ApiError(404, 'Mandate ' + mandateId + ' not found')
  }
  // same refusals as the real endpoint
  const blocker = scenarioBlocker(mandate, scenario)
  if (blocker) {
    throw new ApiError(422, blocker)
  }

  let highestId = 0
  for (const existing of mockStore) {
    highestId = Math.max(highestId, existing.id)
  }
  const placed = mockPlacedOrder(highestId + 1, mandate, scenario)
  // into the store so the dashboard and the confirm link can find it
  mockStore.push(placed)
  return placed
}

// ---- public API ----

export async function logIn(password: string): Promise<void> {
  // withSession false: a wrong password is a 401 too, and that isn't an expired session
  const session = await request<{ session_token: string }>(
    '/auth/session',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    },
    { withSession: false },
  )
  writeSessionToken(session.session_token)
}

// the backend has no logout route, the token just expires after 8 hours. forgetting it here is
// enough for a shared demo laptop
export function logOut(): void {
  writeSessionToken(null)
}

export async function listMandates(limit = 50): Promise<MandateSummary[]> {
  if (USE_MOCKS) {
    return mockDelay(mockMandates.slice(0, limit))
  }
  return request<MandateSummary[]>('/mandates?limit=' + limit)
}

export async function placeDemoOrder(mandateId: number, scenario: DemoScenario): Promise<PlacedOrder> {
  if (USE_MOCKS) {
    return mockDelay(placeMockDemoOrder(mandateId, scenario))
  }
  return request<PlacedOrder>('/demo/orders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mandate_id: mandateId, scenario }),
  })
}

export async function listOrders(limit = 50): Promise<Order[]> {
  if (USE_MOCKS) {
    const newestFirst = [...mockStore].sort((a, b) => b.created_at.localeCompare(a.created_at))
    const plain: Order[] = []
    for (const order of newestFirst.slice(0, limit)) {
      plain.push(stripDetail(order))
    }
    return mockDelay(plain)
  }
  return request<Order[]>('/orders?limit=' + limit)
}

// same numbers the backend works out in stats.py, from the mock orders
function mockStats(): Stats {
  const counts = { total: 0, auto: 0, review: 0, approved: 0, voided: 0 }
  const humanDecisions: { decidedAt: string; ms: number | null; action: ApprovalAction }[] = []
  for (const order of mockStore) {
    counts.total += 1
    if (order.review_state === 'auto_approved') {
      counts.auto += 1
    }
    if (isWaitingForReview(order)) {
      counts.review += 1
    }
    if (order.review_state === 'approved') {
      counts.approved += 1
    }
    if (order.status === 'voided') {
      counts.voided += 1
    }
    const approval = order.approval
    if (approval?.action && approval.decided_at) {
      humanDecisions.push({ decidedAt: approval.decided_at, ms: approval.decision_ms, action: approval.action })
    }
  }

  // newest first, last 10, like rubber_stamp.load_recent_decisions
  humanDecisions.sort((a, b) => b.decidedAt.localeCompare(a.decidedAt))
  const recent = humanDecisions.slice(0, 10)
  const fastThresholdMs = 2000
  let fastStreak = 0
  for (const decision of recent) {
    // a reject or a slow (or untimed) approval ends the streak
    if (decision.action !== 'approve' || decision.ms === null || decision.ms >= fastThresholdMs) {
      break
    }
    fastStreak += 1
  }

  const timings: number[] = []
  for (const decision of humanDecisions) {
    if (decision.ms !== null) {
      timings.push(decision.ms)
    }
  }
  timings.sort((a, b) => a - b)
  let median: number | null = null
  if (timings.length > 0) {
    const middle = Math.floor(timings.length / 2)
    median = timings.length % 2 === 1 ? timings[middle] : Math.round((timings[middle - 1] + timings[middle]) / 2)
  }

  const recentMs: (number | null)[] = []
  for (const decision of recent) {
    recentMs.push(decision.ms)
  }

  return {
    total_orders: counts.total,
    auto_captured: counts.auto,
    needs_review: counts.review,
    approved: counts.approved,
    voided: counts.voided,
    median_decision_ms: median,
    fast_streak: fastStreak,
    rubber_stamp_active: fastStreak >= 3,
    fast_threshold_ms: fastThresholdMs,
    recent_decision_ms: recentMs,
  }
}

export async function getStats(): Promise<Stats> {
  if (USE_MOCKS) {
    return mockDelay(mockStats())
  }
  return request<Stats>('/stats')
}

// fired on window after a demo reset, so the dashboard refetches right away instead of on the next poll
export const DEMO_RESET_EVENT = 'leash:demo-reset'

// same rule as the backend: only seeded rows (buyer_label starts with seed-demo:) are touched
function resetMockDemo(): ResetSummary {
  const voidedIds: number[] = []
  const deletedMandates = new Set<number>()
  let deletedOrders = 0
  for (let index = mockStore.length - 1; index >= 0; index -= 1) {
    const order = mockStore[index]
    if (!order.mandate.buyer_label.startsWith(SEED_LABEL_PREFIX)) {
      continue
    }
    if (order.status === 'authorized') {
      voidedIds.push(order.id)
    }
    deletedMandates.add(order.mandate_id)
    deletedOrders += 1
    mockStore.splice(index, 1)
  }
  return {
    voided_holds: voidedIds.length,
    voided_order_ids: voidedIds,
    deleted_orders: deletedOrders,
    deleted_mandates: deletedMandates.size,
    deleted_disputes: 0,
  }
}

// voids open seeded holds at PayPal, then deletes the seeded rows. 429 if run more than 3 times a minute
export async function resetDemo(): Promise<ResetSummary> {
  const summary = USE_MOCKS ? await mockDelay(resetMockDemo()) : await request<ResetSummary>('/demo/reset', { method: 'POST' })
  window.dispatchEvent(new Event(DEMO_RESET_EVENT))
  return summary
}

export async function getOrder(orderId: number): Promise<OrderDetail> {
  if (USE_MOCKS) {
    // a throw in here becomes a rejected promise, same as a real 404
    return mockDelay(findMockOrder(orderId))
  }
  return request<OrderDetail>('/orders/' + orderId)
}

// ---- human review: merchant side ----

// makes a new review link and kills any older one for this order. only call it when the merchant
// asks, or a link already sent to the human stops working
export async function createReviewLink(orderId: number): Promise<ReviewLink> {
  if (USE_MOCKS) {
    return mockDelay(createMockReviewLink(orderId))
  }
  return request<ReviewLink>('/orders/' + orderId + '/review-link', { method: 'POST' })
}

// ---- human review: the confirm page ----

function reviewPath(token: string): string {
  return '/review/' + encodeURIComponent(token)
}

export async function getReview(token: string): Promise<ReviewView> {
  if (USE_MOCKS) {
    return mockDelay(getMockReview(token))
  }
  return request<ReviewView>(reviewPath(token), undefined, { withSession: false })
}

// starts the server's attention clock. call once, after the order is really on screen
export async function markReviewOpened(token: string): Promise<void> {
  if (USE_MOCKS) {
    openMockReview(token)
    return mockDelay(undefined)
  }
  return request<void>(reviewPath(token) + '/opened', { method: 'POST' }, { withSession: false })
}

async function decideReview(token: string, action: ApprovalAction, decisionMs: number): Promise<ReviewView> {
  // the backend wants a whole number that fits a Postgres int
  const clientMs = Math.min(Math.max(Math.round(decisionMs), 0), 2147483647)
  if (USE_MOCKS) {
    return mockDelay(decideMockReview(token, action, clientMs))
  }
  return request<ReviewView>(
    reviewPath(token) + '/' + action,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision_ms: clientMs }),
    },
    { withSession: false },
  )
}

// captures the money
export function approveReview(token: string, decisionMs: number): Promise<ReviewView> {
  return decideReview(token, 'approve', decisionMs)
}

// releases the hold
export function rejectReview(token: string, decisionMs: number): Promise<ReviewView> {
  return decideReview(token, 'reject', decisionMs)
}

// ---- disputes: merchant side ----

// two disputes on captured mock orders: one waiting on the merchant, one already answered
const mockDisputes: Dispute[] = [
  {
    id: 1,
    paypal_dispute_id: 'PP-R-MOCK-10001234',
    order_id: 101,
    reason: 'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
    status: 'WAITING_FOR_SELLER_RESPONSE',
    evidence_submitted: false,
    evidence_submitted_at: null,
    evidence_type: null,
    has_summary: false,
    seller_response_due_at: new Date(Date.now() + 9 * 24 * 3600 * 1000).toISOString(),
    created_at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
  },
  {
    id: 2,
    paypal_dispute_id: 'PP-R-MOCK-10005678',
    order_id: 104,
    reason: 'UNAUTHORISED',
    status: 'UNDER_REVIEW',
    evidence_submitted: true,
    evidence_submitted_at: new Date(Date.now() - 26 * 3600 * 1000).toISOString(),
    evidence_type: 'OTHER',
    has_summary: true,
    seller_response_due_at: null,
    created_at: new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(),
  },
]
const mockSummaries = new Map<number, string>([
  [2, 'The account holder set a standing instruction for this purchase before it was placed, and the order matched it.'],
])

function findMockDispute(disputeId: number): Dispute {
  const found = mockDisputes.find((dispute) => dispute.id === disputeId)
  if (!found) {
    throw new ApiError(404, 'Dispute ' + disputeId + ' not found')
  }
  return found
}

function mockPacket(dispute: Dispute): EvidencePacket {
  // same parts as build_evidence_packet, from the mock order
  const order = findMockOrder(dispute.order_id)
  const check = order.intent_check
  const approval = order.approval
  const auto = order.review_state === 'auto_approved'
  return {
    dispute: { paypal_dispute_id: dispute.paypal_dispute_id, reason: dispute.reason, status: dispute.status },
    mandate: {
      id: order.mandate.id,
      raw_request: order.mandate.raw_request,
      rules: order.mandate.rules,
      max_price: order.mandate.max_price,
      max_price_inclusive: order.mandate.max_price_inclusive,
      merchant: order.mandate.merchant,
      expires_at: order.mandate.expires_at,
      created_at: order.mandate.created_at,
    },
    order: {
      id: order.id,
      store: order.merchant ?? 'Fernwood Supply Co',
      items: order.items,
      total: order.total,
      shipping_amount: order.shipping_amount,
      currency: order.currency,
      status: order.status,
      paypal_order_id: order.paypal_order_id,
      capture_id: order.capture_id,
      created_at: order.created_at,
    },
    intent_check: check
      ? {
          verdict: check.verdict,
          risk_score: check.risk_score,
          reason: check.reason,
          mismatched_fields: check.mismatched_fields,
          checked_by: check.checked_by,
          checked_at: check.created_at,
          watch_mode: order.watch_mode,
        }
      : null,
    decision: {
      released_by: auto ? 'automatic capture' : approval?.action ? 'human review' : null,
      review_state: order.review_state,
      action: auto ? 'capture' : (approval?.action ?? null),
      decision_ms: auto ? null : (approval?.decision_ms ?? null),
      opened_at: approval?.opened_at ?? null,
      decided_at: auto ? (check?.created_at ?? null) : (approval?.decided_at ?? null),
    },
  }
}

function mockEvidence(disputeId: number, redraft: boolean): DisputeEvidence {
  const dispute = findMockDispute(disputeId)
  if (redraft && dispute.evidence_submitted) {
    throw new ApiError(409, { message: 'Evidence was already submitted, the summary is what PayPal has now' })
  }
  if (redraft || !mockSummaries.has(disputeId)) {
    const packet = mockPacket(dispute)
    const opening = redraft ? 'Before any money moved, ' : 'Before the payment was taken, '
    mockSummaries.set(
      disputeId,
      opening + 'the buyer wrote "' + packet.mandate.raw_request + '". Order ' + packet.order.id +
        ' was checked against that request and released by ' + (packet.decision.released_by ?? 'nobody') +
        '. The items, store and total match what the buyer asked for.',
    )
    dispute.has_summary = true
  }
  return { dispute, packet: mockPacket(dispute), summary: mockSummaries.get(disputeId) ?? '' }
}

export async function listDisputes(limit = 50): Promise<Dispute[]> {
  if (USE_MOCKS) {
    return mockDelay(mockDisputes.slice(0, limit))
  }
  return request<Dispute[]>('/disputes?limit=' + limit)
}

// asks PayPal about every captured order, saves what it finds. safe to run again
export async function syncDisputes(): Promise<DisputeSyncResult> {
  if (USE_MOCKS) {
    let captured = 0
    for (const order of mockStore) {
      if (order.status === 'captured') {
        captured += 1
      }
    }
    return mockDelay({ orders_checked: captured, disputes_found: mockDisputes.length, created: 0, updated: mockDisputes.length, errors: [] })
  }
  return request<DisputeSyncResult>('/disputes/sync', { method: 'POST' })
}

// the first look drafts the summary with Claude (a few seconds), later looks return the saved one
export async function getDisputeEvidence(disputeId: number): Promise<DisputeEvidence> {
  if (USE_MOCKS) {
    return mockDelay(mockEvidence(disputeId, false))
  }
  return request<DisputeEvidence>('/disputes/' + disputeId + '/evidence')
}

export async function redraftDisputeEvidence(disputeId: number): Promise<DisputeEvidence> {
  if (USE_MOCKS) {
    return mockDelay(mockEvidence(disputeId, true))
  }
  return request<DisputeEvidence>('/disputes/' + disputeId + '/redraft', { method: 'POST' })
}

export interface DownloadedFile {
  blob: Blob
  fileName: string
}

// a tiny one-page PDF so the download works in mock mode too. ASCII only, so string length is
// byte length and the xref offsets come out right
function mockEvidencePdf(dispute: Dispute): Blob {
  const lines = [
    'Leash evidence packet (sample data)',
    'Dispute ' + dispute.paypal_dispute_id + ', order #' + dispute.order_id,
    'The real file lists the mandate, the order, the check and who released the money.',
  ]
  let text = 'BT /F1 11 Tf 60 740 Td 15 TL\n'
  for (const line of lines) {
    const safe = line.replace(/[^\x20-\x7e]/g, '?').replace(/([()\\])/g, '\\$1')
    text += '(' + safe + ') Tj T*\n'
  }
  text += 'ET'
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [4 0 R] /Count 1 >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 3 0 R >> >> >>',
    '<< /Length ' + text.length + ' >>\nstream\n' + text + '\nendstream',
  ]
  let pdf = '%PDF-1.4\n'
  const offsets: number[] = []
  for (let index = 0; index < objects.length; index++) {
    offsets.push(pdf.length)
    pdf += index + 1 + ' 0 obj\n' + objects[index] + '\nendobj\n'
  }
  const xrefStart = pdf.length
  pdf += 'xref\n0 ' + (objects.length + 1) + '\n0000000000 65535 f \n'
  for (const offset of offsets) {
    pdf += String(offset).padStart(10, '0') + ' 00000 n \n'
  }
  pdf += 'trailer\n<< /Size ' + (objects.length + 1) + ' /Root 1 0 R >>\nstartxref\n' + xrefStart + '\n%%EOF\n'
  return new Blob([pdf], { type: 'application/pdf' })
}

// the evidence PDF, the same file submit sends PayPal. a plain link can't carry the session
// header, so this fetches it and the page saves the blob
export async function downloadDisputeEvidencePdf(disputeId: number): Promise<DownloadedFile> {
  if (USE_MOCKS) {
    const dispute = findMockDispute(disputeId)
    return mockDelay({ blob: mockEvidencePdf(dispute), fileName: 'leash-evidence-' + dispute.paypal_dispute_id + '.pdf' })
  }
  const token = readSessionToken()
  const response = await fetch(API_URL + '/disputes/' + disputeId + '/evidence.pdf', {
    headers: {
      Accept: 'application/pdf',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
  })
  if (!response.ok) {
    await throwApiError(response, true)
  }
  // the server names it after the PayPal dispute id, keep that name if the header is readable
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="([^"]+)"/.exec(disposition)
  return { blob: await response.blob(), fileName: match ? match[1] : 'leash-evidence-' + disputeId + '.pdf' }
}

// files the PDF and summary with PayPal. PayPal takes this once
export async function submitDisputeEvidence(disputeId: number): Promise<Dispute> {
  if (USE_MOCKS) {
    const dispute = findMockDispute(disputeId)
    if (dispute.evidence_submitted) {
      throw new ApiError(409, { message: 'Evidence was already submitted for this dispute' })
    }
    if (!dispute.has_summary) {
      throw new ApiError(409, { message: 'Draft the summary first' })
    }
    dispute.evidence_submitted = true
    dispute.evidence_submitted_at = new Date().toISOString()
    dispute.evidence_type = 'OTHER'
    return mockDelay(dispute)
  }
  return request<Dispute>('/disputes/' + disputeId + '/submit-evidence', { method: 'POST' })
}
