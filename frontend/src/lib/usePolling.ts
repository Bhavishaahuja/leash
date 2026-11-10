import { useCallback, useEffect, useRef, useState } from 'react'
import type { AsyncState } from './useAsync'

export interface PollingState<T> {
  state: AsyncState<T>
  // true when the latest poll failed but we still have older data on screen
  stale: boolean
  // fetch right now instead of waiting for the next tick, e.g. after a reset
  refresh: () => void
}

// Like useAsync, but keeps re-running the loader every `intervalMs` so the page stays live.
// A failed poll after a good one keeps the old data on screen and just flags it stale, so one
// network blip doesn't blank the dashboard in the middle of a demo.
export function usePolling<T>(load: () => Promise<T>, intervalMs: number): PollingState<T> {
  const [state, setState] = useState<AsyncState<T>>({ status: 'loading' })
  const [stale, setStale] = useState(false)
  // JSON of the last data we showed, so an unchanged poll doesn't re-render the grid
  const lastJson = useRef<string | null>(null)
  // bumped to force an immediate fetch
  const [kick, setKick] = useState(0)

  const refresh = useCallback(() => setKick((value) => value + 1), [])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    // one request at a time. a slow API shouldn't pile up overlapping polls. it lives in here, not
    // in a ref, so a refresh() never waits on a request the old effect started
    let inFlight = false

    async function tick() {
      // a request already out will schedule the next tick itself when it lands
      if (inFlight) {
        return
      }
      // no point polling a tab nobody is looking at, the visibility listener picks it back up
      if (document.hidden) {
        schedule()
        return
      }
      inFlight = true
      try {
        const data = await load()
        if (cancelled) {
          return
        }
        const json = JSON.stringify(data)
        if (json !== lastJson.current) {
          lastJson.current = json
          setState({ status: 'ready', data })
        }
        setStale(false)
      } catch (caught) {
        if (cancelled) {
          return
        }
        const error = caught instanceof Error ? caught : new Error(String(caught))
        // only show the error page if we never had data. otherwise keep what's there
        if (lastJson.current === null) {
          setState({ status: 'error', error })
        } else {
          setStale(true)
        }
      } finally {
        inFlight = false
      }
      schedule()
    }

    function schedule() {
      if (!cancelled) {
        timer = setTimeout(tick, intervalMs)
      }
    }

    function handleVisible() {
      // coming back to the tab: fetch now rather than wait out the interval
      if (!document.hidden) {
        clearTimeout(timer)
        void tick()
      }
    }

    void tick()
    document.addEventListener('visibilitychange', handleVisible)
    return () => {
      cancelled = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', handleVisible)
    }
  }, [load, intervalMs, kick])

  return { state, stale, refresh }
}
