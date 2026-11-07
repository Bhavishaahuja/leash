// The four orders /place-order can send, and when a mandate can't support one.

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
]

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
  return null
}
