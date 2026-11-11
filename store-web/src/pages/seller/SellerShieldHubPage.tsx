import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  Bot,
  CheckCircle2,
  FileSearch,
  Inbox,
  MessageSquare,
  Play,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import {
  fetchSellerOverview,
  SellerApiError,
  triggerMonitorRun,
  type SellerOverview,
} from "../../api/sellerShield";
import { formatISTWithRelative } from "../../utils/date";

export function SellerShieldHubPage() {
  const [overview, setOverview] = useState<SellerOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [runningMonitor, setRunningMonitor] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadOverview = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSellerOverview();
      setOverview(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load SellerShield overview");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadOverview();
  }, []);

  const handleRunMonitor = async () => {
    setRunningMonitor(true);
    setNotice(null);
    setError(null);
    try {
      const run = await triggerMonitorRun();
      setNotice(
        `Monitor run ${run.run_id} checked ${run.checked_orders_count} shipments (${run.anomalies_detected} anomalies flagged).`,
      );
      await loadOverview();
    } catch (err: unknown) {
      if (err instanceof SellerApiError && err.status === 429) {
        const retrySecs = err.details?.retry_after_seconds;
        setError(
          retrySecs
            ? `${err.message} Please retry in ${String(retrySecs)}s.`
            : err.message,
        );
      } else {
        setError(err instanceof Error ? err.message : "Monitor run failed");
      }
    } finally {
      setRunningMonitor(false);
    }
  };

  const quickLinks = [
    {
      to: "/seller/approvals",
      title: "Approval Queue",
      desc: "Review & decide high-value cancellations and refund holds with live store revalidation.",
      badge: overview ? `${overview.pending_approvals_count} pending` : "—",
      tone: "text-amber-300 bg-amber-950/60 border-amber-500/30",
      icon: <ShieldCheck className="h-5 w-5 text-amber-400" />,
    },
    {
      to: "/seller/cases",
      title: "Escalation Cases",
      desc: "Respond to customer escalations, inspect deterministic briefs, and hand back to AI.",
      badge: overview ? `${overview.open_cases_count} open` : "—",
      tone: "text-teal-300 bg-teal-950/60 border-teal-500/30",
      icon: <MessageSquare className="h-5 w-5 text-teal-400" />,
    },
    {
      to: "/seller/activity",
      title: "Monitor & Outbox",
      desc: "Inspect proactive carrier delay runs and retry any failed customer email notifications.",
      badge: overview ? `${overview.failed_notifications_count} failed` : "—",
      tone:
        overview && overview.failed_notifications_count > 0
          ? "text-red-300 bg-red-950/60 border-red-500/30"
          : "text-emerald-300 bg-emerald-950/60 border-emerald-500/30",
      icon: <Inbox className="h-5 w-5 text-sky-400" />,
    },
    {
      to: "/seller/audit",
      title: "Audit Log & Traces",
      desc: "Verify cryptographic SHA-256 chain integrity and inspect full AI decision traces.",
      badge: overview ? `Policy ${overview.active_policy_version}` : "—",
      tone: "text-slate-300 bg-slate-800 border-slate-700",
      icon: <FileSearch className="h-5 w-5 text-purple-400" />,
    },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div className="flex items-center space-x-3">
          <div className="h-10 w-10 rounded-xl bg-teal-500/15 border border-teal-500/30 text-teal-300 flex items-center justify-center flex-shrink-0">
            <Bot className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-white tracking-tight">SellerShield AI Console</h1>
            <p className="text-xs text-slate-400 mt-0.5">
              Operational command center for autonomous support approvals, cases, monitor sweeps, and verifiable audit trails
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2.5 self-start sm:self-auto">
          <button
            type="button"
            onClick={handleRunMonitor}
            disabled={runningMonitor}
            className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-teal-900 disabled:text-teal-400 text-white text-xs font-semibold transition-all shadow-sm"
          >
            <Play className={`h-3.5 w-3.5 ${runningMonitor ? "animate-pulse" : ""}`} />
            <span>{runningMonitor ? "Scanning..." : "Run Delay Monitor"}</span>
          </button>

          <button
            type="button"
            onClick={loadOverview}
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

      {/* Primary KPI Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-2">
          <div className="text-[11px] font-mono uppercase tracking-wider text-slate-400">
            Pending Approvals
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-3xl font-bold text-white tabular-nums">
              {loading ? "—" : overview?.pending_approvals_count ?? 0}
            </span>
            <span className="text-xs font-mono text-amber-300">
              {overview ? `${overview.overdue_approvals_count} overdue` : ""}
            </span>
          </div>
          <p className="text-[11px] text-slate-400">
            Proposals awaiting owner/staff sign-off
          </p>
        </div>

        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-2">
          <div className="text-[11px] font-mono uppercase tracking-wider text-slate-400">
            Open Escalation Cases
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-3xl font-bold text-white tabular-nums">
              {loading ? "—" : overview?.open_cases_count ?? 0}
            </span>
            <span className="text-xs font-mono text-red-300">
              {overview ? `${overview.open_cases_by_priority.high ?? 0} high priority` : ""}
            </span>
          </div>
          <p className="text-[11px] text-slate-400">
            Normal priority: {overview?.open_cases_by_priority.normal ?? 0}
          </p>
        </div>

        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-2">
          <div className="text-[11px] font-mono uppercase tracking-wider text-slate-400">
            Auto-Resolved Rate (IST)
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-3xl font-bold text-teal-300 tabular-nums">
              {loading || !overview ? "—" : `${Math.round(overview.auto_resolved_rate * 100)}%`}
            </span>
            <span className="text-xs font-mono text-slate-400">
              {overview ? `${overview.today_executions_count} execs today` : ""}
            </span>
          </div>
          <p className="text-[11px] text-slate-400">
            {overview
              ? `${overview.today_conversations_count} chats • ${overview.today_proposals_count} proposals`
              : "Loading today stats"}
          </p>
        </div>

        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-2">
          <div className="text-[11px] font-mono uppercase tracking-wider text-slate-400">
            Active Policy &amp; Outbox
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-3xl font-bold text-white font-mono">
              {overview?.active_policy_version ?? "v3"}
            </span>
            <span
              className={`text-xs font-mono ${
                overview && overview.failed_notifications_count > 0
                  ? "text-red-300"
                  : "text-emerald-300"
              }`}
            >
              {overview ? `${overview.failed_notifications_count} failed mail` : ""}
            </span>
          </div>
          <p className="text-[11px] text-slate-400">Deterministic rules enforced on every write</p>
        </div>
      </div>

      {/* Module Quick Links */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {quickLinks.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="group bg-navy-900/90 hover:bg-navy-900 border border-navy-800 hover:border-teal-500/40 rounded-2xl p-5 transition-all flex items-start justify-between gap-4"
          >
            <div className="flex items-start space-x-3.5 min-w-0">
              <div className="h-10 w-10 rounded-xl bg-navy-950 border border-navy-800 flex items-center justify-center flex-shrink-0">
                {item.icon}
              </div>
              <div className="space-y-1.5 min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-bold text-white group-hover:text-teal-300 transition-colors">
                    {item.title}
                  </h2>
                  <span
                    className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-bold border ${item.tone}`}
                  >
                    {item.badge}
                  </span>
                </div>
                <p className="text-xs text-slate-400 leading-relaxed">{item.desc}</p>
              </div>
            </div>
            <ArrowRight className="h-4 w-4 text-slate-500 group-hover:text-teal-400 group-hover:translate-x-0.5 transition-all flex-shrink-0 mt-1" />
          </Link>
        ))}
      </div>

      {/* Last Monitor Run Card */}
      <div className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <Activity className="h-4 w-4 text-teal-400" />
            <h2 className="text-sm font-bold text-white">Latest Proactive Delay Monitor Run</h2>
          </div>
          <Link
            to="/seller/activity"
            className="text-xs font-medium text-teal-400 hover:text-teal-300"
          >
            View All Runs &rarr;
          </Link>
        </div>

        {overview?.last_monitor_run ? (
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs bg-navy-950/80 border border-navy-800/80 rounded-xl p-4">
            <div>
              <div className="text-[10px] font-mono text-slate-400 uppercase">Run ID</div>
              <div className="font-mono font-semibold text-teal-300 mt-0.5">
                {overview.last_monitor_run.run_id}
              </div>
            </div>
            <div>
              <div className="text-[10px] font-mono text-slate-400 uppercase">Shipments Checked</div>
              <div className="font-semibold text-white mt-0.5">
                {overview.last_monitor_run.checked_orders_count}
              </div>
            </div>
            <div>
              <div className="text-[10px] font-mono text-slate-400 uppercase">Anomalies Flagged</div>
              <div className="font-semibold text-amber-300 mt-0.5">
                {overview.last_monitor_run.anomalies_detected}
              </div>
            </div>
            <div>
              <div className="text-[10px] font-mono text-slate-400 uppercase">Executed At</div>
              <div className="text-slate-300 mt-0.5">
                {formatISTWithRelative(overview.last_monitor_run.created_at, true)}
              </div>
            </div>
          </div>
        ) : (
          <p className="text-xs text-slate-400">
            No proactive delay monitor sweep has executed yet. Click &ldquo;Run Delay Monitor&rdquo; above to scan active courier shipments.
          </p>
        )}
      </div>
    </div>
  );
}
