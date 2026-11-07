import { NavLink, Outlet } from 'react-router'
import { USE_MOCKS } from '../api'
import { LeashMark } from './LeashMark'

// Shell for the merchant's screens. The human's confirm page skips this on purpose:
// on a phone they should see only the one decision in front of them.
export function MerchantLayout() {
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
            <NavLink to="/dashboard" className="topbar__link">
              Orders
            </NavLink>
            <NavLink to="/place-order" className="topbar__link">
              Place order
            </NavLink>
          </nav>
          {USE_MOCKS && <span className="mock-flag">Sample data</span>}
        </div>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </div>
  )
}
