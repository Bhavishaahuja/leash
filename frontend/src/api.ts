// Talks to the Leash API. Every function has a mock twin so the UI can be built
// before the backend is live: set VITE_USE_MOCKS=false in .env.local to hit the real API.

import { scenarioBlocker } from './lib/demoScenarios'
import { mockMandates, mockOrders, mockPlacedOrder } from './mocks'
import type { DemoScenario, MandateSummary, Order, OrderDetail, PlacedOrder } from './types'

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

export function hasSession(): boolean {
  return USE_MOCKS || readSessionToken() !== null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = readSessionToken()
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
    if (response.status === 401) {
      writeSessionToken(null)
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

function settleMockOrder(orderId: number, nextStatus: 'captured' | 'voided'): Order {
  const order = findMockOrder(orderId)
  // same rule as the backend's require_status: only an authorized hold can be captured or voided
  if (order.status !== 'authorized') {
    const action = nextStatus === 'captured' ? 'capture' : 'void'
    throw new ApiError(409, "Can't " + action + ' order ' + orderId + ' (status ' + order.status + ')')
  }
  order.status = nextStatus
  if (nextStatus === 'captured') {
    order.capture_id = 'MOCKCAP' + orderId
    order.review_state = 'approved'
  } else {
    order.void_result = 'VOIDED'
    order.review_state = 'rejected'
  }
  order.authorization_expires_at = null
  return stripDetail(order)
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
  const session = await request<{ session_token: string }>('/auth/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  writeSessionToken(session.session_token)
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

export async function captureOrder(orderId: number): Promise<Order> {
  if (USE_MOCKS) {
    return mockDelay(settleMockOrder(orderId, 'captured'))
  }
  return request<Order>('/orders/' + orderId + '/capture', { method: 'POST' })
}

export async function voidOrder(orderId: number): Promise<Order> {
  if (USE_MOCKS) {
    return mockDelay(settleMockOrder(orderId, 'voided'))
  }
  return request<Order>('/orders/' + orderId + '/void', { method: 'POST' })
}
