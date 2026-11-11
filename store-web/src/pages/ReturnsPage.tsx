import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Loader2,
  Package,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { apiClient } from "../api/client";
import type { components } from "../api/schema";
import { formatISTWithRelative } from "../utils/date";
import { formatMoney } from "../utils/money";

type ReturnItem = components["schemas"]["ReturnResponse"];
type RefundItem = components["schemas"]["RefundResponse"];

// 5-Stage Timeline
const TIMELINE_STEPS = [
  { key: "requested", label: "Requested" },
  { key: "approved", label: "Approved" },
  { key: "pickup", label: "Pickup & Transit" },
  { key: "received", label: "Inspected" },
  { key: "refunded", label: "Refunded" },
] as const;

function getStepIndex(status: string): number {
  switch (status) {
    case "requested":
      return 0;
    case "approved":
      return 1;
    case "pickup_scheduled":
    case "in_transit_back":
      return 2;
    case "received":
    case "inspected_ok":
      return 3;
    case "completed":
      return 4;
    default:
      return -1;
  }
}

export const ReturnsPage: React.FC = () => {
  const [returns, setReturns] = useState<ReturnItem[]>([]);
  const [refundsByOrder, setRefundsByOrder] = useState<{ [orderId: string]: RefundItem[] }>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Cancellation state
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<{ [id: string]: string }>({});

  const loadReturnsData = async () => {
    setLoading(true);
    setError(null);
    try {
      const { data: retData, error: retError } = await apiClient.GET("/api/v1/returns");

      if (retError) {
        setError(typeof retError === "string" ? retError : "Failed to load returns records");
        return;
      }

      if (retData && Array.isArray(retData)) {
        setReturns(retData);

        // Fetch refunds for distinct orders in the returns
        const orderIds = Array.from(new Set(retData.map((r) => r.order_id)));
        const refundMap: { [orderId: string]: RefundItem[] } = {};

        await Promise.all(
          orderIds.map(async (oid) => {
            try {
              const { data: rfdData } = await apiClient.GET("/api/v1/orders/{order_id}/refunds", {
                params: {
                  path: { order_id: oid },
                },
              });
              if (rfdData && Array.isArray(rfdData)) {
                refundMap[oid] = rfdData;
              }
            } catch {
              // Ignore single refund lookup failures
            }
          })
        );

        setRefundsByOrder(refundMap);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error connecting to returns API");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadReturnsData();
  }, []);

  // Cancel return request (allowed only while status is 'requested')
  const handleCancelReturn = async (returnId: string) => {
    if (cancellingId) return;

    setCancellingId(returnId);
    setCancelError((prev) => ({ ...prev, [returnId]: "" }));

    const idempotencyKey =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `cancel-${Date.now()}`;

    try {
      const { error: apiError, response } = await apiClient.POST(
        "/api/v1/returns/{return_id}/cancel",
        {
          params: {
            path: { return_id: returnId },
            header: {
              "Idempotency-Key": idempotencyKey,
            },
          },
        }
      );

      if (response?.ok) {
        // Update local state to cancelled
        setReturns((prev) =>
          prev.map((r) =>
            r.return_id === returnId
              ? {
                  ...r,
                  status: "cancelled",
                  updated_at: new Date().toISOString(),
                }
              : r
          )
        );
      } else {
        const msg =
          apiError && typeof apiError === "object" && "error" in apiError
            ? (apiError as { error: { message: string } }).error.message
            : "Failed to cancel return request";
        setCancelError((prev) => ({ ...prev, [returnId]: msg }));
      }
    } catch (err) {
      setCancelError((prev) => ({
        ...prev,
        [returnId]: err instanceof Error ? err.message : "Cancellation error",
      }));
    } finally {
      setCancellingId(null);
    }
  };

  // Group returns into active vs past
  const activeReturns = returns.filter(
    (r) => !["completed", "cancelled", "rejected", "inspection_failed"].includes(r.status)
  );
  const pastReturns = returns.filter((r) =>
    ["completed", "cancelled", "rejected", "inspection_failed"].includes(r.status)
  );

  return (
    <div className="space-y-8">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 pb-2 border-b border-slate-200/80">
        <div>
          <div className="flex items-center space-x-2 text-xs font-semibold text-teal-600 uppercase tracking-wider mb-1">
            <RotateCcw className="h-3.5 w-3.5" />
            <span>Customer Protection & Resolution</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-extrabold text-navy-950 tracking-tight">
            Returns & Refund Ledger
          </h1>
          <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-xl">
            Track multi-stage return inspection, reverse logistics timelines, and SimPay payment reversals.
          </p>
        </div>

        <button
          onClick={loadReturnsData}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-white hover:bg-slate-50 border border-slate-200/80 text-slate-700 text-xs font-semibold shadow-sm transition-all btn-tactile self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-teal-600" : ""}`} />
          <span>Refresh Records</span>
        </button>
      </div>

      {/* Loading Skeleton */}
      {loading && returns.length === 0 && (
        <div className="space-y-4 animate-pulse">
          <div className="h-44 bg-white rounded-2xl border border-slate-200/80 p-6 space-y-4 shadow-card">
            <div className="h-5 w-40 bg-slate-200 rounded"></div>
            <div className="h-4 w-60 bg-slate-100 rounded"></div>
            <div className="h-20 bg-slate-50 rounded-xl"></div>
          </div>
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="p-5 rounded-2xl bg-red-50 border border-red-200 text-red-700 flex items-start space-x-3 shadow-card animate-fade-in">
          <AlertCircle className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div className="space-y-1">
            <h3 className="font-bold text-sm">Failed to load returns</h3>
            <p className="text-xs">{error}</p>
            <button
              onClick={loadReturnsData}
              className="mt-2 text-xs font-semibold text-red-700 underline hover:text-red-900"
            >
              Try again
            </button>
          </div>
        </div>
      )}

      {/* Empty State */}
      {!loading && returns.length === 0 && !error && (
        <div className="bg-white rounded-2xl border border-slate-200/80 p-12 text-center shadow-card max-w-md mx-auto space-y-4 animate-fade-in">
          <div className="h-16 w-16 rounded-2xl bg-teal-50 text-teal-600 flex items-center justify-center mx-auto border border-teal-100">
            <ShieldCheck className="h-8 w-8" />
          </div>
          <div>
            <h3 className="text-base font-bold text-navy-950">No active return claims</h3>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">
              You do not have any pending or historical returns. You can initiate a return from any delivered order details page.
            </p>
          </div>
          <div className="pt-2">
            <Link
              to="/orders"
              className="inline-flex items-center space-x-1.5 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
            >
              <Package className="h-4 w-4" />
              <span>Browse Delivered Orders</span>
            </Link>
          </div>
        </div>
      )}

      {/* Returns List */}
      {returns.length > 0 && (
        <div className="space-y-8">
          {/* Active Returns Section */}
          <div className="space-y-4">
            <div className="flex items-center space-x-2">
              <span className="h-2 w-2 rounded-full bg-amber-500 status-dot-pulse"></span>
              <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500 font-mono">
                Active Return Claims ({activeReturns.length})
              </h2>
            </div>

            {activeReturns.length === 0 ? (
              <p className="text-xs text-slate-400 italic bg-white p-5 rounded-2xl border border-slate-200/80 shadow-card">
                No active return claims currently pending review.
              </p>
            ) : (
              <div className="space-y-5">
                {activeReturns.map((ret) => (
                  <ReturnCard
                    key={ret.return_id}
                    ret={ret}
                    refunds={refundsByOrder[ret.order_id] || []}
                    cancellingId={cancellingId}
                    cancelError={cancelError[ret.return_id]}
                    onCancel={() => handleCancelReturn(ret.return_id)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Past / Closed Returns Section */}
          {pastReturns.length > 0 && (
            <div className="space-y-4 pt-4 border-t border-slate-200/80">
              <div className="flex items-center space-x-2">
                <span className="h-2 w-2 rounded-full bg-slate-400"></span>
                <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500 font-mono">
                  Closed & Completed Returns ({pastReturns.length})
                </h2>
              </div>

              <div className="space-y-5">
                {pastReturns.map((ret) => (
                  <ReturnCard
                    key={ret.return_id}
                    ret={ret}
                    refunds={refundsByOrder[ret.order_id] || []}
                    cancellingId={cancellingId}
                    cancelError={cancelError[ret.return_id]}
                    onCancel={() => handleCancelReturn(ret.return_id)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Single Return Card with Timeline & Refund Status
// ---------------------------------------------------------------------------

interface ReturnCardProps {
  ret: ReturnItem;
  refunds: RefundItem[];
  cancellingId: string | null;
  cancelError?: string;
  onCancel: () => void;
}

const ReturnCard: React.FC<ReturnCardProps> = ({
  ret,
  refunds,
  cancellingId,
  cancelError,
  onCancel,
}) => {
  const currentStep = getStepIndex(ret.status);
  const isTerminalFailed = ["rejected", "cancelled", "inspection_failed"].includes(ret.status);
  const isCancelling = cancellingId === ret.return_id;

  // Find linked refund (if any)
  const linkedRefund = refunds.find((r) => r.return_id === ret.return_id) || refunds[0];

  const getStatusBadge = () => {
    switch (ret.status) {
      case "requested":
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-amber-50 text-amber-800 border border-amber-300">
            <span className="h-2 w-2 rounded-full bg-amber-500 mr-1.5 status-dot-pulse"></span>
            Waiting for seller review
          </span>
        );
      case "approved":
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-800 border border-emerald-300">
            <CheckCircle2 className="h-3.5 w-3.5 mr-1 text-emerald-600" />
            Approved
          </span>
        );
      case "completed":
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-teal-50 text-teal-800 border border-teal-300">
            <CheckCircle2 className="h-3.5 w-3.5 mr-1 text-teal-600" />
            Completed
          </span>
        );
      case "cancelled":
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-700 border border-slate-300">
            <XCircle className="h-3.5 w-3.5 mr-1 text-slate-500" />
            Cancelled
          </span>
        );
      case "rejected":
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-red-50 text-red-800 border border-red-300">
            <XCircle className="h-3.5 w-3.5 mr-1 text-red-600" />
            Rejected by Seller
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-800 border border-slate-200 capitalize">
            {ret.status.replace(/_/g, " ")}
          </span>
        );
    }
  };

  return (
    <div
      data-testid={`return-card-${ret.return_id}`}
      className="bg-white rounded-2xl border border-slate-200/80 shadow-card hover:shadow-card-hover transition-all p-6 sm:p-7 space-y-6 card-hover-lift"
    >
      {/* Return Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-slate-100 pb-5">
        <div>
          <div className="flex items-center space-x-3">
            <span className="font-mono font-bold text-lg text-navy-950">{ret.return_id}</span>
            {getStatusBadge()}
          </div>
          <p className="text-xs text-slate-500 mt-1.5 flex items-center space-x-2">
            <span>
              Order:{" "}
              <Link
                to={`/orders/${ret.order_id}`}
                className="font-mono font-semibold text-teal-700 hover:underline"
              >
                {ret.order_id}
              </Link>
            </span>
            <span>&bull;</span>
            <span className="flex items-center space-x-1 tabular-nums font-mono text-[11px]">
              <Clock className="h-3 w-3 text-slate-400" />
              <span>Created {formatISTWithRelative(ret.created_at, true)}</span>
            </span>
          </p>
        </div>

        {/* Cancel button before approval */}
        {ret.status === "requested" && (
          <div className="flex flex-col sm:items-end">
            <button
              onClick={onCancel}
              disabled={isCancelling}
              className="inline-flex items-center space-x-1.5 px-3.5 py-1.5 rounded-xl border border-red-200 hover:bg-red-50 text-red-700 text-xs font-semibold transition-all btn-tactile disabled:opacity-50"
            >
              {isCancelling ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>Cancelling...</span>
                </>
              ) : (
                <span>Cancel Return</span>
              )}
            </button>
            {cancelError && <p className="text-[10px] text-red-600 mt-1 font-mono">{cancelError}</p>}
          </div>
        )}
      </div>

      {/* 5-Step Timeline (Requested -> Approved -> Pickup -> Received -> Refunded) */}
      {!isTerminalFailed ? (
        <div className="space-y-2 py-2">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400 font-mono">
            Return Lifecycle Status
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 pt-2">
            {TIMELINE_STEPS.map((step, idx) => {
              const isPast = currentStep > idx;
              const isCurrent = currentStep === idx;
              return (
                <div key={step.key} className="space-y-2 text-center sm:text-left">
                  <div className="flex items-center">
                    <div
                      className={`h-7 w-7 rounded-full flex items-center justify-center text-xs font-bold transition-all ${
                        isPast
                          ? "bg-teal-600 text-white"
                          : isCurrent
                          ? "bg-teal-100 text-teal-800 border-2 border-teal-600 shadow-sm"
                          : "bg-slate-100 text-slate-400"
                      }`}
                    >
                      {isPast ? <CheckCircle2 className="h-4 w-4" /> : idx + 1}
                    </div>
                    {idx < TIMELINE_STEPS.length - 1 && (
                      <div
                        className={`hidden sm:block flex-1 h-1 ml-2 mr-2 rounded transition-colors ${
                          isPast ? "bg-teal-600" : "bg-slate-200"
                        }`}
                      />
                    )}
                  </div>
                  <div>
                    <p
                      className={`text-xs font-semibold ${
                        isCurrent
                          ? "text-teal-900"
                          : isPast
                          ? "text-slate-800"
                          : "text-slate-400"
                      }`}
                    >
                      {step.label}
                    </p>
                    <p className="text-[10px] text-slate-400 mt-0.5">
                      {isCurrent
                        ? "In Progress"
                        : isPast
                        ? "Completed"
                        : "Pending"}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="bg-slate-50 border border-slate-200 rounded-xl p-3.5 flex items-center space-x-2 text-xs text-slate-600 font-mono">
          <AlertCircle className="h-4 w-4 text-slate-500 flex-shrink-0" />
          <span>This return request was {ret.status} and is closed.</span>
        </div>
      )}

      {/* Return Reason & Details */}
      <div className="bg-slate-50/70 p-4 rounded-xl border border-slate-200/80 grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
        <div>
          <span className="text-slate-400 uppercase font-semibold text-[10px] block">Reason</span>
          <span className="font-semibold text-slate-900 capitalize mt-0.5 block">
            {ret.reason_code.replace(/_/g, " ")}
          </span>
          {ret.reason_text && (
            <p className="text-slate-500 italic mt-1 text-[11px]">&ldquo;{ret.reason_text}&rdquo;</p>
          )}
        </div>

        <div>
          <span className="text-slate-400 uppercase font-semibold text-[10px] block">
            Resolution
          </span>
          <span className="font-semibold text-slate-900 capitalize mt-0.5 block">
            {ret.resolution === "refund" ? "Original Payment Refund" : "Store Credit"}
          </span>
        </div>

        <div>
          <span className="text-slate-400 uppercase font-semibold text-[10px] block">
            Reviewed By
          </span>
          <span className="font-semibold text-slate-900 mt-0.5 block">
            {ret.approved_by || "Pending Review"}
          </span>
        </div>
      </div>

      {/* Refund Status Card with SIMULATED Badge */}
      {linkedRefund && (
        <div className="bg-gradient-to-r from-teal-50/60 to-slate-50 rounded-xl border border-teal-200/80 p-4 space-y-3 shadow-xs">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <span className="text-xs font-bold text-navy-950 uppercase tracking-wider">
                SimPay Settlement
              </span>
              {/* Mandatory SIMULATED Badge */}
              <span
                data-testid="simulated-badge"
                className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold bg-amber-100 text-amber-900 border border-amber-300 font-mono"
              >
                SIMULATED
              </span>
            </div>

            <span className="font-mono text-xs font-semibold text-slate-600">
              {linkedRefund.refund_id}
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
            <div>
              <span className="text-slate-400 text-[10px] uppercase font-semibold block">
                Refund Amount
              </span>
              <span className="font-bold font-mono tabular-nums text-base text-navy-950">
                {formatMoney(linkedRefund.amount_paise)}
              </span>
            </div>

            <div>
              <span className="text-slate-400 text-[10px] uppercase font-semibold block">
                Gateway State
              </span>
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 capitalize mt-0.5 font-mono">
                {linkedRefund.status}
              </span>
            </div>

            <div>
              <span className="text-slate-400 text-[10px] uppercase font-semibold block">
                SimPay Reference
              </span>
              <span className="font-mono text-slate-700 font-medium mt-0.5 block">
                {linkedRefund.gateway_ref || "SimPay Processing"}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
