// The numbers row above the orders grid, from GET /stats. The last tile is the rubber-stamp
// watch: the Approval Trap made visible, so the merchant can see when approvals stopped being looks.

import { formatDuration } from '../lib/format'
import type { AsyncState } from '../lib/useAsync'
import type { Stats } from '../types'

// fast approvals in a row that turn the watch on. rubber_stamp.STREAK_TO_TRIGGER on the backend,
// the API doesn't send it, see docs/api.md
const STREAK_TO_TRIGGER = 3

interface StatsTilesProps {
  stats: AsyncState<Stats>
}

export function StatsTiles({ stats }: StatsTilesProps) {
  if (stats.status === 'loading') {
    return <div className="stats stats--loading" aria-hidden="true" />
  }
  if (stats.status === 'error') {
    // the grid below still works without these, so a quiet note is enough
    return <p className="state state--error stats__error">Couldn't load the numbers: {stats.error.message}</p>
  }

  const data = stats.data
  const tiles: { label: string; value: string; note?: string }[] = [
    { label: 'Orders', value: String(data.total_orders) },
    { label: 'Auto-captured', value: String(data.auto_captured), note: 'no human needed' },
    { label: 'Needs review', value: String(data.needs_review), note: 'money on hold' },
    { label: 'Approved', value: String(data.approved), note: 'by a human' },
    { label: 'Voided', value: String(data.voided), note: 'never charged' },
    {
      label: 'Median time to decide',
      value: data.median_decision_ms === null ? 'None yet' : formatDuration(data.median_decision_ms),
    },
  ]

  return (
    <div className="stats">
      {tiles.map((tile) => (
        <div key={tile.label} className="stat">
          <p className="stat__label">{tile.label}</p>
          <p className="stat__value">{tile.value}</p>
          {tile.note && <p className="stat__note">{tile.note}</p>}
        </div>
      ))}
      <WatchTile stats={data} />
    </div>
  )
}

function WatchTile({ stats }: { stats: Stats }) {
  const active = stats.rubber_stamp_active
  const threshold = formatDuration(stats.fast_threshold_ms)
  // the streak can run past 3, the dots only need to show progress toward turning on
  const filled = Math.min(stats.fast_streak, STREAK_TO_TRIGGER)

  const dots = []
  for (let index = 0; index < STREAK_TO_TRIGGER; index += 1) {
    dots.push(<span key={index} className={'watch__dot' + (index < filled ? ' watch__dot--on' : '')} />)
  }

  return (
    <div className={'stat stat--watch' + (active ? ' stat--watch-on' : '')}>
      <p className="stat__label">Rubber-stamp watch</p>
      <p className="stat__value watch__state">
        <span className="watch__icon" aria-hidden="true">
          {active ? '!' : '✓'}
        </span>
        {active ? 'On' : 'Off'}
      </p>
      <div className="watch__streak" role="img" aria-label={filled + ' of ' + STREAK_TO_TRIGGER + ' fast approvals in a row'}>
        {dots}
      </div>
      <p className="stat__note">
        {active
          ? 'The last ' + stats.fast_streak + ' approvals took under ' + threshold + '. Leash now asks less often, but more pointedly.'
          : filled + ' of ' + STREAK_TO_TRIGGER + ' approvals in a row under ' + threshold + '. Turns on at ' + STREAK_TO_TRIGGER + '.'}
      </p>
    </div>
  )
}
