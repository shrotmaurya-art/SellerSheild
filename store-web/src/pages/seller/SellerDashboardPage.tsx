import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  Package,
  RefreshCw,
  RotateCcw,
  Truck,
} from "lucide-react";
import { apiClient } from "../../api/client";
import type { components } from "../../api/schema";
import { formatMoney } from "../../utils/money";
import { formatISTWithRelative } from "../../utils/date";

type OrderItem = components["schemas"]["OrderResponse"];
type ReturnItem = components["schemas"]["ReturnResponse"];

export function SellerDashboardPage() {
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [returns, setReturns] = useState<ReturnItem[]>([]);
  const [delayedCount, setDelayedCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadDashboardData = async () => {
    setLoading(true);
    setError(null);
    try {
      // 1. Fetch admin orders (up to 50 for stats)
      const { data: ordData, error: ordErr } = await apiClient.GET("/api/v1/admin/orders", {
        params: {
          query: { limit: 50 },
        },
      });

      if (ordErr) {
        throw new Error("Failed to load store orders");
      }

      const orderList = ordData?.items || [];
      setOrders(orderList);

      // 2. Fetch returns queue
      const { data: retData } = await apiClient.GET("/api/v1/returns");
      const retList = Array.isArray(retData) ? retData : [];
      setReturns(retList);

      // 3. Check for delayed shipments across shipped orders (sequentially to avoid concurrent DB locks)
      let delayed = 0;
      const shippedOrders = orderList.filter((o) => o.status === "shipped");
      for (const o of shippedOrders.slice(0, 5)) {
        try {
          const { data: shp } = await apiClient.GET("/api/v1/orders/{order_id}/shipment", {
            params: { path: { order_id: o.order_id } },
          });
          if (shp?.delayed) {
            delayed += 1;
          }
        } catch {
          // ignore individual shipment check errors
        }
      }
      setDelayedCount(delayed);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard metrics");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDashboardData();
  }, []);

  // Compute metrics
  const inTransitCount = orders.filter((o) => o.status === "shipped").length;
  const pendingReturns = returns.filter((r) => r.status === "requested").length;
  // Consider orders placed in last 24h as "today"
  const ordersToday = orders.filter((o) => {
    const placed = new Date(o.placed_at).getTime();
    return Date.now() - placed < 86400000 * 2; // generous 2 days or current day
  }).length;

  return (
    <div className="space-y-6">
      {/* Page Title */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Seller Dashboard</h1>
          <p className="text-xs text-slate-400 mt-1">
            Real-time fulfillment metrics, active return requests, and exception monitoring.
          </p>
        </div>

        <button
          onClick={loadDashboardData}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh Metrics</span>
        </button>
      </div>

      {/* KPI Tiles */}
      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-3 animate-pulse"
            >
              <div className="h-4 bg-navy-800 rounded w-1/2"></div>
              <div className="h-8 bg-navy-800 rounded w-1/3"></div>
            </div>
          ))}
        </div>
      ) : error ? (
        <div className="p-6 rounded-2xl border border-red-500/30 bg-red-950/20 text-center space-y-3">
          <AlertCircle className="h-6 w-6 text-red-400 mx-auto" />
          <p className="text-xs text-red-300 font-medium">{error}</p>
          <button
            onClick={loadDashboardData}
            className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 text-xs font-semibold transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Orders Today */}
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 hover:border-teal-500/30 transition-colors">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                Orders Today
              </span>
              <div className="h-8 w-8 rounded-lg bg-teal-500/10 text-teal-400 flex items-center justify-center">
                <Package className="h-4 w-4" />
              </div>
            </div>
            <div className="text-3xl font-extrabold text-white mt-2" data-testid="tile-orders-today">
              {ordersToday}
            </div>
            <p className="text-[11px] text-slate-400 mt-1 flex items-center space-x-1">
              <span className="text-teal-400 font-medium">Live store volume</span>
            </p>
          </div>

          {/* In Transit */}
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 hover:border-blue-500/30 transition-colors">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                In Transit
              </span>
              <div className="h-8 w-8 rounded-lg bg-blue-500/10 text-blue-400 flex items-center justify-center">
                <Truck className="h-4 w-4" />
              </div>
            </div>
            <div className="text-3xl font-extrabold text-white mt-2" data-testid="tile-in-transit">
              {inTransitCount}
            </div>
            <p className="text-[11px] text-slate-400 mt-1">Active carrier shipments</p>
          </div>

          {/* Delayed Shipments */}
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 hover:border-amber-500/30 transition-colors">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                Delayed
              </span>
              <div className="h-8 w-8 rounded-lg bg-amber-500/10 text-amber-400 flex items-center justify-center">
                <AlertTriangle className="h-4 w-4" />
              </div>
            </div>
            <div className="text-3xl font-extrabold text-white mt-2" data-testid="tile-delayed">
              {delayedCount}
            </div>
            <p className="text-[11px] text-slate-400 mt-1">Past SLA delivery threshold</p>
          </div>

          {/* Pending Returns */}
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 hover:border-amber-500/30 transition-colors">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                Pending Returns
              </span>
              <div className="h-8 w-8 rounded-lg bg-amber-500/10 text-amber-400 flex items-center justify-center">
                <RotateCcw className="h-4 w-4" />
              </div>
            </div>
            <div className="text-3xl font-extrabold text-amber-400 mt-2" data-testid="tile-pending-returns">
              {pendingReturns}
            </div>
            <p className="text-[11px] text-slate-400 mt-1">Requires seller approval</p>
          </div>
        </div>
      )}

      {/* Main Grid: Recent Orders & Quick Returns Preview */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Recent Orders Table */}
        <div className="lg:col-span-2 bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">
              Recent Store Orders
            </h2>
            <Link
              to="/seller/orders"
              className="text-xs text-teal-400 hover:text-teal-300 font-semibold flex items-center space-x-1"
            >
              <span>View all orders</span>
              <ArrowRight className="h-3 w-3" />
            </Link>
          </div>

          {orders.length === 0 && !loading ? (
            <div className="text-center py-10 text-xs text-slate-400">No orders found.</div>
          ) : (
            <div className="divide-y divide-navy-800 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-slate-400 font-medium">
                    <th className="pb-3">Order ID</th>
                    <th className="pb-3">Customer</th>
                    <th className="pb-3">Date</th>
                    <th className="pb-3">Total</th>
                    <th className="pb-3">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-800/60">
                  {orders.slice(0, 6).map((ord) => (
                    <tr key={ord.order_id} className="hover:bg-navy-800/30 transition-colors">
                      <td className="py-3 font-mono font-bold text-teal-300">{ord.order_id}</td>
                      <td className="py-3 font-medium text-slate-300">{ord.customer_id}</td>
                      <td className="py-3 text-slate-400">
                        {formatISTWithRelative(ord.placed_at, false)}
                      </td>
                      <td className="py-3 font-semibold text-white">
                        {formatMoney(ord.total_paise)}
                      </td>
                      <td className="py-3">
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
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Quick Returns Queue Card */}
        <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">
              Pending Returns ({pendingReturns})
            </h2>
            <Link
              to="/seller/returns"
              className="text-xs text-amber-400 hover:text-amber-300 font-semibold flex items-center space-x-1"
            >
              <span>Queue</span>
              <ArrowRight className="h-3 w-3" />
            </Link>
          </div>

          {returns.filter((r) => r.status === "requested").length === 0 ? (
            <div className="text-center py-12 text-xs text-slate-400">
              No returns waiting for review.
            </div>
          ) : (
            <div className="space-y-3">
              {returns
                .filter((r) => r.status === "requested")
                .slice(0, 4)
                .map((ret) => (
                  <div
                    key={ret.return_id}
                    className="p-3.5 rounded-xl bg-navy-950/60 border border-amber-500/30 space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono font-bold text-amber-300 text-xs">
                        {ret.return_id}
                      </span>
                      <span className="text-[10px] text-slate-400">
                        {ret.order_id}
                      </span>
                    </div>
                    <p className="text-xs text-slate-300 line-clamp-1">{ret.reason_code}</p>
                    <div className="flex items-center justify-between text-[11px] pt-1">
                      <span className="text-slate-400">Customer: {ret.customer_id}</span>
                      <Link
                        to="/seller/returns"
                        className="text-teal-400 hover:text-teal-300 font-semibold"
                      >
                        Review &rarr;
                      </Link>
                    </div>
                  </div>
                ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
