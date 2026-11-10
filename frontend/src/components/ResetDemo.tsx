// "Reset demo" in the top bar, for judges and for us between takes. It asks first, because it voids
// real sandbox holds, then says what it did. The strip sits under the header, styles in index.css.

import type { ResetControls } from '../lib/useResetDemo'
import type { ResetSummary } from '../types'

function plural(count: number, word: string): string {
  return count + ' ' + word + (count === 1 ? '' : 's')
}

function describeSummary(summary: ResetSummary): string {
  if (summary.deleted_orders === 0 && summary.deleted_mandates === 0) {
    return 'Nothing to reset, there was no seeded demo data.'
  }
  let removed = plural(summary.deleted_orders, 'order') + ' and ' + plural(summary.deleted_mandates, 'mandate')
  if (summary.deleted_disputes > 0) {
    removed = plural(summary.deleted_orders, 'order') + ', ' + plural(summary.deleted_mandates, 'mandate')
    removed += ' and ' + plural(summary.deleted_disputes, 'dispute')
  }
  return 'Demo reset. Voided ' + plural(summary.voided_holds, 'hold') + ', removed ' + removed + '. Place new orders from Place order.'
}

export function ResetButton({ controls }: { controls: ResetControls }) {
  const busy = controls.state.step === 'resetting'
  return (
    <button
      type="button"
      className="topbar__link topbar__reset"
      onClick={controls.ask}
      disabled={busy}
      aria-expanded={controls.state.step === 'confirming'}
    >
      <span className="topbar__reset-long">Reset demo</span>
      <span className="topbar__reset-short">Reset</span>
    </button>
  )
}

export function ResetStrip({ controls }: { controls: ResetControls }) {
  const state = controls.state

  // always rendered, so screen readers hear the result through aria-live
  return (
    <div className={'reset-strip reset-strip--' + (state.step === 'idle' ? 'empty' : state.step)} aria-live="polite">
      {state.step !== 'idle' && (
        <div className="reset-strip__inner">
          {state.step === 'confirming' && (
            <>
              <p className="reset-strip__text">
                Reset the demo? This voids every open seeded hold in the PayPal sandbox and deletes the seeded
                orders, mandates and disputes.
              </p>
              <div className="reset-strip__actions">
                <button type="button" className="secondary-button" onClick={controls.dismiss}>
                  Cancel
                </button>
                <button type="button" className="danger-button" onClick={controls.confirm}>
                  Yes, reset
                </button>
              </div>
            </>
          )}
          {state.step === 'resetting' && <p className="reset-strip__text">Resetting, voiding holds at PayPal...</p>}
          {state.step === 'done' && (
            <>
              <p className="reset-strip__text">{describeSummary(state.summary)}</p>
              <div className="reset-strip__actions">
                <button type="button" className="secondary-button" onClick={controls.dismiss}>
                  OK
                </button>
              </div>
            </>
          )}
          {state.step === 'error' && (
            <>
              <p className="reset-strip__text" role="alert">
                Couldn't reset: {state.message}
              </p>
              <div className="reset-strip__actions">
                <button type="button" className="secondary-button" onClick={controls.dismiss}>
                  OK
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
