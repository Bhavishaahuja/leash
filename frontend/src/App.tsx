import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { MerchantLayout } from './components/MerchantLayout'
import { ConfirmPage } from './pages/ConfirmPage'
import { DashboardPage } from './pages/DashboardPage'
import { DisputesPage } from './pages/DisputesPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { PlaceOrderPage } from './pages/PlaceOrderPage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route element={<MerchantLayout />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/place-order" element={<PlaceOrderPage />} />
          <Route path="/disputes" element={<DisputesPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
        {/* outside the merchant layout: the human sees only their decision */}
        <Route path="/confirm/:token" element={<ConfirmPage />} />
      </Routes>
    </BrowserRouter>
  )
}
