// Display helpers. Money arrives as decimal strings ("109.00"), times as ISO strings.

import type { OrderItem } from '../types'

const moneyFormatters = new Map<string, Intl.NumberFormat>()

export function formatMoney(value: string | null, currency = 'USD'): string {
  if (value === null) {
    return 'No cap'
  }
  let formatter = moneyFormatters.get(currency)
  if (!formatter) {
    formatter = new Intl.NumberFormat('en-US', { style: 'currency', currency })
    moneyFormatters.set(currency, formatter)
  }
  return formatter.format(Number(value))
}

const timeFormatter = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

export function formatTime(iso: string): string {
  return timeFormatter.format(new Date(iso))
}

export function describeItem(item: OrderItem): string {
  // same idea as item_display_name in the backend: "Ridgeline trail runner, slate, size women's 8"
  const parts = [item.name]
  if (item.color) {
    parts.push(item.color)
  }
  if (item.size) {
    parts.push(sizeLabel(item.size))
  }
  return parts.join(', ')
}

function sizeLabel(size: string): string {
  // agents sometimes send "size women's 9" already, and "size size women's 9" reads like a bug
  if (size.trim().toLowerCase().startsWith('size')) {
    return size.trim()
  }
  return 'size ' + size
}

// seed_demo.py tags its buyers so a reset can find them (SEED_LABEL_PREFIX in backend/app/demo.py).
// the tag is for the script, not for people, so the screens drop it
export const SEED_LABEL_PREFIX = 'seed-demo:'

export function buyerName(label: string): string {
  if (label.startsWith(SEED_LABEL_PREFIX)) {
    return label.slice(SEED_LABEL_PREFIX.length).trim()
  }
  return label
}

export function summarizeItems(items: OrderItem[]): string {
  if (items.length === 0) {
    return 'No items'
  }
  const first = items[0]
  const label = first.quantity > 1 ? first.quantity + ' x ' + first.name : first.name
  if (items.length === 1) {
    return label
  }
  return label + ' + ' + (items.length - 1) + ' more'
}

export function formatDuration(ms: number): string {
  // "12ms" for the hard rules, "1.2s" for quick taps, "1m 05s" once seconds get hard to read
  if (ms < 1000) {
    return ms + 'ms'
  }
  if (ms < 60_000) {
    return (ms / 1000).toFixed(1) + 's'
  }
  const totalSeconds = Math.round(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return minutes + 'm ' + String(seconds).padStart(2, '0') + 's'
}

export function formatDateTime(iso: string | null): string {
  if (!iso) {
    return 'Not yet'
  }
  return formatTime(iso)
}
