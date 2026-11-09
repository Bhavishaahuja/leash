// The orders /place-order can send, and when a mandate can't support one.

import type { DemoScenario, MandateSummary } from '../types'

export interface ScenarioInfo {
  id: DemoScenario
  title: string
  description: string
}

export const SCENARIOS: ScenarioInfo[] = [
  { id: 'clean', title: 'Clean order', description: 'Matches the mandate, 80% of the cap' },
  { id: 'wrong_color', title: 'Wrong color', description: 'Right item, different color' },
  { id: 'over_cap', title: 'Over the cap', description: 'Right item, 15% over the cap' },
  { id: 'wrong_store', title: 'Wrong store', description: 'Right item, a different store' },
  { id: 'wrong_size', title: 'Wrong size', description: 'Right item, the next size over' },
  { id: 'substitution', title: 'Substitution', description: 'Something else from the same aisle' },
  { id: 'wrong_model', title: 'Wrong model', description: 'The other model, same price' },
  { id: 'wrong_date', title: 'Wrong date', description: 'Same event, a different day' },
  { id: 'wrong_quantity', title: 'Wrong quantity', description: 'One more, same total' },
]

// which catalog scenarios each category can actually run. mirrors the rows the catalog migration
// adds (backend/migrations/versions/b91d6e2f4a73_add_catalog_items.py): only sneakers come in
// several sizes, only groceries have something to swap in, and so on. if the catalog changes,
// change this too, or a button stays enabled and the click comes back as a 422
const CATALOG_SCENARIOS: Record<string, DemoScenario[]> = {
  sneakers: ['wrong_size'],
  groceries: ['substitution'],
  electronics: ['wrong_model'],
  'event tickets': ['wrong_date'],
  subscription: [],
}

const NO_OTHER_VARIANT: Partial<Record<DemoScenario, string>> = {
  wrong_size: 'The catalog has no other size of this',
  substitution: 'The catalog has nothing to swap in',
  wrong_model: 'The catalog has no other model of this',
  wrong_date: 'The catalog has no other date for this',
}

// same refusals as build_demo_order in backend/app/demo.py, so the button can say why up front
// and nobody burns a click on a 422. null means the scenario can run
export function scenarioBlocker(mandate: MandateSummary, scenario: DemoScenario): string | null {
  // every scenario needs this one
  const item = mandate.rules.item
  if (typeof item !== 'string' || item.trim() === '') {
    return 'This mandate names no item'
  }
  if (scenario === 'wrong_color' && !mandate.rules.color) {
    return 'This mandate names no color'
  }
  if (scenario === 'over_cap' && mandate.max_price === null) {
    return 'This mandate has no cap'
  }
  if (scenario === 'wrong_store' && !mandate.merchant) {
    return 'This mandate names no store'
  }
  if (scenario === 'wrong_size' && !mandate.rules.size) {
    return 'This mandate names no size'
  }
  if (scenario === 'wrong_model' && !mandate.rules.model) {
    return 'This mandate names no model'
  }
  if (scenario === 'wrong_date' && !mandate.rules.event_date) {
    return 'This mandate names no event date'
  }

  const noOtherVariant = NO_OTHER_VARIANT[scenario]
  if (noOtherVariant) {
    const possible = CATALOG_SCENARIOS[catalogCategory(mandate.rules.category)]
    if (!possible) {
      return 'Not for anything in the catalog'
    }
    if (!possible.includes(scenario)) {
      return noOtherVariant
    }
  }
  return null
}

// same as normalize_category in backend/app/catalog.py: "Event  Tickets" is "event tickets"
function catalogCategory(category: unknown): string {
  if (typeof category !== 'string') {
    return ''
  }
  return category.trim().toLowerCase().split(/\s+/).join(' ')
}
