import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ChevronRight,
  Filter,
  Package,
  RefreshCw,
  Search,
  Truck,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import type { paths } from "../api/schema";
import { formatISTWithRelative } from "../utils/date";
import { formatMoney } from "../utils/money";

type OrderItem = paths["/api/v1/orders"]["get"]["responses"][200]["content"]["application/json"]["items"][0];

const STATUS_FILTERS = [
  { label: "All Orders", value: "" },
  { label: "In Transit", value: "shipped" },
  { label: "Delivered", value: "delivered" },
  { label: "Processing", value: "placed" },
  { label: "Cancelled", value: "cancelled" },
];

export function OrdersPage() {
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedStatus, setSelectedStatus] = useState("");
  const [searchTerm, setSearchTerm] = useState("");

  const fetchOrders = async (status = selectedStatus, search = searchTerm) => {
    setLoading(true);
    setError(null);

    try {
      const queryParams: Record<string, string | number> = { limit: 50 };
      if (status) queryParams.status = status;
      if (search) queryParams.search = search;

      const { data, error: apiError } = await apiClient.GET("/api/v1/orders", {
        params: {
          query: queryParams,
        },
      });

      if (apiError) {
        setError("Unable to retrieve your order history. Please check your connection.");
      } else if (data) {
        setOrders(data.items);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load orders");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchOrders(selectedStatus, searchTerm);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedStatus]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchOrders(selectedStatus, searchTerm);
  };

  const getStatusChip = (status: string) => {
    switch (status.toLowerCase()) {
      case "delivered":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
            <span>Delivered</span>
          </span>
        );
      case "shipped":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-blue-50 text-blue-700 border border-blue-200">
            <span className="h-1.5 w-1.5 rounded-full bg-blue-500"></span>
            <span>In Transit</span>
          </span>
        );
      case "placed":
      case "packed":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-amber-50 text-amber-700 border border-amber-200">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500"></span>
            <span>Processing</span>
          </span>
        );
      case "cancelled":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-600 border border-slate-200">
            <span className="h-1.5 w-1.5 rounded-full bg-slate-400"></span>
            <span>Cancelled</span>
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-700">
            {status}
          </span>
        );
    }
  };

  return (
    <div className="space-y-6">
      {/* Header and Search Bar */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 pb-2 border-b border-slate-200/80">
        <div>
          <div className="flex items-center space-x-2 text-xs font-semibold text-teal-600 uppercase tracking-wider mb-1">
            <span>Customer Portal</span>
            <span>&bull;</span>
            <span>Fulfillment</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-extrabold text-navy-950 tracking-tight">Purchases & Orders</h1>
          <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-xl">
            Review delivery status, inspect tracking updates, or initiate return and refund requests.
          </p>
        </div>

        {/* Search */}
        <form onSubmit={handleSearchSubmit} className="relative w-full md:w-80 flex-shrink-0">
          <Search className="h-4 w-4 text-slate-400 absolute left-3 top-3 pointer-events-none" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            placeholder="Search ORD-#### or product name..."
            className="w-full text-xs pl-9 pr-8 py-2.5 rounded-xl bg-white border border-slate-200 shadow-sm focus:outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-500/20 transition-all"
          />
          {searchTerm && (
            <button
              type="button"
              onClick={() => {
                setSearchTerm("");
                fetchOrders(selectedStatus, "");
              }}
              className="absolute right-2.5 top-2.5 text-slate-400 hover:text-slate-600 p-0.5"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </form>
      </div>

      {/* Filter Tabs */}
      <div className="flex items-center space-x-2 overflow-x-auto pb-1 scrollbar-none text-xs">
        <div className="flex items-center text-slate-400 mr-1 hidden sm:flex">
          <Filter className="h-3.5 w-3.5 mr-1" />
          <span className="text-[11px] font-semibold uppercase tracking-wider">Status:</span>
        </div>
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setSelectedStatus(f.value)}
            className={`px-3 py-1.5 rounded-xl font-medium whitespace-nowrap transition-all btn-tactile ${
              selectedStatus === f.value
                ? "bg-slate-900 text-white shadow-sm font-semibold"
                : "bg-white text-slate-600 hover:bg-slate-100 hover:text-slate-900 border border-slate-200/80"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Loading Skeleton */}
      {loading && (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="bg-white rounded-2xl border border-slate-200/80 p-6 space-y-4 animate-pulse shadow-card"
            >
              <div className="flex justify-between items-center">
                <div className="h-4 w-32 bg-slate-200 rounded"></div>
                <div className="h-6 w-24 bg-slate-200 rounded-full"></div>
              </div>
              <div className="space-y-2">
                <div className="h-4 w-64 bg-slate-100 rounded"></div>
                <div className="h-3 w-40 bg-slate-100 rounded"></div>
              </div>
              <div className="h-9 w-28 bg-slate-100 rounded-xl"></div>
            </div>
          ))}
        </div>
      )}

      {/* Error State */}
      {error && !loading && (
        <div className="bg-red-50 border border-red-200/80 rounded-2xl p-6 text-center space-y-3 shadow-card animate-fade-in">
          <AlertTriangle className="h-8 w-8 text-red-500 mx-auto" />
          <p className="text-sm font-semibold text-red-900">{error}</p>
          <button
            onClick={() => fetchOrders()}
            className="inline-flex items-center space-x-2 px-4 py-2 rounded-xl bg-red-600 hover:bg-red-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            <span>Retry Query</span>
          </button>
        </div>
      )}

      {/* Empty State */}
      {!loading && !error && orders.length === 0 && (
        <div className="bg-white rounded-2xl border border-slate-200/80 p-12 text-center space-y-4 shadow-card animate-fade-in">
          <div className="h-16 w-16 rounded-2xl bg-slate-100 text-slate-400 flex items-center justify-center mx-auto">
            <Package className="h-8 w-8" />
          </div>
          <div>
            <h3 className="text-base font-bold text-navy-900">No matching orders found</h3>
            <p className="text-xs text-slate-500 mt-1 max-w-sm mx-auto">
              {searchTerm || selectedStatus
                ? "No records matched your search query or status criteria. Try clearing filters to see all purchases."
                : "You haven't placed any orders in this simulated session yet."}
            </p>
          </div>
          {(searchTerm || selectedStatus) && (
            <button
              onClick={() => {
                setSelectedStatus("");
                setSearchTerm("");
                fetchOrders("", "");
              }}
              className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs text-teal-700 font-semibold bg-teal-50 hover:bg-teal-100 transition-colors"
            >
              <span>Clear filters and reset search</span>
            </button>
          )}
        </div>
      )}

      {/* Orders List Cards */}
      {!loading && !error && orders.length > 0 && (
        <div className="space-y-4">
          {orders.map((order) => (
            <Link
              key={order.order_id}
              to={`/orders/${order.order_id}`}
              className="block bg-white rounded-2xl border border-slate-200/80 hover:border-teal-500/60 p-5 sm:p-6 shadow-card hover:shadow-card-hover transition-all card-hover-lift group"
            >
              {/* Order Header */}
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between border-b border-slate-100 pb-4 gap-2">
                <div className="flex items-center space-x-3">
                  <span className="font-mono font-bold text-sm sm:text-base text-navy-950 group-hover:text-teal-700 transition-colors">
                    {order.order_id}
                  </span>
                  {getStatusChip(order.status)}
                </div>

                <div className="text-xs text-slate-500 tabular-nums font-medium">
                  Placed {formatISTWithRelative(order.placed_at)}
                </div>
              </div>

              {/* Item Summary and Total Amount */}
              <div className="py-4 grid grid-cols-1 sm:grid-cols-3 gap-4 items-center">
                <div className="sm:col-span-2 space-y-1.5">
                  <div className="flex items-center space-x-2 text-[11px] text-slate-400 uppercase tracking-wider font-semibold">
                    <span>Order Contents</span>
                    <span>&bull;</span>
                    <span>{order.items.length} {order.items.length === 1 ? "line item" : "line items"}</span>
                  </div>
                  <div className="text-xs sm:text-sm text-slate-800 font-medium line-clamp-2">
                    {order.items.map((it) => `${it.name_snapshot} (×${it.qty})`).join(", ")}
                  </div>
                </div>

                <div className="sm:text-right">
                  <span className="text-[11px] text-slate-400 block font-semibold uppercase tracking-wider">
                    Total Amount
                  </span>
                  <span className="text-base sm:text-xl font-bold font-mono tabular-nums text-navy-950">
                    {formatMoney(order.total_paise)}
                  </span>
                </div>
              </div>

              {/* Fulfillment Footer */}
              <div className="pt-3 border-t border-slate-100 flex items-center justify-between text-xs">
                <div className="flex items-center space-x-1.5 text-slate-600">
                  <Truck className="h-4 w-4 text-teal-600" />
                  <span className="font-medium">
                    {order.status === "shipped"
                      ? "In transit with SwiftShip Carrier"
                      : order.status === "delivered"
                      ? "Delivered to address"
                      : "Order confirmed & processing"}
                  </span>
                </div>

                <div className="inline-flex items-center space-x-1 text-teal-700 font-semibold group-hover:translate-x-1 transition-transform duration-150">
                  <span>View Details</span>
                  <ChevronRight className="h-4 w-4" />
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
