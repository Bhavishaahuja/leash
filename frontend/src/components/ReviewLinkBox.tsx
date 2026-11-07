// Merchant side of a human review: makes the one-time link for the confirm page and shows it
// so it can be sent to the human. Used in the dashboard's order panel and on /place-order.
//
// The link is only made when the merchant clicks. Every new link kills the old one, so making
// one on page load would break a link already sitting in someone's messages.

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { USE_MOCKS, createReviewLink } from '../api'
import { formatDateTime } from '../lib/format'
import type { ReviewLink } from '../types'

type LinkState =
  | { status: 'idle' }
  | { status: 'creating' }
  | { status: 'ready'; link: ReviewLink }
  | { status: 'error'; message: string }

function confirmUrl(token: string): string {
  return window.location.origin + '/confirm/' + encodeURIComponent(token)
}

export function ReviewLinkBox({ orderId }: { orderId: number }) {
  const [state, setState] = useState<LinkState>({ status: 'idle' })
  const [copied, setCopied] = useState(false)
  const copiedTimer = useRef<number | undefined>(undefined)

  useEffect(() => {
    return () => window.clearTimeout(copiedTimer.current)
  }, [])

  async function create() {
    setState({ status: 'creating' })
    setCopied(false)
    try {
      const link = await createReviewLink(orderId)
      setState({ status: 'ready', link })
    } catch (error) {
      setState({ status: 'error', message: error instanceof Error ? error.message : String(error) })
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      window.clearTimeout(copiedTimer.current)
      copiedTimer.current = window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // clipboard blocked (http, or permissions). the link is selectable text, so it can be copied by hand
    }
  }

  if (state.status === 'ready') {
    const url = confirmUrl(state.link.review_token)
    return (
      <div className="review-link">
        <label className="review-link__label" htmlFor={'review-link-' + orderId}>
          Send this link to the shopper
        </label>
        <input
          id={'review-link-' + orderId}
          className="review-link__url"
          readOnly
          value={url}
          onFocus={(event) => event.target.select()}
        />
        <div className="review-link__actions">
          <button type="button" className="primary-button" onClick={() => copy(url)}>
            {copied ? 'Copied' : 'Copy link'}
          </button>
          {USE_MOCKS ? (
            // mock links live in this tab's memory, so open them here rather than in a new tab
            <Link className="secondary-button" to={'/confirm/' + encodeURIComponent(state.link.review_token)}>
              Open
            </Link>
          ) : (
            <a className="secondary-button" href={url} target="_blank" rel="noreferrer">
              Open
            </a>
          )}
        </div>
        <p className="review-link__note">
          Works once, until {formatDateTime(state.link.expires_at)}. Making a new link turns this one off.{' '}
          <button type="button" className="link-button" onClick={create}>
            Make a new link
          </button>
        </p>
      </div>
    )
  }

  return (
    <div className="review-link">
      <button
        type="button"
        className="primary-button review-link__create"
        onClick={create}
        disabled={state.status === 'creating'}
      >
        {state.status === 'creating' ? 'Making link...' : 'Get review link'}
      </button>
      {state.status === 'error' && (
        <p className="form-error" role="alert">
          Couldn't make a link: {state.message}
        </p>
      )}
    </div>
  )
}
