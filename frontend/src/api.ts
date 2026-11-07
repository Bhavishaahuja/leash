// Talks to the Leash API. Every function has a mock twin so the UI can be built
// before the backend is live: set VITE_USE_MOCKS=false in .env.local to hit the real API.

import { scenarioBlocker } from './lib/demoScenarios'
import { mockMandates, mockOrders, mockPlacedOrder } from './mocks'
import type {
  ApprovalAction,
  DemoScenario,
  MandateSummary,
  Order,
  OrderDetail,
  PlacedOrder,
  ReviewLink,
  ReviewView,
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
        }
      : null,
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
