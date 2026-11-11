import React, { useEffect, useState } from "react";
import { AlertTriangle, Check, Clock, HelpCircle, X } from "lucide-react";

export interface ConfirmationDetailItem {
  label: string;
  value: string;
}

export interface ConfirmationCardProps {
  title?: string;
  description?: string;
  details?: ConfirmationDetailItem[];
  expiresAt?: Date | string | number;
  onConfirm?: () => Promise<void> | void;
  onCancel?: () => void;
  isConfirmed?: boolean;
}

export const ConfirmationCard: React.FC<ConfirmationCardProps> = ({
  title = "Confirm Action",
  description = "Please confirm this action to proceed. This request expires in 10 minutes.",
  details = [],
  expiresAt,
  onConfirm,
  onCancel,
  isConfirmed = false,
}) => {
  // Calculate expiration timestamp (defaults to 10 minutes from now)
  const [expiryTimestamp] = useState<number>(() => {
    if (!expiresAt) {
      return Date.now() + 10 * 60 * 1000;
    }
    if (typeof expiresAt === "number") {
      return expiresAt;
    }
    if (typeof expiresAt === "string") {
      return new Date(expiresAt).getTime();
    }
    return expiresAt.getTime();
  });

  const [remainingSeconds, setRemainingSeconds] = useState<number>(() => {
    const remaining = Math.max(0, Math.floor((expiryTimestamp - Date.now()) / 1000));
    return remaining;
  });

  const isSubmittingRef = React.useRef(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [confirmed, setConfirmed] = useState(isConfirmed);
  const [cancelled, setCancelled] = useState(false);

  const isExpired = remainingSeconds <= 0;

  useEffect(() => {
    if (isExpired || confirmed || cancelled) return;

    const timer = setInterval(() => {
      const remaining = Math.max(0, Math.floor((expiryTimestamp - Date.now()) / 1000));
      setRemainingSeconds(remaining);
      if (remaining <= 0) {
        clearInterval(timer);
      }
    }, 1000);

    return () => clearInterval(timer);
  }, [expiryTimestamp, isExpired, confirmed, cancelled]);

  // Format MM:SS
  const formatTime = (secs: number) => {
    const mins = Math.floor(secs / 60);
    const s = secs % 60;
    return `${mins.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  };

  // Double-click safe confirm handler
  const handleConfirm = async () => {
    if (isSubmittingRef.current || isExpired || confirmed || cancelled) {
      return;
    }
    isSubmittingRef.current = true;
    setIsSubmitting(true);
    try {
      if (onConfirm) {
        await onConfirm();
      }
      setConfirmed(true);
    } catch {
      // Allow retry if failed
      isSubmittingRef.current = false;
      setIsSubmitting(false);
    } finally {
      isSubmittingRef.current = false;
      setIsSubmitting(false);
    }
  };

  const handleCancel = () => {
    if (isSubmitting || isExpired || confirmed || cancelled) {
      return;
    }
    setCancelled(true);
    if (onCancel) {
      onCancel();
    }
  };

  return (
    <div
      data-testid="confirmation-card"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      {/* Header */}
      <div className="bg-navy-900 text-white p-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="h-7 w-7 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
            <HelpCircle className="h-4 w-4" />
          </div>
          <div>
            <div className="font-bold text-sm leading-tight">{title}</div>
            <div className="text-[10px] text-slate-300 mt-0.5">Action Confirmation Required</div>
          </div>
        </div>

        {/* Countdown Badge */}
        <div
          data-testid="countdown-badge"
          className={`flex items-center space-x-1 px-2.5 py-1 rounded-full text-[11px] font-mono font-bold border transition-colors ${
            isExpired
              ? "bg-red-50 text-red-600 border-red-200"
              : remainingSeconds < 120
              ? "bg-amber-50 text-amber-700 border-amber-300 animate-pulse"
              : "bg-teal-900/80 text-teal-200 border-teal-700"
          }`}
        >
          <Clock className="h-3 w-3" />
          <span>{isExpired ? "00:00 (Expired)" : formatTime(remainingSeconds)}</span>
        </div>
      </div>

      {/* Description & Details */}
      <div className="p-3.5 space-y-3">
        <p className="text-slate-600 text-xs leading-relaxed">{description}</p>

        {details.length > 0 && (
          <div className="bg-slate-50 rounded-lg p-2.5 border border-slate-100 space-y-1.5 text-[11px]">
            {details.map((item, idx) => (
              <div key={idx} className="flex justify-between items-center">
                <span className="text-slate-500">{item.label}</span>
                <span className="font-semibold text-slate-900">{item.value}</span>
              </div>
            ))}
          </div>
        )}

        {/* Status Messages */}
        {confirmed && (
          <div
            data-testid="confirmed-message"
            className="flex items-center space-x-2 bg-emerald-50 border border-emerald-200 text-emerald-800 p-2.5 rounded-lg text-xs font-medium"
          >
            <Check className="h-4 w-4 text-emerald-600 flex-shrink-0" />
            <span>Action confirmed successfully.</span>
          </div>
        )}

        {cancelled && (
          <div
            data-testid="cancelled-message"
            className="flex items-center space-x-2 bg-slate-100 border border-slate-200 text-slate-700 p-2.5 rounded-lg text-xs font-medium"
          >
            <X className="h-4 w-4 text-slate-500 flex-shrink-0" />
            <span>Action cancelled by user.</span>
          </div>
        )}

        {isExpired && !confirmed && !cancelled && (
          <div
            data-testid="expired-message"
            className="flex items-center space-x-2 bg-red-50 border border-red-200 text-red-700 p-2.5 rounded-lg text-xs font-medium"
          >
            <AlertTriangle className="h-4 w-4 text-red-500 flex-shrink-0" />
            <span>This confirmation request has expired (10 min limit).</span>
          </div>
        )}
      </div>

      {/* Action Buttons */}
      <div className="p-3 bg-slate-50 border-t border-slate-100 flex items-center space-x-2">
        <button
          type="button"
          data-testid="confirm-button"
          onClick={handleConfirm}
          disabled={isSubmitting || isExpired || confirmed || cancelled}
          className="flex-1 py-2 px-3 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-slate-200 text-white disabled:text-slate-400 font-semibold text-xs transition-colors shadow-sm disabled:cursor-not-allowed flex items-center justify-center space-x-1.5"
        >
          {isSubmitting ? (
            <>
              <div className="h-3 w-3 rounded-full border-2 border-white border-t-transparent animate-spin"></div>
              <span>Processing...</span>
            </>
          ) : (
            <>
              <Check className="h-3.5 w-3.5" />
              <span>Confirm</span>
            </>
          )}
        </button>

        <button
          type="button"
          data-testid="cancel-button"
          onClick={handleCancel}
          disabled={isSubmitting || isExpired || confirmed || cancelled}
          className="py-2 px-3 rounded-xl bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 disabled:text-slate-400 font-semibold text-xs transition-colors disabled:cursor-not-allowed flex items-center justify-center space-x-1"
        >
          <X className="h-3.5 w-3.5" />
          <span>Cancel</span>
        </button>
      </div>
    </div>
  );
};
