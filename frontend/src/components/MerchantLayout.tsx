import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { SESSION_EXPIRED_EVENT, USE_MOCKS, hasSession, logOut } from '../api'
import { LeashMark } from './LeashMark'
import { LoginForm } from './LoginForm'

type SessionState = 'in' | 'out' | 'expired'

// Shell for the merchant's screens. The human's confirm page skips this on purpose:
// on a phone they should see only the one decision in front of them.
//
// It's also the one login gate for every merchant page: no session token, no page. In mock
// mode hasSession() is always true, so a fresh clone still works with no setup.
export function MerchantLayout() {
  const [session, setSession] = useState<SessionState>(() => (hasSession() ? 'in' : 'out'))

  useEffect(() => {
    // api.ts fires this on any 401 from a merchant route, from whichever page made the call
    function handleExpired() {
      setSession('expired')
    }
    window.addEventListener(SESSION_EXPIRED_EVENT, handleExpired)
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, handleExpired)
  }, [])

  function handleLogOut() {
    logOut()
    setSession('out')
  }

  const loggedIn = session === 'in'

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar__inner">
          <NavLink to="/dashboard" className="brand">
            <LeashMark />
            <span className="brand__name">Leash</span>
          </NavLink>
          <span className="topbar__store">Fernwood Supply Co</span>
          <nav className="topbar__nav" aria-label="Main">
            {loggedIn && (
              <>
                <NavLink to="/dashboard" className="topbar__link">
                  Orders
                </NavLink>
                <NavLink to="/place-order" className="topbar__link">
                  Place order
                </NavLink>
              </>
            )}
            {loggedIn && !USE_MOCKS && (
              <button type="button" className="topbar__link topbar__logout" onClick={handleLogOut}>
                Log out
              </button>
            )}
          </nav>
          {USE_MOCKS && <span className="mock-flag">Sample data</span>}
        </div>
      </header>
      <main className="page">
        {loggedIn ? (
          <Outlet />
        ) : (
          <LoginForm expired={session === 'expired'} onLoggedIn={() => setSession('in')} />
        )}
      </main>
    </div>
  )
}
