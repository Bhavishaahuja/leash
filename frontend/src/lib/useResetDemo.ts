// State for the top bar's "Reset demo": ask, reset, then show what happened. ResetDemo.tsx draws it.

import { useState } from 'react'
import { resetDemo } from '../api'
import type { ResetSummary } from '../types'

export type ResetState =
  | { step: 'idle' }
  | { step: 'confirming' }
  | { step: 'resetting' }
  | { step: 'done'; summary: ResetSummary }
  | { step: 'error'; message: string }

export function useResetDemo() {
  const [state, setState] = useState<ResetState>({ step: 'idle' })

  async function confirm() {
    setState({ step: 'resetting' })
    try {
      setState({ step: 'done', summary: await resetDemo() })
    } catch (caught) {
      // the API's own words, e.g. "The demo was just reset, try again in a minute" on a 429
      setState({ step: 'error', message: caught instanceof Error ? caught.message : 'Reset failed' })
    }
  }

  return {
    state,
    ask: () => setState({ step: 'confirming' }),
    confirm,
    dismiss: () => setState({ step: 'idle' }),
  }
}

export type ResetControls = ReturnType<typeof useResetDemo>
