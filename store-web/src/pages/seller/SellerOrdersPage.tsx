import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Eye,
  Filter,
  Package,
  RefreshCw,
  Search,
  Truck,
  X,
} from "lucide-react";
import { apiClient } from "../../api/client";
import type { components } from "../../api/schema";
import { formatMoney } from "../../utils/money";
import { formatISTWithRelative } from "../../utils/date";

type OrderItem = components["schemas"]["OrderResponse"];
type ShipmentDetail = components["schemas"]["ShipmentResponse"];
type RefundDetail = components["schemas"]["RefundResponse"];

export function SellerOrdersPage() {
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Detail Modal State
  const [selectedOrder, setSelectedOrder] = useState<OrderItem | null>(null);
  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [refunds, setRefunds] = useState<RefundDetail[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const fetchOrders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const queryParams: { limit: number; status?: string; search?: string } = { limit: 50 };
      if (statusFilter !== "all") {
        queryParams.status = statusFilter;
      }
      if (searchQuery.trim()) {
        queryParams.search = searchQuery.trim();
      }

      const { data, error: err } = await apiClient.GET("/api/v1/admin/orders", {
        params: { query: queryParams },
      });
      if (err) {
        throw new Error("Failed to load orders");
      }

      setOrders(data?.items || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load orders");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, searchQuery]);

  useEffect(() => {
    fetchOrders();
  }, [fetchOrders]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchOrders();
  };

  const openOrderDetail = async (ord: OrderItem) => {
    setSelectedOrder(ord);
    setLoadingDetail(true);
    setShipment(null);
    setRefunds([]);

    try {
      // 1. Fetch shipment
      try {
        const { data: shp } = await apiClient.GET("/api/v1/orders/{order_id}/shipment", {
          params: { path: { order_id: ord.order_id } },
        });
        if (shp) setShipment(shp);
      } catch {
        // may not exist
      }

      // 2. Fetch refund ledger
      try {
        const { data: rfdList } = await apiClient.GET("/api/v1/orders/{order_id}/refunds", {
          params: { path: { order_id: ord.order_id } },
        });
        if (rfdList && Array.isArray(rfdList)) setRefunds(rfdList);
      } catch {
        // may not exist
      }
    } finally {
      setLoadingDetail(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Order Management</h1>
          <p className="text-xs text-slate-400 mt-1">
            Browse store orders, inspect fulfillment tracking, and review refund ledgers.
          </p>
        </div>

        <button
          onClick={fetchOrders}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
        <form onSubmit={handleSearchSubmit} className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by Order ID, customer, item or SKU..."
            className="w-full pl-9 pr-4 py-2 bg-navy-900 border border-navy-800 rounded-xl text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-teal-500"
          />
        </form>

        <div className="flex items-center space-x-2">
          <Filter className="h-4 w-4 text-slate-400" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            data-testid="order-status-filter"
            className="bg-navy-900 border border-navy-800 text-slate-200 rounded-xl px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-teal-500"
          >
            <option value="all">All Statuses</option>
            <option value="placed">Placed</option>
            <option value="paid">Paid</option>
            <option value="shipped">Shipped</option>
            <option value="delivered">Delivered</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>
      </div>

      {/* Orders List / Table */}
      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-3 animate-pulse"
            >
              <div className="h-4 bg-navy-800 rounded w-1/3"></div>
              <div className="h-3 bg-navy-800 rounded w-2/3"></div>
            </div>
          ))}
        </div>
      ) : error ? (
        <div className="p-6 rounded-2xl border border-red-500/30 bg-red-950/20 text-center space-y-3">
          <AlertCircle className="h-6 w-6 text-red-400 mx-auto" />
          <p className="text-xs text-red-300 font-medium">{error}</p>
          <button
            onClick={fetchOrders}
            className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 text-xs font-semibold transition-colors"
          >
            Try Again
          </button>
        </div>
      ) : orders.length === 0 ? (
        <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-12 text-center text-slate-400 space-y-2">
          <Package className="h-8 w-8 mx-auto text-slate-600" />
          <p className="text-sm font-semibold text-slate-300">No orders match criteria</p>
          <p className="text-xs text-slate-500">Try modifying your search or status filter.</p>
        </div>
      ) : (
        <div className="bg-navy-900 border border-navy-800/80 rounded-2xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-navy-950/60 border-b border-navy-800 text-slate-400 font-semibold uppercase tracking-wider text-[11px]">
                <tr>
                  <th className="py-3.5 px-4">Order ID</th>
                  <th className="py-3.5 px-4">Customer</th>
                  <th className="py-3.5 px-4">Items</th>
                  <th className="py-3.5 px-4">Placed At</th>
                  <th className="py-3.5 px-4">Total</th>
                  <th className="py-3.5 px-4">Status</th>
                  <th className="py-3.5 px-4 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-800/60">
                {orders.map((ord) => (
                  <tr
                    key={ord.order_id}
                    data-testid="order-table-row"
                    className="hover:bg-navy-800/40 transition-colors cursor-pointer"
                    onClick={() => openOrderDetail(ord)}
                  >
                    <td className="py-3.5 px-4 font-mono font-bold text-teal-300">
                      {ord.order_id}
                    </td>
                    <td className="py-3.5 px-4 text-slate-300 font-medium">
                      {ord.customer_id}
                    </td>
                    <td className="py-3.5 px-4 text-slate-400">
                      {ord.items.length} {ord.items.length === 1 ? "item" : "items"}
                    </td>
                    <td className="py-3.5 px-4 text-slate-400 whitespace-nowrap">
                      {formatISTWithRelative(ord.placed_at, false)}
                    </td>
                    <td className="py-3.5 px-4 font-semibold text-white">
                      {formatMoney(ord.total_paise)}
                    </td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                          ord.status === "delivered"
                            ? "bg-teal-900/60 text-teal-300 border border-teal-500/30"
                            : ord.status === "shipped"
                            ? "bg-blue-900/60 text-blue-300 border border-blue-500/30"
                            : ord.status === "cancelled"
                            ? "bg-red-900/60 text-red-300 border border-red-500/30"
                            : "bg-amber-900/60 text-amber-300 border border-amber-500/30"
                        }`}
                      >
                        {ord.status}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          openOrderDetail(ord);
                        }}
                        className="p-1.5 rounded-lg bg-navy-800 hover:bg-navy-700 text-teal-400 transition-colors"
                        title="View Order Detail"
                      >
                        <Eye className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Order Detail Modal with Shipment & Refund Ledger */}
      {selectedOrder && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
          <div className="bg-navy-900 border border-navy-800 rounded-2xl max-w-2xl w-full p-6 shadow-2xl space-y-6 max-h-[90vh] overflow-y-auto">
            {/* Modal Header */}
            <div className="flex items-center justify-between pb-4 border-b border-navy-800">
              <div>
                <div className="flex items-center space-x-2">
                  <h3 className="text-lg font-bold text-white font-mono">
                    {selectedOrder.order_id}
                  </h3>
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                      selectedOrder.status === "delivered"
                        ? "bg-teal-900/60 text-teal-300"
                        : "bg-amber-900/60 text-amber-300"
                    }`}
                  >
                    {selectedOrder.status}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-0.5">
                  Customer: <span className="text-slate-200">{selectedOrder.customer_id}</span> &bull;{" "}
                  {formatISTWithRelative(selectedOrder.placed_at, false)}
                </p>
              </div>

              <button
                onClick={() => setSelectedOrder(null)}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-navy-800 transition-colors"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {loadingDetail ? (
              <div className="flex items-center justify-center py-12 text-slate-400 text-xs">
                <RefreshCw className="h-5 w-5 animate-spin mr-2 text-teal-400" />
                <span>Loading order detail, shipment and refunds...</span>
              </div>
            ) : (
              <div className="space-y-6">
                {/* Items */}
                <div className="space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400">
                    Order Items ({selectedOrder.items.length})
                  </h4>
                  <div className="border border-navy-800 rounded-xl divide-y divide-navy-800 bg-navy-950/40">
                    {selectedOrder.items.map((it) => (
                      <div
                        key={it.item_id}
                        className="p-3 flex items-center justify-between text-xs"
                      >
                        <div>
                          <div className="font-semibold text-slate-200">{it.name_snapshot}</div>
                          <div className="text-[10px] text-slate-400">
                            SKU: {it.sku} &bull; Qty: {it.qty}
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="font-semibold text-white">
                            {formatMoney(it.line_paid_paise)}
                          </div>
                          {it.discount_alloc_paise > 0 && (
                            <div className="text-[10px] text-teal-400">
                              disc -{formatMoney(it.discount_alloc_paise)}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Shipment Section */}
                <div className="space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center space-x-1.5">
                    <Truck className="h-3.5 w-3.5 text-teal-400" />
                    <span>Shipment Tracking</span>
                  </h4>

                  {shipment ? (
                    <div className="p-4 rounded-xl bg-navy-950/60 border border-navy-800 space-y-3">
                      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
                        <div>
                          <span className="text-[10px] text-slate-500 uppercase">Carrier</span>
                          <p className="font-semibold text-slate-200">{shipment.carrier}</p>
                        </div>
                        <div>
                          <span className="text-[10px] text-slate-500 uppercase">Tracking #</span>
                          <p className="font-mono font-semibold text-teal-300">
                            {shipment.tracking_no}
                          </p>
                        </div>
                        <div>
                          <span className="text-[10px] text-slate-500 uppercase">ETA</span>
                          <p className="font-semibold text-slate-200">
                            {shipment.eta_at
                              ? formatISTWithRelative(shipment.eta_at, false)
                              : "N/A"}
                          </p>
                        </div>
                      </div>

                      {shipment.events && shipment.events.length > 0 && (
                        <div className="pt-2 border-t border-navy-800/80 space-y-2">
                          <span className="text-[10px] font-bold text-slate-400 uppercase">
                            Events Timeline
                          </span>
                          <div className="space-y-2 pl-3 border-l-2 border-navy-700">
                            {shipment.events.map((ev) => (
                              <div key={ev.event_id} className="text-[11px] space-y-0.5">
                                <div className="text-slate-400 flex items-center space-x-1.5">
                                  <span>{formatISTWithRelative(ev.ts, true)}</span>
                                  <span>&bull;</span>
                                  <span className="text-slate-300">{ev.location}</span>
                                </div>
                                <div className="font-medium text-slate-200">{ev.note}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="p-4 rounded-xl bg-navy-950/40 border border-navy-800 text-xs text-slate-500">
                      No active carrier shipment for this order.
                    </div>
                  )}
                </div>

                {/* Refund Ledger */}
                <div className="space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400">
                    Refund Ledger ({refunds.length})
                  </h4>

                  {refunds.length === 0 ? (
                    <div className="p-4 rounded-xl bg-navy-950/40 border border-navy-800 text-xs text-slate-500">
                      No refunds have been initiated for this order.
                    </div>
                  ) : (
                    <div className="border border-navy-800 rounded-xl divide-y divide-navy-800 bg-navy-950/40">
                      {refunds.map((rf) => (
                        <div
                          key={rf.refund_id}
                          className="p-3 flex items-center justify-between text-xs"
                        >
                          <div>
                            <div className="flex items-center space-x-2">
                              <span className="font-mono font-bold text-amber-300">
                                {rf.refund_id}
                              </span>
                              <span className="text-[10px] uppercase font-mono px-1.5 py-0.2 rounded bg-navy-800 text-slate-300">
                                {rf.status}
                              </span>
                            </div>
                            <div className="text-[10px] text-slate-400 mt-0.5">
                              Target: {rf.method} &bull;{" "}
                              {formatISTWithRelative(rf.created_at, false)}
                            </div>
                          </div>
                          <div className="text-right">
                            <span className="font-bold text-white">
                              {formatMoney(rf.amount_paise)}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
