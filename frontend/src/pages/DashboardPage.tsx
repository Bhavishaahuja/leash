import {
  CellStyleModule,
  ClientSideRowModelModule,
  ExternalFilterModule,
  ModuleRegistry,
  QuickFilterModule,
  RowSelectionModule,
  RowStyleModule,
  ValidationModule,
  themeQuartz,
  type ColDef,
  type IRowNode,
  type RowClickedEvent,
} from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { listOrders } from '../api'
import { OrderPanel } from '../components/OrderPanel'
import {
  DecideTimeCell,
  DecisionCell,
  OrderCell,
  ReasonCell,
  RiskCell,
  StatusCell,
  TotalCell,
} from '../components/orderCells'
import { formatTime, summarizeItems } from '../lib/format'
import { DECISION_LABELS, decisionKind, decisionMs, displayStatus } from '../lib/orderState'
import { useAsync } from '../lib/useAsync'
import type { Order } from '../types'

// only the grid features we use, so the bundle stays small. ValidationModule only runs in dev
// and warns in the console when a grid option needs a module that isn't registered here
const gridModules = [
  ClientSideRowModelModule,
  QuickFilterModule,
  ExternalFilterModule,
  RowSelectionModule,
  RowStyleModule,
  CellStyleModule,
]
if (import.meta.env.DEV) {
  gridModules.push(ValidationModule)
}
ModuleRegistry.registerModules(gridModules)

// Quartz, pulled toward the app's palette so the grid doesn't look bolted on
const gridTheme = themeQuartz.withParams({
  fontFamily: 'inherit',
  fontSize: 14,
  foregroundColor: '#1d2a2a',
  backgroundColor: '#ffffff',
  headerBackgroundColor: '#efede6',
  headerTextColor: '#5c6a68',
  headerFontWeight: 600,
  headerFontSize: 12,
  borderColor: '#e2dfd6',
  rowHoverColor: '#f5f4ef',
  selectedRowBackgroundColor: '#e3eee9',
  accentColor: '#2c5e52',
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowBorder: { color: '#e2dfd6' },
  columnBorder: false,
  spacing: 7,
})

type Filter = 'all' | 'needs_review' | 'auto' | 'human'

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'needs_review', label: 'Needs review' },
  { id: 'auto', label: 'Auto-captured' },
  { id: 'human', label: 'Human decided' },
]

function matchesFilter(order: Order, filter: Filter): boolean {
  if (filter === 'needs_review') {
    return order.review_state === 'needs_review'
  }
  if (filter === 'auto') {
    return order.review_state === 'auto_approved'
  }
  if (filter === 'human') {
    return order.review_state === 'approved' || order.review_state === 'rejected'
  }
  return true
}

// how urgent a status is, so sorting the Status column puts work-to-do on top
const STATUS_ORDER = ['needs_review', 'created', 'authorized', 'failed', 'voided', 'captured']

// stable loader identity so useAsync fetches once
const loadOrders = () => listOrders()

// Merchant view: every agent order, what the check said, and what the human did about it.
export function DashboardPage() {
  const orders = useAsync(loadOrders)
  const gridRef = useRef<AgGridReact<Order>>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const columnDefs = useMemo<ColDef<Order>[]>(
    () => [
      {
        headerName: 'Time',
        field: 'created_at',
        valueFormatter: (params) => (params.value ? formatTime(params.value) : ''),
        sort: 'desc',
        width: 120,
        cellClass: 'muted',
      },
      {
        headerName: 'Order',
        colId: 'order',
        // what quick search and sorting see: the item, the buyer and the order number
        valueGetter: (params) =>
          params.data ? summarizeItems(params.data.items) + ' ' + params.data.mandate.buyer_label + ' #' + params.data.id : '',
        cellRenderer: OrderCell,
        minWidth: 200,
        flex: 1.2,
      },
      {
        headerName: 'Total',
        field: 'total',
        valueGetter: (params) => (params.data ? Number(params.data.total) : null),
        cellRenderer: TotalCell,
        type: 'rightAligned',
        width: 100,
      },
      {
        headerName: 'Status',
        colId: 'status',
        valueGetter: (params) => (params.data ? displayStatus(params.data) : ''),
        comparator: (a: string, b: string) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b),
        cellRenderer: StatusCell,
        width: 160,
      },
      {
        headerName: 'Risk',
        colId: 'risk',
        valueGetter: (params) => params.data?.intent_check?.risk_score ?? null,
        cellRenderer: RiskCell,
        width: 125,
      },
      {
        headerName: 'AI reason',
        colId: 'reason',
        valueGetter: (params) => params.data?.intent_check?.reason ?? '',
        cellRenderer: ReasonCell,
        minWidth: 240,
        flex: 2,
        sortable: false,
      },
      {
        headerName: 'Decision',
        colId: 'decision',
        valueGetter: (params) => (params.data ? DECISION_LABELS[decisionKind(params.data)] : ''),
        cellRenderer: DecisionCell,
        width: 150,
      },
      {
        headerName: 'Time to decide',
        colId: 'decision_ms',
        valueGetter: (params) => (params.data ? decisionMs(params.data) : null),
        cellRenderer: DecideTimeCell,
        width: 140,
      },
    ],
    [],
  )

  const defaultColDef = useMemo<ColDef<Order>>(
    () => ({
      sortable: true,
      resizable: true,
      suppressMovable: true,
    }),
    [],
  )

  // the grid calls these on every filter pass, so they read the latest filter via deps
  const isExternalFilterPresent = useCallback(() => filter !== 'all', [filter])
  const doesExternalFilterPass = useCallback(
    (node: IRowNode<Order>) => (node.data ? matchesFilter(node.data, filter) : true),
    [filter],
  )

  useEffect(() => {
    // the grid doesn't watch our filter state, so tell it to re-run the external filter
    gridRef.current?.api?.onFilterChanged()
  }, [filter])

  const openOrder = useCallback((event: RowClickedEvent<Order>) => {
    if (event.data) {
      setSelectedId(event.data.id)
    }
  }, [])

  const closePanel = useCallback(() => {
    setSelectedId(null)
    gridRef.current?.api.deselectAll()
  }, [])

  const allOrders = orders.status === 'ready' ? orders.data : []
  const selectedOrder = allOrders.find((order) => order.id === selectedId) ?? null

  let needsReviewCount = 0
  for (const order of allOrders) {
    if (order.review_state === 'needs_review') {
      needsReviewCount += 1
    }
  }

  return (
    <section>
      <div className="page__head">
        <div>
          <h1 className="page__title">Agent orders</h1>
          <p className="page__lede">
            Payments are held until the order matches what the shopper asked for.
          </p>
        </div>
      </div>

      <div className="card">
        {orders.status === 'loading' && <p className="state">Loading orders...</p>}
        {orders.status === 'error' && (
          <p className="state state--error">Couldn't load orders: {orders.error.message}</p>
        )}
        {orders.status === 'ready' && orders.data.length === 0 && (
          <p className="state">No agent orders yet.</p>
        )}
        {orders.status === 'ready' && orders.data.length > 0 && (
          <>
            <div className="grid-toolbar">
              <div className="segmented" role="group" aria-label="Filter orders">
                {FILTERS.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    className="segmented__button"
                    aria-pressed={filter === option.id}
                    onClick={() => setFilter(option.id)}
                  >
                    {option.label}
                    {option.id === 'needs_review' && needsReviewCount > 0 && (
                      <span className="segmented__count">{needsReviewCount}</span>
                    )}
                  </button>
                ))}
              </div>
              <input
                type="search"
                className="search"
                placeholder="Search item, buyer or order #"
                aria-label="Search orders"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <div className="grid-wrap">
              <AgGridReact<Order>
                ref={gridRef}
                theme={gridTheme}
                rowData={orders.data}
                columnDefs={columnDefs}
                defaultColDef={defaultColDef}
                getRowId={(params) => String(params.data.id)}
                rowHeight={60}
                headerHeight={40}
                domLayout="autoHeight"
                quickFilterText={search}
                isExternalFilterPresent={isExternalFilterPresent}
                doesExternalFilterPass={doesExternalFilterPass}
                rowSelection={{ mode: 'singleRow', checkboxes: false, enableClickSelection: true }}
                rowClassRules={{ 'row--needs-review': (params) => params.data?.review_state === 'needs_review' }}
                onRowClicked={openOrder}
                onCellKeyDown={(event) => {
                  // Enter on a focused row opens it, same as a click
                  const keyEvent = event.event as KeyboardEvent | null | undefined
                  if (keyEvent?.key === 'Enter' && event.data) {
                    event.node.setSelected(true)
                    setSelectedId(event.data.id)
                  }
                }}
                overlayNoRowsTemplate='<span class="muted">No orders match this filter.</span>'
              />
            </div>
          </>
        )}
      </div>

      {selectedOrder && <OrderPanel order={selectedOrder} onClose={closePanel} />}
    </section>
  )
}
