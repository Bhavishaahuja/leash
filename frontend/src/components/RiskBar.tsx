// Risk score from the intent check, 0 to 100. Higher means more likely the human
// didn't want this. Used in the grid and in the side panel.

function riskLevel(score: number): 'low' | 'medium' | 'high' {
  if (score < 34) {
    return 'low'
  }
  if (score < 67) {
    return 'medium'
  }
  return 'high'
}

export function RiskBar({ score }: { score: number | null }) {
  if (score === null) {
    return <span className="muted">Not scored</span>
  }

  const level = riskLevel(score)
  return (
    <span className={'risk risk--' + level}>
      <span
        className="risk__track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={score}
        aria-label={'Risk ' + score + ' out of 100, ' + level}
      >
        <span className="risk__fill" style={{ width: Math.max(score, 3) + '%' }} />
      </span>
      <span className="risk__value">{score}</span>
    </span>
  )
}
