import { useCallback, useState } from 'react'
import {
  downloadDisputeEvidencePdf,
  getDisputeEvidence,
  listDisputes,
  redraftDisputeEvidence,
  submitDisputeEvidence,
  syncDisputes,
} from '../api'
import { describeItem, formatDateTime, formatDuration, formatMoney } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import type { Dispute, DisputeEvidence, DisputeSyncResult } from '../types'

// Merchant view of PayPal disputes on captured orders. Leash answers with its own records: the
// buyer's mandate, what was ordered, the check, and who released the money. Filing is once only,
// PayPal won't take a second copy, so the button asks first.

// PayPal's reason codes, in words. anything new from PayPal shows as its raw code
const REASONS: Record<string, string> = {
  MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: 'Not as described',
  MERCHANDISE_OR_SERVICE_NOT_RECEIVED: 'Not received',
  UNAUTHORISED: 'Unauthorized',
  CREDIT_NOT_PROCESSED: 'Credit not processed',
  DUPLICATE_TRANSACTION: 'Duplicate transaction',
  INCORRECT_AMOUNT: 'Incorrect amount',
  PAYMENT_BY_OTHER_MEANS: 'Paid another way',
  CANCELED_RECURRING_BILLING: 'Canceled recurring billing',
  OTHER: 'Other',
}

const STATUSES: Record<string, string> = {
  WAITING_FOR_SELLER_RESPONSE: 'Waiting on you',
  WAITING_FOR_BUYER_RESPONSE: 'Waiting on buyer',
  UNDER_REVIEW: 'PayPal reviewing',
  RESOLVED: 'Resolved',
  OPEN: 'Open',
  OTHER: 'Other',
}

function reasonLabel(reason: string | null): string {
  if (!reason) {
    return 'Unknown reason'
  }
  return REASONS[reason] ?? reason
}

function statusLabel(status: string | null): string {
  if (!status) {
    return 'Unknown'
  }
  return STATUSES[status] ?? status
}

// reuses the order chips' colors: orange when it's on the merchant, green once answered
function chipTone(dispute: Dispute): string {
  if (dispute.evidence_submitted) {
    return 'captured'
  }
  if (dispute.status === 'WAITING_FOR_SELLER_RESPONSE') {
    return 'needs_review'
  }
  if (dispute.status === 'RESOLVED') {
    return 'voided'
  }
  return 'authorized'
}

// hands a fetched file to the browser as a download
function saveFile(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  document.body.appendChild(link)
  link.click()
  link.remove()
  // give the browser a moment to start the download before the URL goes away
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function daysLeft(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now()
  if (ms <= 0) {
    return 'past due'
  }
  const days = Math.floor(ms / (24 * 3600 * 1000))
  if (days === 0) {
    return 'due today'
  }
  return days === 1 ? '1 day left' : days + ' days left'
}

export function DisputesPage() {
  // bumping this reloads the list after a sync or a filing
  const [version, setVersion] = useState(0)
  const loadDisputes = useCallback(() => {
    void version
    return listDisputes()
  }, [version])
  const disputes = useAsync(loadDisputes)

  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [syncResult, setSyncResult] = useState<DisputeSyncResult | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)

  async function runSync() {
    setSyncing(true)
    setSyncError(null)
    try {
      setSyncResult(await syncDisputes())
      setVersion((value) => value + 1)
    } catch (caught) {
      setSyncError(caught instanceof Error ? caught.message : 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  return (
    <section className="disputes">
      <div className="page__head">
        <div>
          <h1 className="page__title">Disputes</h1>
          <p className="page__lede">
            When a buyer disputes a captured order, Leash answers with what they asked for and who released the money.
          </p>
        </div>
        <button type="button" className="secondary-button" onClick={runSync} disabled={syncing}>
          {syncing ? 'Checking PayPal...' : 'Check PayPal for disputes'}
        </button>
      </div>

      {syncError && (
        <p className="state state--error" role="alert">
          Couldn't check PayPal: {syncError}
        </p>
      )}
      {syncResult && <SyncSummary result={syncResult} />}

      {disputes.status === 'loading' && (
        <div className="card">
          <p className="state">Loading disputes...</p>
        </div>
      )}
      {disputes.status === 'error' && (
        <div className="card">
          <p className="state state--error">Couldn't load disputes: {disputes.error.message}</p>
        </div>
      )}
      {disputes.status === 'ready' && disputes.data.length === 0 && (
        <div className="card">
          <div className="state disputes-empty">
            <p className="disputes-empty__title">No disputes on record</p>
            <p>
              That's good news. When a buyer disputes a captured order in PayPal, press Check PayPal for disputes to
              pull it in here. Leash then answers with what the buyer asked for, how the order was checked and who
              released the money.
            </p>
            <p className="disputes-empty__note">
              Testing in the sandbox? Open a dispute as the sandbox buyer on an order Leash captured, then check
              again. A new dispute can take a minute or two to show up.
            </p>
          </div>
        </div>
      )}
      {disputes.status === 'ready' && disputes.data.length > 0 && (
        <ul className="dispute-list">
          {disputes.data.map((dispute) => (
            <li key={dispute.id}>
              <button
                type="button"
                className={'card dispute-row' + (dispute.id === selectedId ? ' dispute-row--selected' : '')}
                onClick={() => setSelectedId(dispute.id === selectedId ? null : dispute.id)}
                aria-expanded={dispute.id === selectedId}
              >
                <span className="dispute-row__main">
                  <span className="dispute-row__title">{reasonLabel(dispute.reason)}</span>
                  <span className="muted">
                    <span className="mono">{dispute.paypal_dispute_id}</span> on order #{dispute.order_id}
                  </span>
                </span>
                <span className="dispute-row__side">
                  <span className={'chip chip--' + chipTone(dispute)}>
                    <span className="chip__dot" aria-hidden="true" />
                    {dispute.evidence_submitted ? 'Evidence filed' : statusLabel(dispute.status)}
                  </span>
                  {!dispute.evidence_submitted && dispute.seller_response_due_at && (
                    <span className="dispute-row__due">{daysLeft(dispute.seller_response_due_at)}</span>
                  )}
                </span>
              </button>
              {dispute.id === selectedId && (
                <EvidencePanel key={dispute.id} disputeId={dispute.id} onFiled={() => setVersion((value) => value + 1)} />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function SyncSummary({ result }: { result: DisputeSyncResult }) {
  const checked = result.orders_checked === 1 ? '1 captured order' : result.orders_checked + ' captured orders'
  let found = 'no disputes'
  if (result.disputes_found === 1) {
    found = '1 dispute'
  } else if (result.disputes_found > 1) {
    found = result.disputes_found + ' disputes'
  }
  return (
    <div className="sync-summary">
      <p>
        Checked {checked}, found {found}
        {result.created > 0 ? ' (' + result.created + ' new)' : ''}.
      </p>
      {result.errors.map((error) => (
        <p key={error.order_id} className="form-error">
          Order #{error.order_id}: {error.message}
        </p>
      ))}
    </div>
  )
}

type PanelState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; evidence: DisputeEvidence }

function EvidencePanel({ disputeId, onFiled }: { disputeId: number; onFiled: () => void }) {
  const loadEvidence = useCallback(() => getDisputeEvidence(disputeId), [disputeId])
  const first = useAsync(loadEvidence)
  // set after a redraft or a filing, so the panel shows the newest answer without refetching
  const [latest, setLatest] = useState<DisputeEvidence | null>(null)
  const [busy, setBusy] = useState<'redraft' | 'submit' | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  let state: PanelState = { status: 'loading' }
  if (latest) {
    state = { status: 'ready', evidence: latest }
  } else if (first.status === 'ready') {
    state = { status: 'ready', evidence: first.data }
  } else if (first.status === 'error') {
    state = { status: 'error', message: first.error.message }
  }

  if (state.status === 'loading') {
    return (
      <div className="card dispute-panel">
        <p className="state">Building the evidence and drafting a summary. The first look takes a few seconds.</p>
      </div>
    )
  }
  if (state.status === 'error') {
    return (
      <div className="card dispute-panel">
        <p className="state state--error">Couldn't build the evidence: {state.message}</p>
      </div>
    )
  }

  const { dispute, packet, summary } = state.evidence
  const mandate = packet.mandate
  const order = packet.order
  const check = packet.intent_check
  const decision = packet.decision
  const canAct = !dispute.evidence_submitted && busy === null

  async function redraft() {
    setBusy('redraft')
    setActionError(null)
    setConfirming(false)
    try {
      setLatest(await redraftDisputeEvidence(disputeId))
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : 'Redraft failed')
    } finally {
      setBusy(null)
    }
  }

  async function submit() {
    if (state.status !== 'ready') {
      return
    }
    const current = state.evidence
    setBusy('submit')
    setActionError(null)
    try {
      const filed = await submitDisputeEvidence(disputeId)
      setLatest({ ...current, dispute: filed })
      onFiled()
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : 'Filing failed')
    } finally {
      setBusy(null)
      setConfirming(false)
    }
  }

  async function downloadPdf() {
    setDownloading(true)
    setDownloadError(null)
    try {
      const file = await downloadDisputeEvidencePdf(disputeId)
      saveFile(file.blob, file.fileName)
    } catch (caught) {
      setDownloadError(caught instanceof Error ? caught.message : 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  let capText = 'No cap'
  if (mandate.max_price !== null) {
    capText = (mandate.max_price_inclusive ? 'Up to ' : 'Under ') + formatMoney(mandate.max_price, order.currency)
  }

  return (
    <div className="card dispute-panel">
      <p className="eyebrow">1. What the buyer asked for, before the order</p>
      <blockquote className="dispute-panel__quote">"{mandate.raw_request}"</blockquote>
      <dl className="dispute-facts">
        <div>
          <dt>Cap</dt>
          <dd>{capText}</dd>
        </div>
        <div>
          <dt>Store</dt>
          <dd>{mandate.merchant ?? 'Any store'}</dd>
        </div>
        <div>
          <dt>Set</dt>
          <dd>{formatDateTime(mandate.created_at)}</dd>
        </div>
      </dl>

      <p className="eyebrow">2. What the agent ordered</p>
      <ul className="dispute-panel__items">
        {order.items.map((item, index) => (
          <li key={index}>
            <span>
              {item.quantity} x {describeItem(item)}
            </span>
            <span>{formatMoney(item.unit_price, order.currency)}</span>
          </li>
        ))}
      </ul>
      <p className="muted">
        Order #{order.id} at {order.store}, total {formatMoney(order.total, order.currency)}. PayPal capture{' '}
        <span className="mono">{order.capture_id ?? 'none'}</span>
      </p>

      <p className="eyebrow">3. Checked against the request, before any money moved</p>
      {check ? (
        <>
          <p className={'dispute-panel__reason verdict-text--' + check.verdict}>{check.reason}</p>
          <p className="muted">
            Verdict {check.verdict}, risk {check.risk_score ?? 'unknown'} of 100, by{' '}
            {check.checked_by === 'ai' ? 'Claude' : 'the hard rules'}
            {check.watch_mode === 'sharp' ? ', rubber-stamp watch on' : ''}
          </p>
        </>
      ) : (
        <p className="muted">No check result on record.</p>
      )}

      <p className="eyebrow">4. Who released the money</p>
      <p>
        {decision.released_by === 'automatic capture' && 'Captured automatically: the order matched the request.'}
        {decision.released_by === 'human review' &&
          'The buyer approved it on their review link' +
            (decision.decision_ms !== null ? ', after ' + formatDuration(decision.decision_ms) + ' on the page' : '') +
            '.'}
        {decision.released_by === null && 'No decision on record.'}
      </p>
      {decision.decided_at && <p className="muted">Decided {formatDateTime(decision.decided_at)}</p>}

      <p className="eyebrow">Summary for the PayPal reviewer</p>
      <p className="dispute-panel__summary">{summary || 'No summary saved.'}</p>
      <p className="muted">Filed as the notes, with the four parts above attached as a PDF.</p>
      <div className="dispute-panel__download">
        <button type="button" className="secondary-button" onClick={downloadPdf} disabled={downloading}>
          {downloading ? 'Preparing PDF...' : 'Download PDF'}
        </button>
        <span className="muted">
          {dispute.evidence_submitted ? 'The same file PayPal has.' : 'The same file PayPal gets when you file.'}
        </span>
      </div>
      {downloadError && (
        <p className="form-error" role="alert">
          Couldn't download the PDF: {downloadError}
        </p>
      )}

      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      {dispute.evidence_submitted ? (
        <p className="dispute-panel__filed">
          Filed with PayPal as {dispute.evidence_type ?? 'evidence'}
          {dispute.evidence_submitted_at ? ' on ' + formatDateTime(dispute.evidence_submitted_at) : ''}.
        </p>
      ) : confirming ? (
        <div className="dispute-panel__actions">
          <p className="dispute-panel__warn">PayPal takes evidence once. Send this summary and PDF now?</p>
          <button type="button" className="secondary-button" onClick={() => setConfirming(false)} disabled={busy !== null}>
            Cancel
          </button>
          <button type="button" className="primary-button" onClick={submit} disabled={busy !== null}>
            {busy === 'submit' ? 'Filing...' : 'Yes, file it'}
          </button>
        </div>
      ) : (
        <div className="dispute-panel__actions">
          <button type="button" className="secondary-button" onClick={redraft} disabled={!canAct}>
            {busy === 'redraft' ? 'Redrafting...' : 'Redraft summary'}
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => setConfirming(true)}
            disabled={!canAct || dispute.status !== 'WAITING_FOR_SELLER_RESPONSE'}
          >
            File with PayPal
          </button>
          {dispute.status !== 'WAITING_FOR_SELLER_RESPONSE' && (
            <p className="muted">PayPal only takes evidence while the dispute is waiting on you.</p>
          )}
        </div>
      )}
    </div>
  )
}
