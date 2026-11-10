import {
  CellStyleModule,
  ClientSideRowModelModule,
  ExternalFilterModule,
  ModuleRegistry,
  QuickFilterModule,
  RowApiModule,
  RowSelectionModule,
  RowStyleModule,
  ValidationModule,
  themeQuartz,
  type ColDef,
  type IRowNode,
  type RowClassParams,
  type RowClickedEvent,
  type RowDataUpdatedEvent,
} from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DEMO_RESET_EVENT, getStats, listOrders } from '../api'
import { OrderPanel } from '../components/OrderPanel'
import { StatsTiles } from '../components/StatsTiles'
import {
  DecideTimeCell,
  DecisionCell,
  OrderCell,
  ReasonCell,
  RiskCell,
  StatusCell,
  TotalCell,
} from '../components/orderCells'
import { buyerName, formatTime, summarizeItems } from '../lib/format'
import { DECISION_LABELS, decisionKind, decisionMs, displayStatus } from '../lib/orderState'
import { usePolling } from '../lib/usePolling'
import type { Order } from '../types'

// only the grid features we use, so the bundle stays small. ValidationModule only runs in dev
// and warns in the console when a grid option needs a module that isn't registered here
const gridModules = [
  ClientSideRowModelModule,
  QuickFilterModule,
  ExternalFilterModule,
  // forEachNode and redrawRows, for the flash on rows that just changed
  RowApiModule,
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

// below this the grid only has room for about three columns, so it keeps the ones a merchant on a
// phone actually needs: what was ordered, whether money is held, and how much
const NARROW_SCREEN = '(max-width: 640px)'

function useNarrowScreen(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW_SCREEN).matches)
  useEffect(() => {
    const query = window.matchMedia(NARROW_SCREEN)
    const update = () => setNarrow(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return narrow
}

// stable loader identity so the poll doesn't restart on every render
const loadOrders = () => listOrders()
const loadStats = () => getStats()

// how often the grid asks for fresh orders. fast enough that an approve on the phone shows up on the
// laptop while you're still looking, slow enough to be nothing for the API
const POLL_MS = 4000

// how long a changed row keeps its flash class. matches the row-flash animation in index.css
const FLASH_MS = 2400

// Merchant view: every agent order, what the check said, and what the human did about it.
export function DashboardPage() {
  const { state: orders, stale, refresh: refreshOrders } = usePolling(loadOrders, POLL_MS)
  const { state: stats, refresh: refreshStats } = usePolling(loadStats, POLL_MS)
  const gridRef = useRef<AgGridReact<Order>>(null)
  // each row's status as of the last poll, so we can tell which rows just changed.
  // null until the first load, which shouldn't flash anything
  const lastStatuses = useRef<Map<number, string> | null>(null)
  // rows flashing right now: order id to the status it flashes as
  const flashing = useRef<Map<number, string>>(new Map())
  const flashTimers = useRef<ReturnType<typeof setTimeout>[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const narrow = useNarrowScreen()

  const columnDefs = useMemo<ColDef<Order>[]>(() => {
    const timeColumn: ColDef<Order> = {
      headerName: 'Time',
      field: 'created_at',
      valueFormatter: (params) => (params.value ? formatTime(params.value) : ''),
      // still sorts newest first when hidden on a phone
      sort: 'desc',
      hide: narrow,
      width: 120,
      cellClass: 'muted',
    }
    const orderColumn: ColDef<Order> = {
      headerName: 'Order',
      colId: 'order',
      // what quick search and sorting see: the item, the buyer and the order number
      valueGetter: (params) =>
        params.data ? summarizeItems(params.data.items) + ' ' + buyerName(params.data.mandate.buyer_label) + ' #' + params.data.id : '',
      cellRenderer: OrderCell,
      minWidth: narrow ? 120 : 200,
      flex: 1.2,
    }
    const totalColumn: ColDef<Order> = {
      headerName: 'Total',
      field: 'total',
      valueGetter: (params) => (params.data ? Number(params.data.total) : null),
      cellRenderer: TotalCell,
      type: 'rightAligned',
      width: narrow ? 90 : 100,
    }
    const statusColumn: ColDef<Order> = {
      headerName: 'Status',
      colId: 'status',
      valueGetter: (params) => (params.data ? displayStatus(params.data) : ''),
      comparator: (a: string, b: string) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b),
      cellRenderer: StatusCell,
      // wide enough for the longest chip, "Waiting on buyer"
      width: narrow ? 150 : 160,
    }

    // on a phone, status comes right after the order so you can see what's held without scrolling
    let leadColumns = [timeColumn, orderColumn, totalColumn, statusColumn]
    if (narrow) {
      leadColumns = [timeColumn, orderColumn, statusColumn, totalColumn]
    }

    return [
      ...leadColumns,
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
    ]
  }, [narrow])

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

  useEffect(() => {
    // after a reset from the top bar, don't make the merchant wait for the next poll
    function handleReset() {
      refreshOrders()
      refreshStats()
    }
    window.addEventListener(DEMO_RESET_EVENT, handleReset)
    return () => window.removeEventListener(DEMO_RESET_EVENT, handleReset)
  }, [refreshOrders, refreshStats])

  useEffect(() => {
    const timers = flashTimers.current
    return () => {
      for (const timer of timers) {
        clearTimeout(timer)
      }
    }
  }, [])

  // runs after the grid has applied a poll. getRowId means it only touched rows that changed, so
  // selection and scroll stay put. here we just find which rows are new or changed status and
  // flash them in their new color: amber when a hold lands, green on capture, gray on void
  const onRowDataUpdated = useCallback((event: RowDataUpdatedEvent<Order>) => {
    const nextStatuses = new Map<number, string>()
    const changedNodes: IRowNode<Order>[] = []
    event.api.forEachNode((node) => {
      if (!node.data) {
        return
      }
      const status = displayStatus(node.data)
      nextStatuses.set(node.data.id, status)
      const before = lastStatuses.current?.get(node.data.id)
      if (lastStatuses.current !== null && before !== status) {
        flashing.current.set(node.data.id, status)
        changedNodes.push(node)
      }
    })
    lastStatuses.current = nextStatuses
    if (changedNodes.length === 0) {
      return
    }

    // redraw rebuilds the row element, so the CSS animation plays from the start
    event.api.redrawRows({ rowNodes: changedNodes })
    const timer = setTimeout(() => {
      for (const node of changedNodes) {
        if (node.data) {
          flashing.current.delete(node.data.id)
        }
      }
      // gridRef, not event.api: the grid may have been rebuilt since
      gridRef.current?.api?.redrawRows({ rowNodes: changedNodes })
    }, FLASH_MS)
    flashTimers.current.push(timer)
  }, [])

  const getRowClass = useCallback((params: RowClassParams<Order>) => {
    const status = params.data ? flashing.current.get(params.data.id) : undefined
    return status ? 'row--flash row--flash-' + status : undefined
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

      <StatsTiles stats={stats} />

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
              <div className="grid-toolbar__end">
                <span className={'live' + (stale ? ' live--stale' : '')} role="status">
                  <span className="live__dot" aria-hidden="true" />
                  {stale ? 'Reconnecting...' : 'Live'}
                </span>
                <input
                  type="search"
                  className="search"
                  placeholder="Search item, buyer or order #"
                  aria-label="Search orders"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
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
                getRowClass={getRowClass}
                onRowDataUpdated={onRowDataUpdated}
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
