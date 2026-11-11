import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Eye,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import {
  decideSellerApproval,
  fetchSellerApprovals,
  fetchSellerAudit,
  SellerApiError,
  type SellerApproval,
} from "../../api/sellerShield";
import { DecisionTraceDrawer } from "../../components/seller/DecisionTraceDrawer";
import { formatISTWithRelative } from "../../utils/date";
import { formatMoney } from "../../utils/money";

const STATUS_TABS = [
  "pending",
  "needs_revalidation",
  "approved",
  "rejected",
  "expired",
  "withdrawn",
  "all",
];

export function SellerApprovalsPage() {
  const [approvals, setApprovals] = useState<SellerApproval[]>([]);
  const [total, setTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState<string>("pending");
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [actionTarget, setActionTarget] = useState<SellerApproval | null>(null);
  const [actionType, setActionType] = useState<"approve" | "reject" | null>(null);
  const [reasonText, setReasonText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const [traceRequestId, setTraceRequestId] = useState<string | null>(null);
  const [traceLookupError, setTraceLookupError] = useState<string | null>(null);

  const loadApprovals = useCallback(async (filterVal = statusFilter) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchSellerApprovals({ status: filterVal, limit: 50, offset: 0 });
      setApprovals(res.approvals);
      setTotal(res.total);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load approval queue");
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    loadApprovals(statusFilter);
  }, [loadApprovals, statusFilter]);

  const openDecisionDialog = (apr: SellerApproval, kind: "approve" | "reject") => {
    setActionTarget(apr);
    setActionType(kind);
    setReasonText(
      kind === "approve" ? "Verified against live order and store policy rules" : "",
    );
    setModalError(null);
  };

  const handleSubmitDecision = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!actionTarget || !actionType) return;
    if (actionType === "reject" && !reasonText.trim()) {
      setModalError("A non-empty rejection reason is required.");
      return;
    }

    setSubmitting(true);
    setModalError(null);
    setNotice(null);
    try {
      const updated = await decideSellerApproval(actionTarget.approval_id, {
        decision: actionType,
        reason: reasonText.trim() || null,
        expected_version: actionTarget.expected_version,
      });
      setNotice(
        `Approval ${updated.approval_id} marked as ${updated.status.toUpperCase()} for order ${updated.order_id}.`,
      );
      setActionTarget(null);
      setActionType(null);
      await loadApprovals(statusFilter);
    } catch (err: unknown) {
      if (err instanceof SellerApiError && err.status === 409) {
        setModalError(`${err.message} Queue reloaded with latest state.`);
        await loadApprovals(statusFilter);
      } else {
        setModalError(err instanceof Error ? err.message : "Decision request failed");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleOpenTrace = async (apr: SellerApproval) => {
    setTraceLookupError(null);
    try {
      const res = await fetchSellerAudit({ resource_id: apr.approval_id, limit: 10 });
      const withReq = res.events.find((ev) => Boolean(ev.request_id));
      if (withReq?.request_id) {
        setTraceRequestId(withReq.request_id);
        return;
      }
      const propAudit = await fetchSellerAudit({ resource_id: apr.proposal_id, limit: 10 });
      const propReq = propAudit.events.find((ev) => Boolean(ev.request_id));
      if (propReq?.request_id) {
        setTraceRequestId(propReq.request_id);
        return;
      }
      setTraceLookupError(`No decision trace request_id found for ${apr.approval_id}.`);
    } catch (err: unknown) {
      setTraceLookupError(err instanceof Error ? err.message : "Trace lookup failed");
    }
  };

  const statusTone = (st: string) => {
    switch (st) {
      case "pending":
        return "bg-amber-950 text-amber-300 border-amber-500/40";
      case "needs_revalidation":
        return "bg-orange-950 text-orange-300 border-orange-500/40";
      case "approved":
        return "bg-emerald-950 text-emerald-300 border-emerald-500/40";
      case "rejected":
      case "expired":
        return "bg-red-950 text-red-300 border-red-500/40";
      default:
        return "bg-slate-800 text-slate-300 border-slate-700";
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">Seller Approval Queue</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Deterministic approval cards with live store revalidation, risk signals, and owner/staff enforcement ({total} total)
          </p>
        </div>

        <button
          type="button"
          onClick={() => loadApprovals(statusFilter)}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-navy-900 hover:bg-navy-800 border border-navy-700 text-slate-300 text-xs font-medium self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh Queue</span>
        </button>
      </div>

      {/* Filter Tabs */}
      <div className="flex flex-wrap gap-2">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setStatusFilter(tab)}
            className={`px-3 py-1.5 rounded-xl text-xs font-mono uppercase transition-colors ${
              statusFilter === tab
                ? "bg-teal-600 text-white font-bold shadow-sm"
                : "bg-navy-900 text-slate-400 hover:text-white border border-navy-800"
            }`}
          >
            {tab.replace("_", " ")}
          </button>
        ))}
      </div>

      {notice && (
        <div className="bg-emerald-950/50 border border-emerald-500/40 rounded-xl p-3.5 text-xs text-emerald-200 flex items-center space-x-2">
          <CheckCircle2 className="h-4 w-4 text-emerald-400 flex-shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {(error || traceLookupError) && (
        <div className="bg-red-950/40 border border-red-500/40 rounded-xl p-3.5 text-xs text-red-200 flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error || traceLookupError}</span>
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center text-xs font-mono text-slate-400">
          Loading deterministic approval cards...
        </div>
      ) : approvals.length === 0 ? (
        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-10 text-center space-y-2">
          <ShieldCheck className="h-8 w-8 text-teal-400 mx-auto" />
          <div className="text-sm font-bold text-white">Queue Clear</div>
          <p className="text-xs text-slate-400">
            No approvals currently in &ldquo;{statusFilter}&rdquo; state.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {approvals.map((apr) => {
            const actionable =
              apr.status === "pending" || apr.status === "needs_revalidation";
            const riskBand = String(apr.risk?.band || "low").toLowerCase();
            const riskSignals = Array.isArray(apr.risk?.signals)
              ? (apr.risk?.signals as string[])
              : apr.risk_flags;

            return (
              <article
                key={apr.approval_id}
                className="bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-4 text-xs"
              >
                {/* Card Header */}
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 border-b border-navy-800/80 pb-3.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono font-bold text-sm text-teal-300">
                      {apr.approval_id}
                    </span>
                    <span
                      className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border ${statusTone(
                        apr.status,
                      )}`}
                    >
                      {apr.status}
                    </span>
                    <span className="px-2 py-0.5 rounded bg-navy-950 border border-navy-800 font-mono text-[10px] text-slate-300">
                      Order {apr.order_id}
                    </span>
                    <span className="px-2 py-0.5 rounded bg-navy-950 border border-navy-800 font-mono text-[10px] text-slate-400">
                      Role: {apr.required_role}
                    </span>
                    <span
                      className={`px-2 py-0.5 rounded font-mono text-[10px] font-semibold uppercase ${
                        apr.recommendation === "approve"
                          ? "bg-emerald-950/60 text-emerald-300 border border-emerald-500/30"
                          : "bg-amber-950/60 text-amber-300 border border-amber-500/30"
                      }`}
                    >
                      Rec: {apr.recommendation}
                    </span>
                  </div>

                  <div className="flex items-center space-x-2">
                    <button
                      type="button"
                      onClick={() => handleOpenTrace(apr)}
                      className="inline-flex items-center space-x-1 px-2.5 py-1.5 rounded-lg bg-navy-950 hover:bg-navy-800 border border-navy-700 text-slate-300 font-medium"
                    >
                      <Eye className="h-3.5 w-3.5" />
                      <span>Trace</span>
                    </button>

                    {actionable && (
                      <>
                        <button
                          type="button"
                          onClick={() => openDecisionDialog(apr, "approve")}
                          className="inline-flex items-center space-x-1 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold"
                        >
                          <ThumbsUp className="h-3.5 w-3.5" />
                          <span>Approve</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => openDecisionDialog(apr, "reject")}
                          className="inline-flex items-center space-x-1 px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-700 text-white font-semibold"
                        >
                          <ThumbsDown className="h-3.5 w-3.5" />
                          <span>Reject</span>
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {/* Financials + Rule Results + Risk */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                  {/* Computed Amount Breakdown */}
                  <div className="bg-navy-950/80 border border-navy-800/80 rounded-xl p-3.5 space-y-2">
                    <div className="text-[10px] font-mono uppercase text-slate-400 font-semibold">
                      Computed Amount ({apr.method} / {apr.resolution})
                    </div>
                    <div className="text-lg font-bold text-white tabular-nums">
                      {formatMoney(apr.computed_amount?.total_paise ?? apr.amount_paise)}
                    </div>
                    {apr.computed_amount && (
                      <div className="text-[11px] text-slate-400 space-y-0.5 font-mono">
                        <div>Items: {formatMoney(apr.computed_amount.items_paise)}</div>
                        <div>
                          Shipping Refund: {formatMoney(apr.computed_amount.shipping_refund_paise)}
                        </div>
                        <div>Return Fee: {formatMoney(apr.computed_amount.fee_paise)}</div>
                      </div>
                    )}
                    <div className="text-[11px] text-slate-400 pt-1">
                      Due: {formatISTWithRelative(apr.due_at, true)}
                    </div>
                  </div>

                  {/* Policy Rule Checks */}
                  <div className="bg-navy-950/80 border border-navy-800/80 rounded-xl p-3.5 space-y-2">
                    <div className="text-[10px] font-mono uppercase text-slate-400 font-semibold">
                      Deterministic Rule Checks (v{apr.policy_version})
                    </div>
                    {apr.rule_results.length === 0 ? (
                      <p className="text-slate-400">No detailed rules attached.</p>
                    ) : (
                      <ul className="space-y-1.5 max-h-36 overflow-y-auto custom-scrollbar pr-1">
                        {apr.rule_results.map((r, i) => {
                          const passed = Boolean(r.passed);
                          return (
                            <li
                              key={String(r.id || i)}
                              className="flex items-start space-x-2 text-[11px]"
                            >
                              <span
                                className={`mt-0.5 h-2 w-2 rounded-full flex-shrink-0 ${
                                  passed ? "bg-emerald-400" : "bg-amber-400"
                                }`}
                              />
                              <span className="text-slate-300">
                                <span className="font-mono text-slate-400">
                                  {String(r.id || `rule-${i}`)}:
                                </span>{" "}
                                {String(r.seller_text || (passed ? "Passed" : "Flagged"))}
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>

                  {/* Risk & Context */}
                  <div className="bg-navy-950/80 border border-navy-800/80 rounded-xl p-3.5 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-mono uppercase text-slate-400 font-semibold">
                        Risk Profile &amp; History
                      </span>
                      <span
                        className={`px-2 py-0.5 rounded font-mono text-[10px] uppercase font-bold ${
                          riskBand === "high"
                            ? "bg-red-950 text-red-300"
                            : riskBand === "medium"
                              ? "bg-amber-950 text-amber-300"
                              : "bg-emerald-950 text-emerald-300"
                        }`}
                      >
                        {riskBand} risk
                      </span>
                    </div>
                    <div className="text-[11px] text-slate-300">
                      Signals:{" "}
                      {riskSignals.length > 0 ? riskSignals.join(", ") : "None flagged"}
                    </div>
                    {apr.similar_past_decisions && (
                      <div className="text-[11px] font-mono text-slate-400">
                        Past Decisions: {apr.similar_past_decisions.approved_count} approved •{" "}
                        {apr.similar_past_decisions.rejected_count} rejected •{" "}
                        {apr.similar_past_decisions.same_order_count} same order
                      </div>
                    )}
                    {apr.recommendation_reason && (
                      <div className="text-[11px] text-teal-300/90">
                        {apr.recommendation_reason}
                      </div>
                    )}
                  </div>
                </div>

                {/* Sandboxed Untrusted Customer Input */}
                {apr.unverified_customer_statement && (
                  <div className="bg-amber-950/20 border border-amber-500/30 rounded-xl p-3 space-y-1">
                    <div className="flex items-center space-x-1.5 text-[10px] font-mono uppercase text-amber-300 font-bold">
                      <ShieldAlert className="h-3.5 w-3.5" />
                      <span>{apr.unverified_customer_statement.label}</span>
                    </div>
                    <p className="text-slate-200 font-mono text-[11px] break-words">
                      {apr.unverified_customer_statement.text}
                    </p>
                  </div>
                )}

                {/* Revalidation Diff Warning */}
                {apr.revalidation_diff && apr.revalidation_diff.length > 0 && (
                  <div className="bg-orange-950/30 border border-orange-500/40 rounded-xl p-3 space-y-1">
                    <div className="flex items-center space-x-1.5 text-[10px] font-mono uppercase text-orange-300 font-bold">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      <span>Live Revalidation Drift Detected</span>
                    </div>
                    <pre className="text-[11px] font-mono text-orange-200 overflow-x-auto">
                      {JSON.stringify(apr.revalidation_diff, null, 2)}
                    </pre>
                  </div>
                )}

                {apr.decision_reason && (
                  <div className="text-[11px] text-slate-400 font-mono">
                    Seller note ({apr.decided_by || "seller"}): {apr.decision_reason}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {/* Decision Modal */}
      {actionTarget && actionType && (
        <div
          className="fixed inset-0 z-50 bg-slate-950/75 backdrop-blur-sm flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="decision-modal-title"
        >
          <form
            onSubmit={handleSubmitDecision}
            className="bg-[#0B1426] border border-navy-700 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl"
          >
            <div className="flex items-center justify-between">
              <h2 id="decision-modal-title" className="text-base font-bold text-white">
                {actionType === "approve" ? "Approve Proposal" : "Reject Proposal"}{" "}
                <span className="font-mono text-teal-300">{actionTarget.approval_id}</span>
              </h2>
              <button
                type="button"
                onClick={() => setActionTarget(null)}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white"
                aria-label="Close decision dialog"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <p className="text-xs text-slate-400">
              Order <strong className="text-white">{actionTarget.order_id}</strong> &bull; Amount{" "}
              <strong className="text-white">
                {formatMoney(actionTarget.computed_amount?.total_paise ?? actionTarget.amount_paise)}
              </strong>{" "}
              &bull; Version v{actionTarget.expected_version}
            </p>

            <div>
              <label
                htmlFor="approval-decision-reason"
                className="block text-xs font-semibold text-slate-300 mb-1.5"
              >
                Decision Reason {actionType === "reject" ? "(Required)" : "(Optional)"}
              </label>
              <textarea
                id="approval-decision-reason"
                rows={3}
                value={reasonText}
                onChange={(e) => setReasonText(e.target.value)}
                required={actionType === "reject"}
                placeholder={
                  actionType === "reject"
                    ? "Enter clear reason for rejecting..."
                    : "Optional audit note..."
                }
                className="w-full rounded-xl bg-navy-950 border border-navy-800 p-3 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
              />
            </div>

            {modalError && (
              <div className="text-xs text-red-300 bg-red-950/50 border border-red-500/40 rounded-lg p-2.5">
                {modalError}
              </div>
            )}

            <div className="flex items-center justify-end space-x-2 pt-2">
              <button
                type="button"
                onClick={() => setActionTarget(null)}
                className="px-4 py-2 rounded-xl bg-navy-900 hover:bg-navy-800 border border-navy-700 text-xs font-medium text-slate-300"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting}
                className={`px-4 py-2 rounded-xl text-xs font-semibold text-white transition-colors ${
                  actionType === "approve"
                    ? "bg-emerald-600 hover:bg-emerald-700"
                    : "bg-red-600 hover:bg-red-700"
                }`}
              >
                {submitting
                  ? "Submitting..."
                  : actionType === "approve"
                    ? "Confirm Approval"
                    : "Confirm Rejection"}
              </button>
            </div>
          </form>
        </div>
      )}

      <DecisionTraceDrawer
        requestId={traceRequestId}
        onClose={() => setTraceRequestId(null)}
      />
    </div>
  );
}
