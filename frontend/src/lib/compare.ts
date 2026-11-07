// Lines up what the human asked for against what the agent ordered, one row per field,
// for the side panel. A row is flagged when the intent check named that field, or a
// hard rule for it failed.

import type { Order } from '../types'
import { formatMoney, formatTime } from './format'
import { mandateCurrency } from './orderState'

export interface CompareRow {
  label: string
  wanted: string
  got: string
  flagged: boolean
}

// which check fields count against which row. the AI check uses item/color/size/etc,
// the hard rules use price/merchant/expiry/currency
const FLAG_FIELDS: Record<string, string[]> = {
  Item: ['item', 'extra_item', 'substitution', 'instructions_in_order'],
  Color: ['color'],
  Size: ['size'],
  Quantity: ['quantity'],
  Price: ['price', 'currency'],
  Store: ['merchant'],
  'Valid until': ['expiry'],
}

function ruleText(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) {
    return value
  }
  if (typeof value === 'number') {
    return String(value)
  }
  return null
}

function flaggedFields(order: Order): Set<string> {
  const flagged = new Set<string>()
  const check = order.intent_check
  if (!check) {
    return flagged
  }
  for (const field of check.mismatched_fields ?? []) {
    flagged.add(field)
  }
  for (const result of check.rule_results ?? []) {
    if (!result.passed) {
      flagged.add(result.rule)
    }
  }
  return flagged
}

function joinDistinct(values: (string | null)[]): string {
  const seen: string[] = []
  for (const value of values) {
    if (value && !seen.includes(value)) {
      seen.push(value)
    }
  }
  return seen.length > 0 ? seen.join(', ') : 'Not listed'
}

export function compareMandate(order: Order): CompareRow[] {
  const rules = order.mandate.rules
  const flagged = flaggedFields(order)
  const rows: CompareRow[] = []

  function add(label: string, wanted: string | null, got: string, alwaysShow = false) {
    let isFlagged = false
    for (const field of FLAG_FIELDS[label] ?? []) {
      if (flagged.has(field)) {
        isFlagged = true
      }
    }
    // skip fields the human never mentioned, unless the check flagged them anyway
    if (wanted === null && !isFlagged && !alwaysShow) {
      return
    }
    rows.push({ label, wanted: wanted ?? 'Not specified', got, flagged: isFlagged })
  }

  const itemNames: string[] = []
  const colors: (string | null)[] = []
  const sizes: (string | null)[] = []
  let totalQuantity = 0
  for (const item of order.items) {
    itemNames.push(item.quantity > 1 ? item.quantity + ' x ' + item.name : item.name)
    colors.push(item.color)
    sizes.push(item.size)
    totalQuantity += item.quantity
  }

  add('Item', ruleText(rules.item), itemNames.join(', ') || 'No items', true)

  let wantedColor = ruleText(rules.color)
  if (!wantedColor && Array.isArray(rules.excluded_colors) && rules.excluded_colors.length > 0) {
    wantedColor = 'Anything but ' + rules.excluded_colors.join(', ')
  }
  add('Color', wantedColor, joinDistinct(colors))

  add('Size', ruleText(rules.size), joinDistinct(sizes))
  add('Quantity', ruleText(rules.quantity), String(totalQuantity))

  let wantedPrice: string | null = null
  if (order.mandate.max_price !== null) {
    const cap = formatMoney(order.mandate.max_price, mandateCurrency(order))
    wantedPrice = (order.mandate.max_price_inclusive ? 'Up to ' : 'Under ') + cap
  }
  let gotPrice = formatMoney(order.total, order.currency)
  if (order.shipping_amount !== null && Number(order.shipping_amount) > 0) {
    gotPrice += ' incl. ' + formatMoney(order.shipping_amount, order.currency) + ' shipping'
  }
  add('Price', wantedPrice, gotPrice, true)

  add('Store', order.mandate.merchant, 'Fernwood Supply Co', true)

  const expires = order.mandate.expires_at ? formatTime(order.mandate.expires_at) : null
  add('Valid until', expires, 'Ordered ' + formatTime(order.created_at))

  return rows
}
