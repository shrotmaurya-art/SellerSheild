import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Mail,
  Play,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import {
  fetchMonitorRuns,
  fetchSellerNotifications,
  retrySellerNotification,
  SellerApiError,
  triggerMonitorRun,
  type MonitorRunItem,
  type NotificationItem,
} from "../../api/sellerShield";
import { formatISTWithRelative } from "../../utils/date";

const NOTIF_FILTERS = ["all", "sent", "queued", "failed", "exhausted"];

export function SellerActivityPage() {
  const [runs, setRuns] = useState<MonitorRunItem[]>([]);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [notifFilter, setNotifFilter] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const [runningMonitor, setRunningMonitor] = useState(false);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async (statusFilter = notifFilter) => {
    setLoading(true);
    setError(null);
    try {
      const [runsRes, notifsRes] = await Promise.all([
        fetchMonitorRuns(),
        fetchSellerNotifications(statusFilter),
      ]);
      setRuns(runsRes.runs);
      setNotifications(notifsRes.notifications);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load activity feed");
    } finally {
      setLoading(false);
    }
  }, [notifFilter]);

  useEffect(() => {
    loadData(notifFilter);
  }, [loadData, notifFilter]);

  const handleRunMonitor = async () => {
    setRunningMonitor(true);
    setNotice(null);
    setError(null);
    try {
      const res = await triggerMonitorRun();
      setNotice(
        `Completed monitor sweep ${res.run_id}: scanned ${res.checked_orders_count} shipments, flagged ${res.anomalies_detected}.`,
      );
      await loadData(notifFilter);
    } catch (err: unknown) {
      if (err instanceof SellerApiError && err.status === 429) {
        const secs = err.details?.retry_after_seconds;
        setError(secs ? `${err.message} Retry in ${String(secs)}s.` : err.message);
      } else {
        setError(err instanceof Error ? err.message : "Monitor sweep failed");
      }
    } finally {
      setRunningMonitor(false);
    }
  };

  const handleRetryNotification = async (id: string) => {
    setRetryingId(id);
    setNotice(null);
    setError(null);
    try {
      const updated = await retrySellerNotification(id);
      setNotice(`Notification ${updated.notification_id} re-queued for delivery.`);
      await loadData(notifFilter);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Retry failed");
    } finally {
      setRetryingId(null);
    }
  };

  const statusBadge = (st: string) => {
    switch (st) {
      case "sent":
      case "delivered":
        return "bg-emerald-950 text-emerald-300 border-emerald-500/30";
      case "queued":
      case "sending":
      case "retry_scheduled":
        return "bg-sky-950 text-sky-300 border-sky-500/30";
      case "failed":
      case "exhausted":
        return "bg-red-950 text-red-300 border-red-500/30";
      default:
        return "bg-slate-800 text-slate-300 border-slate-700";
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">Monitor Runs &amp; Outbox</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Proactive courier delay sweeps and transactional customer email delivery queue
          </p>
        </div>

        <div className="flex items-center space-x-2.5 self-start sm:self-auto">
          <button
            type="button"
            onClick={handleRunMonitor}
            disabled={runningMonitor}
            className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-teal-900 disabled:text-teal-400 text-white text-xs font-semibold transition-all"
          >
            <Play className="h-3.5 w-3.5" />
            <span>{runningMonitor ? "Running..." : "Trigger Monitor Sweep"}</span>
          </button>

          <button
            type="button"
            onClick={() => loadData(notifFilter)}
            disabled={loading}
            className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-navy-900 hover:bg-navy-800 border border-navy-700 text-slate-300 text-xs font-medium transition-colors"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {notice && (
        <div className="bg-emerald-950/50 border border-emerald-500/40 rounded-xl p-3.5 text-xs text-emerald-200 flex items-center space-x-2">
          <CheckCircle2 className="h-4 w-4 text-emerald-400 flex-shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-red-500/40 rounded-xl p-3.5 text-xs text-red-200 flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Monitor Runs */}
      <section className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-4">
        <div className="flex items-center space-x-2">
          <Activity className="h-4 w-4 text-teal-400" />
          <h2 className="text-sm font-bold text-white">Delay Monitor Run History</h2>
          <span className="text-xs font-mono text-slate-400">({runs.length})</span>
        </div>

        {runs.length === 0 && !loading ? (
          <p className="text-xs text-slate-400 py-4">
            No monitor runs recorded yet. Trigger a sweep above to detect delayed or lost shipments.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-navy-800 text-[10px] font-mono uppercase text-slate-400">
                  <th className="py-2.5 pr-4">Run ID</th>
                  <th className="py-2.5 pr-4">Checked</th>
                  <th className="py-2.5 pr-4">Anomalies</th>
                  <th className="py-2.5 pr-4">Actions</th>
                  <th className="py-2.5">Started (IST)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-800/70">
                {runs.map((r) => (
                  <tr key={r.run_id} className="hover:bg-navy-950/40">
                    <td className="py-3 pr-4 font-mono text-teal-300 font-semibold">{r.run_id}</td>
                    <td className="py-3 pr-4 text-white tabular-nums">{r.checked_orders_count}</td>
                    <td className="py-3 pr-4 tabular-nums">
                      <span
                        className={
                          r.anomalies_detected > 0
                            ? "text-amber-300 font-semibold"
                            : "text-slate-400"
                        }
                      >
                        {r.anomalies_detected}
                      </span>
                    </td>
                    <td className="py-3 pr-4 text-slate-300 font-mono text-[11px]">
                      {r.actions_taken.length > 0 ? r.actions_taken.join(", ") : "none"}
                    </td>
                    <td className="py-3 text-slate-400">
                      {formatISTWithRelative(r.created_at, true)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Outbox Notifications */}
      <section className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div className="flex items-center space-x-2">
            <Mail className="h-4 w-4 text-teal-400" />
            <h2 className="text-sm font-bold text-white">Customer Notification Outbox</h2>
            <span className="text-xs font-mono text-slate-400">({notifications.length})</span>
          </div>

          <div className="flex flex-wrap gap-1.5">
            {NOTIF_FILTERS.map((st) => (
              <button
                key={st}
                type="button"
                onClick={() => setNotifFilter(st)}
                className={`px-2.5 py-1 rounded-lg text-[11px] font-mono uppercase transition-colors ${
                  notifFilter === st
                    ? "bg-teal-600 text-white font-bold"
                    : "bg-navy-950 text-slate-400 hover:text-white border border-navy-800"
                }`}
              >
                {st}
              </button>
            ))}
          </div>
        </div>

        {notifications.length === 0 && !loading ? (
          <p className="text-xs text-slate-400 py-4">No notifications match the selected filter.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-navy-800 text-[10px] font-mono uppercase text-slate-400">
                  <th className="py-2.5 pr-4">Notification ID</th>
                  <th className="py-2.5 pr-4">Subject / Recipient</th>
                  <th className="py-2.5 pr-4">Status</th>
                  <th className="py-2.5 pr-4">Attempts</th>
                  <th className="py-2.5 pr-4">Created (IST)</th>
                  <th className="py-2.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-800/70">
                {notifications.map((n) => (
                  <tr key={n.notification_id} className="hover:bg-navy-950/40">
                    <td className="py-3 pr-4 font-mono text-teal-300 font-semibold whitespace-nowrap">
                      {n.notification_id}
                    </td>
                    <td className="py-3 pr-4 min-w-[200px]">
                      <div className="font-semibold text-white">{n.subject || "(No subject)"}</div>
                      <div className="text-[11px] font-mono text-slate-400">{n.recipient}</div>
                    </td>
                    <td className="py-3 pr-4 whitespace-nowrap">
                      <span
                        className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border ${statusBadge(
                          n.status,
                        )}`}
                      >
                        {n.status}
                      </span>
                    </td>
                    <td className="py-3 pr-4 font-mono text-slate-300 tabular-nums">{n.attempts}</td>
                    <td className="py-3 pr-4 text-slate-400 whitespace-nowrap">
                      {formatISTWithRelative(n.created_at, true)}
                    </td>
                    <td className="py-3 text-right whitespace-nowrap">
                      {(n.status === "failed" || n.status === "exhausted") && (
                        <button
                          type="button"
                          onClick={() => handleRetryNotification(n.notification_id)}
                          disabled={retryingId === n.notification_id}
                          className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-lg bg-amber-600 hover:bg-amber-700 disabled:bg-amber-900 text-white text-[11px] font-semibold transition-colors"
                        >
                          <RotateCcw className="h-3 w-3" />
                          <span>{retryingId === n.notification_id ? "Queuing..." : "Retry"}</span>
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
