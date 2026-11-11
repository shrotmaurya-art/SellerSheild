import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Package,
  RefreshCw,
  RotateCcw,
  ThumbsDown,
  ThumbsUp,
  X,
  XCircle,
} from "lucide-react";
import { apiClient, authFetch } from "../../api/client";
import type { components } from "../../api/schema";
import { formatISTWithRelative } from "../../utils/date";

type ReturnItem = components["schemas"]["ReturnResponse"];
type EvidenceItem = components["schemas"]["EvidenceResponse"];

export function SellerReturnsPage() {
  const [returns, setReturns] = useState<ReturnItem[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Action dialog states
  const [actionTarget, setActionTarget] = useState<ReturnItem | null>(null);
  const [actionType, setActionType] = useState<"approve" | "reject" | null>(null);
  const [reasonText, setReasonText] = useState<string>("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  // Evidence modal state
  const [selectedReturn, setSelectedReturn] = useState<ReturnItem | null>(null);
  const [evidenceList, setEvidenceList] = useState<EvidenceItem[]>([]);
  const [loadingEvidence, setLoadingEvidence] = useState(false);

  const fetchReturns = async () => {
    setLoading(true);
    setError(null);
    try {
      const { data, error: apiErr } = await apiClient.GET("/api/v1/returns");
      if (apiErr) {
        throw new Error("Failed to load returns queue");
      }
      setReturns(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load returns");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchReturns();
  }, []);

  const openApproveDialog = (ret: ReturnItem) => {
    setActionTarget(ret);
    setActionType("approve");
    setReasonText("Verified customer complaint; return approved for inspection");
    setActionError(null);
  };

  const openRejectDialog = (ret: ReturnItem) => {
    setActionTarget(ret);
    setActionType("reject");
    setReasonText("");
    setActionError(null);
  };

  const handleApproveReject = async () => {
    if (!actionTarget || !actionType) return;
    if (!reasonText.trim()) {
      setActionError("A reason is required to approve or reject a return.");
      return;
    }

    setActionLoading(true);
    setActionError(null);
    try {
      const idemKey = `ret-trans-${actionTarget.return_id}-${Date.now()}`;
      const toStatus = actionType === "approve" ? "approved" : "rejected";

      const res = await authFetch(`/api/v1/returns/${actionTarget.return_id}/transition`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
        body: JSON.stringify({
          to: toStatus,
          reason: reasonText.trim(),
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Failed to transition return (${res.status})`);
      }

      setActionTarget(null);
      setActionType(null);
      setReasonText("");
      fetchReturns();
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : "Transition failed");
    } finally {
      setActionLoading(false);
    }
  };

  const handleReceive = async (ret: ReturnItem) => {
    setActionLoading(true);
    try {
      // If still in 'approved', advance pickup and transit first
      if (ret.status === "approved") {
        await authFetch(`/api/v1/returns/${ret.return_id}/transition`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `ret-pickup-${ret.return_id}-${Date.now()}`,
          },
          credentials: "include",
          body: JSON.stringify({ to: "pickup_scheduled", reason: "Pickup completed" }),
        });
        await authFetch(`/api/v1/returns/${ret.return_id}/transition`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `ret-transit-${ret.return_id}-${Date.now()}`,
          },
          credentials: "include",
          body: JSON.stringify({ to: "in_transit_back", reason: "In transit to facility" }),
        });
      } else if (ret.status === "pickup_scheduled") {
        await authFetch(`/api/v1/returns/${ret.return_id}/transition`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `ret-transit-${ret.return_id}-${Date.now()}`,
          },
          credentials: "include",
          body: JSON.stringify({ to: "in_transit_back", reason: "In transit to facility" }),
        });
      }

      // Call real admin receive endpoint
      const idemKey = `ret-recv-${ret.return_id}-${Date.now()}`;
      const res = await authFetch(`/api/v1/admin/returns/${ret.return_id}/receive`, {
        method: "POST",
        headers: {
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Receive failed (${res.status})`);
      }

      fetchReturns();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Failed to receive item");
    } finally {
      setActionLoading(false);
    }
  };

  const handleInspect = async (ret: ReturnItem, result: "ok" | "failed") => {
    setActionLoading(true);
    try {
      const idemKey = `ret-insp-${ret.return_id}-${Date.now()}`;
      const toState = result === "ok" ? "inspected_ok" : "inspection_failed";
      const note = result === "ok" ? "Item inspected in good returnable condition" : "Item damaged or incomplete";

      const res = await authFetch(`/api/v1/admin/returns/${ret.return_id}/inspect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
        body: JSON.stringify({
          result,
          to: toState,
          note,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Inspection failed (${res.status})`);
      }

      fetchReturns();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Inspection failed");
    } finally {
      setActionLoading(false);
    }
  };

  const openEvidence = async (ret: ReturnItem) => {
    setSelectedReturn(ret);
    setLoadingEvidence(true);
    setEvidenceList([]);
    try {
      const res = await authFetch(`/api/v1/orders/${ret.order_id}/evidence`, {
        credentials: "include",
      });
      if (res.ok) {
        const data = await res.json();
        setEvidenceList(data.evidence || []);
      }
    } catch {
      // ignore
    } finally {
      setLoadingEvidence(false);
    }
  };

  const filteredReturns = returns.filter((r) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "pending") return r.status === "requested";
    if (statusFilter === "active") return ["approved", "pickup_scheduled", "in_transit_back", "received"].includes(r.status);
    if (statusFilter === "completed") return r.status === "completed" || r.status === "inspected_ok";
    return r.status === statusFilter;
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Returns Queue</h1>
          <p className="text-xs text-slate-400 mt-1">
            Review customer claims, authorize returns, receive warehouse shipments, and record inspection outcomes.
          </p>
        </div>

        <button
          onClick={fetchReturns}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh Queue</span>
        </button>
      </div>

      {/* Filter Tabs */}
      <div className="flex items-center space-x-2 border-b border-navy-800 pb-3 overflow-x-auto text-xs">
        <button
          onClick={() => setStatusFilter("all")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            statusFilter === "all"
              ? "bg-teal-500/20 text-teal-300 border border-teal-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          All ({returns.length})
        </button>
        <button
          onClick={() => setStatusFilter("pending")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            statusFilter === "pending"
              ? "bg-amber-500/20 text-amber-300 border border-amber-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          Pending Review ({returns.filter((r) => r.status === "requested").length})
        </button>
        <button
          onClick={() => setStatusFilter("active")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            statusFilter === "active"
              ? "bg-blue-500/20 text-blue-300 border border-blue-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          In Progress ({returns.filter((r) => ["approved", "pickup_scheduled", "in_transit_back", "received"].includes(r.status)).length})
        </button>
        <button
          onClick={() => setStatusFilter("completed")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            statusFilter === "completed"
              ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          Completed
        </button>
      </div>

      {/* Returns List */}
      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
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
            onClick={fetchReturns}
            className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 text-xs font-semibold transition-colors"
          >
            Retry
          </button>
        </div>
      ) : filteredReturns.length === 0 ? (
        <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-12 text-center text-slate-400 space-y-2">
          <RotateCcw className="h-8 w-8 mx-auto text-slate-600" />
          <p className="text-sm font-semibold text-slate-300">No returns found</p>
          <p className="text-xs text-slate-500">There are no return requests matching this filter.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {filteredReturns.map((ret) => {
            const isPending = ret.status === "requested";
            const isApproved = ret.status === "approved";
            const isTransit = ["pickup_scheduled", "in_transit_back"].includes(ret.status);
            const isReceived = ret.status === "received";
            const isInspected = ret.status === "inspected_ok";
            const isCompleted = ret.status === "completed";

            return (
              <div
                key={ret.return_id}
                className={`bg-navy-900 rounded-2xl p-5 border transition-all ${
                  isPending
                    ? "border-amber-500/50 shadow-md shadow-amber-950/20"
                    : "border-navy-800/80 hover:border-navy-700"
                }`}
                data-testid={`return-item-${ret.return_id}`}
              >
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                  {/* Left: Info */}
                  <div className="space-y-2">
                    <div className="flex items-center space-x-3">
                      <span className="font-mono font-bold text-teal-300 text-base">
                        {ret.return_id}
                      </span>
                      <span className="text-xs font-mono text-slate-400">
                        Order: {ret.order_id}
                      </span>
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold uppercase ${
                          isPending
                            ? "bg-amber-900/60 text-amber-300 border border-amber-500/40"
                            : isApproved
                            ? "bg-blue-900/60 text-blue-300 border border-blue-500/40"
                            : isTransit
                            ? "bg-blue-900/60 text-blue-300"
                            : isReceived
                            ? "bg-purple-900/60 text-purple-300 border border-purple-500/40"
                            : isInspected || isCompleted
                            ? "bg-teal-900/60 text-teal-300 border border-teal-500/40"
                            : "bg-red-900/60 text-red-300 border border-red-500/40"
                        }`}
                      >
                        {ret.status.replace("_", " ")}
                      </span>
                    </div>

                    <div className="text-xs text-slate-300 space-y-1">
                      <p>
                        <span className="text-slate-400">Reason:</span>{" "}
                        <span className="font-semibold text-white capitalize">{ret.reason_code}</span>
                        {ret.reason_text && <span className="text-slate-400 italic"> — "{ret.reason_text}"</span>}
                      </p>
                      <p className="text-slate-400">
                        Customer: <span className="text-slate-200">{ret.customer_id}</span> &bull;{" "}
                        Initiated {formatISTWithRelative(ret.created_at, false)}
                      </p>
                    </div>

                    {/* Items */}
                    <div className="flex items-center space-x-2 pt-1">
                      <span className="text-[11px] text-slate-400">Items to return:</span>
                      {ret.items.map((it) => (
                        <span
                          key={it.item_id}
                          className="px-2 py-0.5 rounded bg-navy-950 text-slate-300 text-[10px] font-mono border border-navy-800"
                        >
                          {it.item_id} (Qty: {it.qty})
                        </span>
                      ))}
                    </div>
                  </div>

                  {/* Right: Actions */}
                  <div className="flex flex-wrap items-center gap-2 pt-2 lg:pt-0 border-t lg:border-t-0 border-navy-800">
                    <button
                      onClick={() => openEvidence(ret)}
                      className="px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-300 text-xs font-medium transition-colors flex items-center space-x-1"
                    >
                      <FileText className="h-3.5 w-3.5 text-slate-400" />
                      <span>Photos</span>
                    </button>

                    {/* Pending Review Actions */}
                    {isPending && (
                      <>
                        <button
                          onClick={() => openApproveDialog(ret)}
                          data-testid="approve-return-button"
                          className="px-3.5 py-1.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-colors flex items-center space-x-1"
                        >
                          <ThumbsUp className="h-3.5 w-3.5" />
                          <span>Approve</span>
                        </button>
                        <button
                          onClick={() => openRejectDialog(ret)}
                          data-testid="reject-return-button"
                          className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 border border-red-500/40 text-xs font-semibold transition-colors flex items-center space-x-1"
                        >
                          <ThumbsDown className="h-3.5 w-3.5" />
                          <span>Reject</span>
                        </button>
                      </>
                    )}

                    {/* Approved / In-Transit Actions: Receive */}
                    {(isApproved || isTransit) && (
                      <button
                        onClick={() => handleReceive(ret)}
                        data-testid="receive-return-button"
                        className="px-3.5 py-1.5 rounded-xl bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold shadow-sm transition-colors flex items-center space-x-1"
                      >
                        <Package className="h-3.5 w-3.5" />
                        <span>Receive</span>
                      </button>
                    )}

                    {/* Received Actions: Inspect OK or Inspect Failed */}
                    {isReceived && (
                      <>
                        <button
                          onClick={() => handleInspect(ret, "ok")}
                          data-testid="inspect-ok-button"
                          className="px-3.5 py-1.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-colors flex items-center space-x-1"
                        >
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          <span>Inspect OK</span>
                        </button>
                        <button
                          onClick={() => handleInspect(ret, "failed")}
                          data-testid="inspect-fail-button"
                          className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 border border-red-500/40 text-xs font-semibold transition-colors flex items-center space-x-1"
                        >
                          <XCircle className="h-3.5 w-3.5" />
                          <span>Inspect Fail</span>
                        </button>
                      </>
                    )}

                    {/* Completed */}
                    {isCompleted && (
                      <span className="text-xs text-emerald-400 flex items-center space-x-1 font-semibold px-2 py-1">
                        <CheckCircle2 className="h-4 w-4" />
                        <span>Refunded</span>
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Approve / Reject Dialog */}
      {actionTarget && actionType && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-navy-900 border border-navy-800 rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-navy-800">
              <h3 className="text-base font-bold text-white">
                {actionType === "approve" ? "Approve Return Request" : "Reject Return Request"}
              </h3>
              <button
                onClick={() => setActionTarget(null)}
                className="text-slate-400 hover:text-white"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <p className="text-xs text-slate-300">
              Provide a clear audit reason for this return decision for return{" "}
              <span className="font-mono text-teal-300 font-bold">{actionTarget.return_id}</span>:
            </p>

            <textarea
              value={reasonText}
              onChange={(e) => setReasonText(e.target.value)}
              placeholder="Required decision reason..."
              rows={3}
              data-testid="return-action-reason"
              className="w-full px-3 py-2 bg-navy-950 border border-navy-800 rounded-xl text-xs text-white focus:outline-none focus:ring-2 focus:ring-teal-500"
            />

            {actionError && (
              <div className="text-xs text-red-400 bg-red-950/30 p-2.5 rounded-lg border border-red-500/30">
                {actionError}
              </div>
            )}

            <div className="flex items-center justify-end space-x-2 pt-2">
              <button
                onClick={() => setActionTarget(null)}
                className="px-3.5 py-1.5 rounded-xl border border-navy-700 bg-navy-800 text-slate-300 text-xs font-semibold hover:bg-navy-700 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleApproveReject}
                disabled={actionLoading || !reasonText.trim()}
                data-testid="confirm-return-action"
                className={`px-4 py-1.5 rounded-xl text-white text-xs font-semibold transition-colors disabled:opacity-50 ${
                  actionType === "approve"
                    ? "bg-teal-600 hover:bg-teal-700"
                    : "bg-red-600 hover:bg-red-700"
                }`}
              >
                {actionLoading ? "Submitting..." : `Confirm ${actionType === "approve" ? "Approval" : "Rejection"}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Evidence Photos Modal */}
      {selectedReturn && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-navy-900 border border-navy-800 rounded-2xl max-w-lg w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-navy-800">
              <h3 className="text-base font-bold text-white">
                Damage Photos ({selectedReturn.order_id})
              </h3>
              <button
                onClick={() => setSelectedReturn(null)}
                className="text-slate-400 hover:text-white"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {loadingEvidence ? (
              <div className="py-8 text-center text-xs text-slate-400">Loading photos...</div>
            ) : evidenceList.length === 0 ? (
              <div className="py-8 text-center text-xs text-slate-400">
                No evidence photos attached to this order.
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                {evidenceList.map((ev) => (
                  <div key={ev.evidence_id} className="border border-navy-800 rounded-xl overflow-hidden bg-navy-950 p-2">
                    <img
                      src={`/api/v1/evidence/${ev.evidence_id}`}
                      alt="Customer proof"
                      className="w-full h-32 object-cover rounded-lg"
                    />
                    <div className="text-[10px] text-slate-400 mt-1 truncate">
                      {ev.file_name} ({Math.round(ev.file_size_bytes / 1024)} KB)
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
