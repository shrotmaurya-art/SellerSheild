import React, { useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Loader2,
  RotateCcw,
  Upload,
  X,
} from "lucide-react";
import { apiClient, authFetch, getAccessToken } from "../api/client";
import type { paths } from "../api/schema";
import { formatMoney } from "../utils/money";
import { Link } from "react-router-dom";

type OrderDetail = paths["/api/v1/orders/{order_id}"]["get"]["responses"][200]["content"]["application/json"];

export interface ReturnModalProps {
  order: OrderDetail;
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (returnId: string) => void;
}

const REASON_CODES = [
  { code: "defective", label: "Defective item" },
  { code: "damaged_in_transit", label: "Damaged during transit" },
  { code: "wrong_item", label: "Wrong item received" },
  { code: "not_as_described", label: "Not as described" },
  { code: "size_fit", label: "Size or fit issue" },
  { code: "changed_mind", label: "Changed mind / unwanted" },
];

const ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024; // 2 MB

export const ReturnModal: React.FC<ReturnModalProps> = ({
  order,
  isOpen,
  onClose,
  onSuccess,
}) => {
  // Step: "form" -> "confirm" -> "success"
  const [step, setStep] = useState<"form" | "confirm" | "success">("form");

  // Item selection: map of itemId -> qty
  const [selectedItems, setSelectedItems] = useState<{ [itemId: string]: number }>(() => {
    const initial: { [itemId: string]: number } = {};
    if (order.items && order.items.length > 0) {
      initial[order.items[0].item_id] = 1;
    }
    return initial;
  });

  const [reasonCode, setReasonCode] = useState<string>("defective");
  const [reasonText, setReasonText] = useState<string>("Cookware set has defects");
  const [resolution, setResolution] = useState<string>("refund");

  // Evidence upload states
  const [evidenceFile, setEvidenceFile] = useState<File | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [isUploadingEvidence, setIsUploadingEvidence] = useState(false);
  const [uploadedEvidenceId, setUploadedEvidenceId] = useState<string | null>(null);

  // Return submission states
  const isSubmittingRef = React.useRef(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [createdReturnId, setCreatedReturnId] = useState<string | null>(null);

  if (!isOpen) return null;

  const toggleItem = (itemId: string) => {
    setSelectedItems((prev) => {
      const copy = { ...prev };
      if (copy[itemId]) {
        delete copy[itemId];
      } else {
        copy[itemId] = 1;
      }
      return copy;
    });
  };

  const updateQty = (itemId: string, qty: number, maxQty: number) => {
    if (qty < 1 || qty > maxQty) return;
    setSelectedItems((prev) => ({
      ...prev,
      [itemId]: qty,
    }));
  };

  // Calculate refund estimate for selected items
  const selectedItemsList = order.items.filter((item) => selectedItems[item.item_id] > 0);
  const estimatedRefundPaise = selectedItemsList.reduce((acc, item) => {
    const qty = selectedItems[item.item_id] || 0;
    const unitPrice = Math.round(item.line_paid_paise / item.qty);
    return acc + unitPrice * qty;
  }, 0);

  // Handle Evidence File Selection with validation
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setEvidenceError(null);

    // 1. Check file type
    const isImage = ALLOWED_IMAGE_TYPES.includes(file.type) ||
      /\.(jpe?g|png|webp)$/i.test(file.name);

    if (!isImage) {
      setEvidenceError(
        `Invalid file type. Only JPEG, PNG, and WebP images are allowed (received "${file.name}").`
      );
      e.target.value = "";
      return;
    }

    // 2. Check file size (max 2 MB)
    if (file.size > MAX_UPLOAD_BYTES) {
      const sizeMB = (file.size / (1024 * 1024)).toFixed(2);
      setEvidenceError(
        `File size exceeds 2 MB limit (selected file is ${sizeMB} MB). Please choose a smaller image.`
      );
      e.target.value = "";
      return;
    }

    setEvidenceFile(file);
    setIsUploadingEvidence(true);

    try {
      const formData = new FormData();
      formData.append("file", file);

      const token = getAccessToken();
      const idempotencyKey =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `ev-${Date.now()}`;

      const res = await authFetch(`/api/v1/orders/${order.order_id}/evidence`, {
        method: "POST",
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          "Idempotency-Key": idempotencyKey,
        },
        body: formData,
      });

      if (!res.ok) {
        const errorJson = await res.json().catch(() => ({}));
        const errMsg = errorJson?.error?.message || "Failed to upload evidence";
        setEvidenceError(errMsg);
      } else {
        const data = await res.json();
        setUploadedEvidenceId(data.evidence_id || "uploaded");
      }
    } catch (err) {
      setEvidenceError(err instanceof Error ? err.message : "Evidence upload failed");
    } finally {
      setIsUploadingEvidence(false);
    }
  };

  // Double-click safe submission handler with once-per-attempt Idempotency-Key
  const handleFinalSubmit = async () => {
    if (isSubmittingRef.current || isSubmitting || selectedItemsList.length === 0) return;

    isSubmittingRef.current = true;
    setIsSubmitting(true);
    setSubmitError(null);

    const idempotencyKey =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `ret-${Date.now()}`;

    try {
      const payload = {
        items: selectedItemsList.map((item) => ({
          item_id: item.item_id,
          qty: selectedItems[item.item_id],
        })),
        reason_code: reasonCode,
        reason_text: reasonText.trim() || "Item return requested",
        resolution: resolution,
      };

      // Cast apiClient.POST as any because FastAPI generated requestBody as never
      // due to raw_body request handling for idempotency hashing
      const postFn = apiClient.POST as (
        path: string,
        init: {
          params?: { path?: { [k: string]: string }; header?: { [k: string]: string } };
          body?: unknown;
        }
      ) => Promise<{
        data?: { return_id: string };
        error?: unknown;
        response?: Response;
      }>;

      const { data, error, response } = await postFn("/api/v1/orders/{order_id}/returns", {
        params: {
          path: { order_id: order.order_id },
          header: {
            "Idempotency-Key": idempotencyKey,
          },
        },
        body: payload,
      });

      if (response?.status === 201 || (response?.ok && data)) {
        if (data) {
          setCreatedReturnId(data.return_id);
          setStep("success");
          onSuccess(data.return_id);
        }
      } else {
        const errMessage =
          error && typeof error === "object" && "error" in error
            ? (error as { error: { message: string } }).error.message
            : "Failed to submit return request.";
        setSubmitError(errMessage);
      }
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Network error during return submission");
    } finally {
      isSubmittingRef.current = false;
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-navy-950/60 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-lg overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="bg-navy-900 text-white p-4 sm:p-5 flex items-center justify-between border-b border-navy-800">
          <div className="flex items-center space-x-2.5">
            <div className="h-8 w-8 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
              <RotateCcw className="h-4 w-4" />
            </div>
            <div>
              <h3 className="font-bold text-sm tracking-tight">Return Request</h3>
              <p className="text-[11px] text-slate-300 font-mono mt-0.5">Order {order.order_id}</p>
            </div>
          </div>

          <button
            onClick={onClose}
            className="text-slate-400 hover:text-white p-1 rounded-lg transition-colors"
            aria-label="Close modal"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Modal Body */}
        <div className="flex-1 overflow-y-auto p-5 space-y-5 text-xs text-slate-800">
          {/* STEP 1: FORM */}
          {step === "form" && (
            <>
              {/* Item selection */}
              <div className="space-y-2">
                <label className="font-bold text-slate-700 block uppercase tracking-wider text-[10px]">
                  Choose items to return
                </label>
                <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden bg-slate-50/50">
                  {order.items.map((item) => {
                    const isChecked = Boolean(selectedItems[item.item_id]);
                    const currentQty = selectedItems[item.item_id] || 1;
                    return (
                      <div
                        key={item.item_id}
                        className="p-3 flex items-center justify-between gap-3 bg-white"
                      >
                        <div className="flex items-start space-x-3">
                          <input
                            type="checkbox"
                            checked={isChecked}
                            onChange={() => toggleItem(item.item_id)}
                            className="mt-1 h-4 w-4 rounded border-slate-300 text-teal-600 focus:ring-teal-500"
                            id={`item-${item.item_id}`}
                          />
                          <div>
                            <label
                              htmlFor={`item-${item.item_id}`}
                              className="font-semibold text-slate-900 cursor-pointer text-xs"
                            >
                              {item.name_snapshot}
                            </label>
                            <div className="text-[11px] text-slate-400 mt-0.5">
                              Ordered Qty: {item.qty} &bull; {formatMoney(item.unit_price_paise)} each
                            </div>
                          </div>
                        </div>

                        {isChecked && (
                          <div className="flex items-center space-x-1.5">
                            <span className="text-[10px] text-slate-400 font-medium">Qty:</span>
                            <select
                              value={currentQty}
                              onChange={(e) =>
                                updateQty(item.item_id, parseInt(e.target.value, 10), item.qty)
                              }
                              className="py-1 px-2 text-xs border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-teal-500 font-semibold"
                            >
                              {Array.from({ length: item.qty }, (_, i) => i + 1).map((n) => (
                                <option key={n} value={n}>
                                  {n}
                                </option>
                              ))}
                            </select>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Reason Code Dropdown */}
              <div className="space-y-1.5">
                <label className="font-bold text-slate-700 block uppercase tracking-wider text-[10px]">
                  Reason Code
                </label>
                <select
                  value={reasonCode}
                  onChange={(e) => setReasonCode(e.target.value)}
                  className="w-full text-xs py-2 px-3 rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 font-medium"
                >
                  {REASON_CODES.map((r) => (
                    <option key={r.code} value={r.code}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Reason Description Text */}
              <div className="space-y-1.5">
                <label className="font-bold text-slate-700 block uppercase tracking-wider text-[10px]">
                  Reason Details
                </label>
                <textarea
                  rows={2}
                  value={reasonText}
                  onChange={(e) => setReasonText(e.target.value)}
                  placeholder="Describe the issue or reason for returning this item..."
                  className="w-full text-xs p-3 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500"
                />
              </div>

              {/* Resolution options */}
              <div className="space-y-1.5">
                <label className="font-bold text-slate-700 block uppercase tracking-wider text-[10px]">
                  Preferred Resolution
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <label
                    className={`flex items-center space-x-2 p-2.5 rounded-xl border cursor-pointer transition-colors ${
                      resolution === "refund"
                        ? "border-teal-500 bg-teal-50/40 text-teal-900 font-semibold"
                        : "border-slate-200 hover:bg-slate-50 text-slate-700"
                    }`}
                  >
                    <input
                      type="radio"
                      name="resolution"
                      value="refund"
                      checked={resolution === "refund"}
                      onChange={() => setResolution("refund")}
                      className="text-teal-600 focus:ring-teal-500"
                    />
                    <span>Refund (Original Payment)</span>
                  </label>

                  <label
                    className={`flex items-center space-x-2 p-2.5 rounded-xl border cursor-pointer transition-colors ${
                      resolution === "store_credit"
                        ? "border-teal-500 bg-teal-50/40 text-teal-900 font-semibold"
                        : "border-slate-200 hover:bg-slate-50 text-slate-700"
                    }`}
                  >
                    <input
                      type="radio"
                      name="resolution"
                      value="store_credit"
                      checked={resolution === "store_credit"}
                      onChange={() => setResolution("store_credit")}
                      className="text-teal-600 focus:ring-teal-500"
                    />
                    <span>Store Credit</span>
                  </label>
                </div>
              </div>

              {/* Evidence Upload Section */}
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <div className="flex items-center justify-between">
                  <label className="font-bold text-slate-700 block uppercase tracking-wider text-[10px]">
                    Evidence Photo (Optional / Recommended)
                  </label>
                  <span className="text-[10px] text-slate-400">JPG, PNG, WebP &bull; Max 2 MB</span>
                </div>

                <div className="space-y-2">
                  <div className="flex items-center space-x-2">
                    <label className="cursor-pointer inline-flex items-center space-x-1.5 px-3 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold transition-colors border border-slate-200">
                      <Upload className="h-3.5 w-3.5 text-teal-600" />
                      <span>Choose Evidence File</span>
                      <input
                        type="file"
                        accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp,.txt"
                        onChange={handleFileChange}
                        className="hidden"
                      />
                    </label>

                    {isUploadingEvidence && (
                      <span className="inline-flex items-center space-x-1 text-teal-600">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        <span>Uploading...</span>
                      </span>
                    )}

                    {uploadedEvidenceId && !evidenceError && (
                      <span className="inline-flex items-center space-x-1 text-emerald-600 font-medium">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        <span>Photo attached ({evidenceFile?.name})</span>
                      </span>
                    )}
                  </div>

                  {/* Clear size and type error alert */}
                  {evidenceError && (
                    <div
                      data-testid="evidence-error"
                      className="p-2.5 rounded-xl bg-red-50 border border-red-200 text-red-700 flex items-start space-x-2"
                    >
                      <AlertCircle className="h-4 w-4 text-red-500 flex-shrink-0 mt-0.5" />
                      <div className="space-y-0.5">
                        <p className="font-semibold">Evidence Upload Error</p>
                        <p className="text-[11px] text-red-600 leading-normal">{evidenceError}</p>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Estimated Refund Preview */}
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-100 flex items-center justify-between">
                <div>
                  <span className="text-slate-500 text-[11px] block">Estimated Refund Amount</span>
                  <span className="text-[10px] text-slate-400">
                    {selectedItemsList.length} item(s) selected
                  </span>
                </div>
                <span className="text-base font-bold text-navy-900">
                  {formatMoney(estimatedRefundPaise)}
                </span>
              </div>
            </>
          )}

          {/* STEP 2: CONFIRM DIALOG */}
          {step === "confirm" && (
            <div className="space-y-4 py-2">
              <div className="bg-amber-50 border border-amber-200 rounded-xl p-3.5 text-amber-900 space-y-1">
                <div className="font-bold flex items-center space-x-1.5">
                  <AlertCircle className="h-4 w-4 text-amber-600" />
                  <span>Confirm Return Submission</span>
                </div>
                <p className="text-[11px] text-amber-800">
                  Please review the details of your return request. Once confirmed, this will be submitted to the seller for approval.
                </p>
              </div>

              <div className="bg-slate-50 p-3.5 rounded-xl border border-slate-200 space-y-2 text-[11px]">
                <div className="flex justify-between border-b border-slate-200/60 pb-2">
                  <span className="text-slate-500">Order ID:</span>
                  <span className="font-mono font-semibold text-slate-900">{order.order_id}</span>
                </div>

                <div className="space-y-1 py-1">
                  <span className="text-slate-500 block">Returning Items:</span>
                  {selectedItemsList.map((item) => (
                    <div key={item.item_id} className="flex justify-between font-medium text-slate-900 pl-2">
                      <span>{item.name_snapshot} (×{selectedItems[item.item_id]})</span>
                      <span>
                        {formatMoney(
                          Math.round(item.line_paid_paise / item.qty) * selectedItems[item.item_id]
                        )}
                      </span>
                    </div>
                  ))}
                </div>

                <div className="flex justify-between border-t border-slate-200/60 pt-2">
                  <span className="text-slate-500">Reason:</span>
                  <span className="font-semibold text-slate-900 capitalize">
                    {reasonCode.replace(/_/g, " ")}
                  </span>
                </div>

                <div className="flex justify-between">
                  <span className="text-slate-500">Resolution:</span>
                  <span className="font-semibold text-slate-900 capitalize">
                    {resolution === "refund" ? "Original Payment" : "Store Credit"}
                  </span>
                </div>

                {uploadedEvidenceId && (
                  <div className="flex justify-between">
                    <span className="text-slate-500">Evidence Attached:</span>
                    <span className="text-emerald-700 font-medium">Yes ({evidenceFile?.name})</span>
                  </div>
                )}

                <div className="flex justify-between border-t border-slate-200 pt-2 text-xs font-bold text-navy-900">
                  <span>Estimated Total Refund:</span>
                  <span className="text-sm">{formatMoney(estimatedRefundPaise)}</span>
                </div>
              </div>

              {submitError && (
                <div className="p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 flex items-start space-x-2">
                  <AlertCircle className="h-4 w-4 text-red-500 flex-shrink-0 mt-0.5" />
                  <p className="text-xs">{submitError}</p>
                </div>
              )}
            </div>
          )}

          {/* STEP 3: SUCCESS */}
          {step === "success" && (
            <div className="text-center py-6 space-y-4">
              <div className="h-14 w-14 rounded-full bg-teal-50 text-teal-600 flex items-center justify-center mx-auto">
                <CheckCircle2 className="h-8 w-8" />
              </div>

              <div className="space-y-1">
                <h4 className="text-base font-bold text-navy-900">Return Request Created</h4>
                <p className="text-xs text-slate-500">
                  Your return request has been submitted successfully with ID:
                </p>
                <div className="font-mono text-lg font-bold text-teal-700 py-1" data-testid="return-id">
                  {createdReturnId}
                </div>
              </div>

              {/* Status Chip */}
              <div className="inline-flex items-center px-3 py-1.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-800 border border-amber-300">
                <span className="h-2 w-2 rounded-full bg-amber-500 mr-2 animate-pulse"></span>
                <span>Waiting for seller review</span>
              </div>

              <p className="text-[11px] text-slate-500 max-w-xs mx-auto">
                The seller will review your request and evidence. You will be notified when your return is approved for pickup.
              </p>

              <div className="pt-2 flex items-center justify-center space-x-3">
                <Link
                  to="/returns"
                  onClick={onClose}
                  className="px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white font-semibold text-xs transition-colors shadow-sm"
                >
                  View in Returns & Refunds &rarr;
                </Link>
                <button
                  onClick={onClose}
                  className="px-4 py-2 rounded-xl border border-slate-200 hover:bg-slate-50 text-slate-700 font-semibold text-xs transition-colors"
                >
                  Done
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Modal Footer Controls */}
        {step !== "success" && (
          <div className="p-4 bg-slate-50 border-t border-slate-200 flex items-center justify-between">
            {step === "form" ? (
              <>
                <button
                  type="button"
                  onClick={onClose}
                  className="px-4 py-2 rounded-xl border border-slate-200 hover:bg-white text-slate-700 font-semibold text-xs transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={selectedItemsList.length === 0}
                  onClick={() => setStep("confirm")}
                  className="px-5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-slate-200 text-white disabled:text-slate-400 font-semibold text-xs transition-colors flex items-center space-x-1.5 shadow-sm disabled:cursor-not-allowed"
                >
                  <span>Review Return</span>
                  <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => setStep("form")}
                  disabled={isSubmitting}
                  className="px-4 py-2 rounded-xl border border-slate-200 hover:bg-white text-slate-700 font-semibold text-xs transition-colors disabled:opacity-50"
                >
                  Back
                </button>
                <button
                  type="button"
                  data-testid="confirm-return-btn"
                  disabled={isSubmitting}
                  onClick={handleFinalSubmit}
                  className="px-5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-slate-300 text-white disabled:text-slate-500 font-semibold text-xs transition-colors flex items-center space-x-1.5 shadow-sm disabled:cursor-not-allowed"
                >
                  {isSubmitting ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      <span>Submitting...</span>
                    </>
                  ) : (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      <span>Confirm Return</span>
                    </>
                  )}
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
