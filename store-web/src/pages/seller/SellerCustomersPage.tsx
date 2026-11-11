import { useEffect, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  RefreshCw,
  Users,
} from "lucide-react";
import { authFetch } from "../../api/client";
import type { components } from "../../api/schema";
import { formatMoney } from "../../utils/money";

type CustomerSummary = components["schemas"]["CustomerAdminSummaryResponse"];

const SEED_CUSTOMER_IDS = ["C001", "C002", "C003", "C004", "C005"];

export function SellerCustomersPage() {
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCustomers = async () => {
    setLoading(true);
    setError(null);
    try {
      // Query known customers C001 - C005 sequentially to avoid SQLite connection locking
      const summaries: CustomerSummary[] = [];
      for (const cid of SEED_CUSTOMER_IDS) {
        try {
          const res = await authFetch(`/api/v1/admin/customers/${cid}`);
          if (res.ok) {
            summaries.push((await res.json()) as CustomerSummary);
          }
        } catch (fetchErr) {
          console.error(`Error fetching customer ${cid}:`, fetchErr);
        }
      }

      setCustomers(summaries);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load customer profiles");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCustomers();
  }, []);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Customer Intelligence</h1>
          <p className="text-xs text-slate-400 mt-1">
            Account age, 30-day return velocity, 90-day refund ratio, and customer risk tiers.
          </p>
        </div>

        <button
          onClick={fetchCustomers}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Customer List */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-4 animate-pulse"
            >
              <div className="h-5 bg-navy-800 rounded w-1/2"></div>
              <div className="h-4 bg-navy-800 rounded w-2/3"></div>
              <div className="h-10 bg-navy-800 rounded w-full"></div>
            </div>
          ))}
        </div>
      ) : error ? (
        <div className="p-6 rounded-2xl border border-red-500/30 bg-red-950/20 text-center space-y-3">
          <AlertCircle className="h-6 w-6 text-red-400 mx-auto" />
          <p className="text-xs text-red-300 font-medium">{error}</p>
          <button
            onClick={fetchCustomers}
            className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 text-xs font-semibold transition-colors"
          >
            Retry
          </button>
        </div>
      ) : customers.length === 0 ? (
        <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-12 text-center text-slate-400 space-y-2">
          <Users className="h-8 w-8 mx-auto text-slate-600" />
          <p className="text-sm font-semibold text-slate-300">No customer records found</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {customers.map((c) => {
            const hasHighReturns = c.returns_30d >= 3;
            const refundRatioPercent = (c.refund_ratio_90d * 100).toFixed(1);

            return (
              <div
                key={c.customer_id}
                className={`bg-navy-900 rounded-2xl p-5 border transition-all ${
                  hasHighReturns
                    ? "border-amber-500/40 shadow-lg shadow-amber-950/20"
                    : "border-navy-800/80 hover:border-navy-700"
                }`}
                data-testid={`customer-card-${c.customer_id}`}
              >
                {/* Top: Customer name and ID */}
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="font-bold text-white text-base">{c.full_name}</h3>
                    <p className="font-mono text-xs text-teal-300 mt-0.5">{c.customer_id}</p>
                  </div>

                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                      hasHighReturns
                        ? "bg-amber-900/60 text-amber-300 border border-amber-500/40"
                        : "bg-teal-900/60 text-teal-300 border border-teal-500/30"
                    }`}
                  >
                    {hasHighReturns ? "Elevated Risk" : "Standard"}
                  </span>
                </div>

                {/* Metrics Grid */}
                <div className="mt-4 grid grid-cols-2 gap-3 pt-3 border-t border-navy-800/80 text-xs">
                  {/* Account Age */}
                  <div className="bg-navy-950/60 p-2.5 rounded-xl border border-navy-800">
                    <span className="text-[10px] uppercase font-semibold text-slate-400 block">
                      Account Age
                    </span>
                    <span className="font-semibold text-slate-200 mt-0.5 block">
                      {c.account_age}
                    </span>
                  </div>

                  {/* Returns (30 Days) */}
                  <div
                    className={`p-2.5 rounded-xl border ${
                      hasHighReturns
                        ? "bg-amber-950/30 border-amber-500/40"
                        : "bg-navy-950/60 border-navy-800"
                    }`}
                  >
                    <span className="text-[10px] uppercase font-semibold text-slate-400 block">
                      Returns (30d)
                    </span>
                    <span
                      className={`font-bold mt-0.5 block ${
                        hasHighReturns ? "text-amber-300 text-sm" : "text-slate-200"
                      }`}
                      data-testid={`returns-30d-${c.customer_id}`}
                    >
                      {c.returns_30d} {c.returns_30d === 1 ? "return" : "returns"}
                    </span>
                  </div>

                  {/* 90-day Refund Ratio */}
                  <div className="bg-navy-950/60 p-2.5 rounded-xl border border-navy-800">
                    <span className="text-[10px] uppercase font-semibold text-slate-400 block">
                      90d Refund Ratio
                    </span>
                    <span className="font-semibold text-slate-200 mt-0.5 block">
                      {refundRatioPercent}%
                    </span>
                  </div>

                  {/* Total Spend */}
                  <div className="bg-navy-950/60 p-2.5 rounded-xl border border-navy-800">
                    <span className="text-[10px] uppercase font-semibold text-slate-400 block">
                      Total Spend
                    </span>
                    <span className="font-semibold text-white mt-0.5 block">
                      {formatMoney(c.total_spend_paise)}
                    </span>
                  </div>
                </div>

                {/* Bottom summary note */}
                <div className="mt-3 text-[11px] text-slate-400 flex items-center justify-between">
                  <span>{c.total_orders} lifetime orders</span>
                  {hasHighReturns && (
                    <span className="flex items-center space-x-1 text-amber-400 font-semibold">
                      <AlertTriangle className="h-3 w-3" />
                      <span>Review claims</span>
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
