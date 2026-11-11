import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Eye,
  FileSearch,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import {
  fetchSellerAudit,
  verifySellerAudit,
  type AuditEventItem,
  type AuditVerifyResult,
} from "../../api/sellerShield";
import { DecisionTraceDrawer } from "../../components/seller/DecisionTraceDrawer";
import { formatIST } from "../../utils/date";

export function SellerAuditPage() {
  const [events, setEvents] = useState<AuditEventItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState<AuditVerifyResult | null>(null);
  const [requestIdFilter, setRequestIdFilter] = useState("");
  const [resourceIdFilter, setResourceIdFilter] = useState("");
  const [eventTypeFilter, setEventTypeFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null);

  const loadAudit = useCallback(
    async (cursorVal: string | null = null, append = false) => {
      if (append) {
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const res = await fetchSellerAudit({
          limit: 40,
          cursor: cursorVal,
          request_id: requestIdFilter,
          resource_id: resourceIdFilter,
          event_type: eventTypeFilter,
        });
        setEvents((prev) => (append ? [...prev, ...res.events] : res.events));
        setNextCursor(res.next_cursor);
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : "Failed to load audit events");
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [requestIdFilter, resourceIdFilter, eventTypeFilter],
  );

  const handleVerifyChain = useCallback(async () => {
    setVerifying(true);
    setError(null);
    try {
      const res = await verifySellerAudit();
      setVerifyResult(res);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Chain verification failed");
    } finally {
      setVerifying(false);
    }
  }, []);

  useEffect(() => {
    loadAudit(null, false);
    handleVerifyChain();
  }, [loadAudit, handleVerifyChain]);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    loadAudit(null, false);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">Audit Log &amp; Decision Traces</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Append-only cryptographic hash chain recording every understanding step, tool call, approval, and execution
          </p>
        </div>

        <div className="flex items-center space-x-2.5 self-start sm:self-auto">
          <button
            type="button"
            onClick={handleVerifyChain}
            disabled={verifying}
            className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-teal-900 text-white text-xs font-semibold transition-all"
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            <span>{verifying ? "Verifying..." : "Verify SHA-256 Chain"}</span>
          </button>

          <button
            type="button"
            onClick={() => loadAudit(null, false)}
            disabled={loading}
            className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-navy-900 hover:bg-navy-800 border border-navy-700 text-slate-300 text-xs font-medium transition-colors"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Chain Integrity Banner */}
      {verifyResult && (
        <div
          className={`rounded-2xl p-4 border text-xs flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 ${
            verifyResult.verified
              ? "bg-emerald-950/35 border-emerald-500/40 text-emerald-200"
              : "bg-red-950/40 border-red-500/40 text-red-200"
          }`}
        >
          <div className="flex items-center space-x-2.5">
            {verifyResult.verified ? (
              <CheckCircle2 className="h-5 w-5 text-emerald-400 flex-shrink-0" />
            ) : (
              <AlertCircle className="h-5 w-5 text-red-400 flex-shrink-0" />
            )}
            <div>
              <div className="font-bold">
                {verifyResult.verified
                  ? "SHA-256 Hash Chain Verified"
                  : `Hash Chain Break Detected (seq ${String(verifyResult.broken_seq ?? "?")})`}
              </div>
              <div className="text-[11px] font-mono opacity-85 mt-0.5">
                Events verified: {verifyResult.total_events}
                {verifyResult.error ? ` • Error: ${verifyResult.error}` : ""}
              </div>
            </div>
          </div>

          {verifyResult.head_hash && (
            <div className="font-mono text-[11px] bg-navy-950/70 px-3 py-1.5 rounded-lg border border-navy-800 text-slate-300">
              Head: {verifyResult.head_hash.slice(0, 18)}...
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-red-500/40 rounded-xl p-3.5 text-xs text-red-200 flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Filter Bar */}
      <form
        onSubmit={handleSearch}
        className="bg-navy-900 border border-navy-800 rounded-2xl p-4 grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs"
      >
        <input
          type="text"
          value={requestIdFilter}
          onChange={(e) => setRequestIdFilter(e.target.value)}
          placeholder="Filter by Request ID..."
          aria-label="Filter by Request ID"
          className="px-3 py-2 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
        />
        <input
          type="text"
          value={resourceIdFilter}
          onChange={(e) => setResourceIdFilter(e.target.value)}
          placeholder="Filter by Resource ID (ORD-..., APR-...)"
          aria-label="Filter by Resource ID"
          className="px-3 py-2 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
        />
        <input
          type="text"
          value={eventTypeFilter}
          onChange={(e) => setEventTypeFilter(e.target.value)}
          placeholder="Filter by Event Type..."
          aria-label="Filter by Event Type"
          className="px-3 py-2 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
        />
        <button
          type="submit"
          className="inline-flex items-center justify-center space-x-1.5 px-4 py-2 rounded-xl bg-navy-800 hover:bg-navy-700 text-white font-semibold transition-colors"
        >
          <Search className="h-3.5 w-3.5" />
          <span>Apply Filters</span>
        </button>
      </form>

      {/* Audit Table */}
      <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-4">
        <div className="flex items-center space-x-2">
          <FileSearch className="h-4 w-4 text-teal-400" />
          <h2 className="text-sm font-bold text-white">Immutable Audit Events</h2>
        </div>

        {events.length === 0 && !loading ? (
          <p className="text-xs text-slate-400 py-4">No audit events match your filter criteria.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-navy-800 text-[10px] font-mono uppercase text-slate-400">
                  <th className="py-2.5 pr-4">Seq</th>
                  <th className="py-2.5 pr-4">Event Type</th>
                  <th className="py-2.5 pr-4">Actor</th>
                  <th className="py-2.5 pr-4">Request ID</th>
                  <th className="py-2.5 pr-4">Details Preview</th>
                  <th className="py-2.5 pr-4">Timestamp (IST)</th>
                  <th className="py-2.5 text-right">Trace</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-800/70">
                {events.map((ev) => (
                  <tr key={ev.event_id} className="hover:bg-navy-950/40">
                    <td className="py-3 pr-4 font-mono text-slate-400 tabular-nums">{ev.seq}</td>
                    <td className="py-3 pr-4">
                      <span className="px-2 py-0.5 rounded bg-navy-950 border border-navy-800 font-mono text-[11px] text-teal-300">
                        {ev.event_type}
                      </span>
                    </td>
                    <td className="py-3 pr-4 font-mono text-slate-300">{ev.actor}</td>
                    <td className="py-3 pr-4 font-mono text-slate-300 whitespace-nowrap">
                      {ev.request_id || "—"}
                    </td>
                    <td className="py-3 pr-4 max-w-xs truncate font-mono text-[11px] text-slate-400">
                      {JSON.stringify(ev.details || {})}
                    </td>
                    <td className="py-3 pr-4 text-slate-400 whitespace-nowrap">
                      {formatIST(ev.created_at)}
                    </td>
                    <td className="py-3 text-right whitespace-nowrap">
                      {ev.request_id && (
                        <button
                          type="button"
                          onClick={() => setSelectedRequestId(ev.request_id)}
                          className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-lg bg-navy-800 hover:bg-teal-700 text-slate-200 hover:text-white text-[11px] font-semibold transition-colors"
                        >
                          <Eye className="h-3 w-3" />
                          <span>Trace</span>
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {nextCursor && (
          <div className="pt-2 text-center">
            <button
              type="button"
              onClick={() => loadAudit(nextCursor, true)}
              disabled={loadingMore}
              className="px-4 py-2 rounded-xl bg-navy-800 hover:bg-navy-700 text-xs font-semibold text-white transition-colors"
            >
              {loadingMore ? "Loading..." : "Load Older Events"}
            </button>
          </div>
        )}
      </div>

      <DecisionTraceDrawer
        requestId={selectedRequestId}
        onClose={() => setSelectedRequestId(null)}
      />
    </div>
  );
}
