import React from "react";
import { AlertCircle, RefreshCw, X } from "lucide-react";

export interface ErrorBannerCardProps {
  title?: string;
  message: string;
  code?: string;
  requestId?: string;
  onRetry?: () => void;
  onDismiss?: () => void;
}

export const ErrorBannerCard: React.FC<ErrorBannerCardProps> = ({
  title = "Something went wrong",
  message,
  code,
  requestId,
  onRetry,
  onDismiss,
}) => {
  return (
    <div
      data-testid="error-banner-card"
      className="bg-white rounded-xl border border-red-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      <div className="bg-red-500 text-white p-3 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <AlertCircle className="h-4 w-4" />
          <span className="font-bold text-xs">{title}</span>
        </div>

        {code && (
          <span className="font-mono text-[10px] bg-red-600 px-1.5 py-0.5 rounded text-red-100">
            {code}
          </span>
        )}
      </div>

      <div className="p-3.5 space-y-2.5">
        <p className="text-slate-700 text-xs leading-relaxed">{message}</p>

        {requestId && (
          <div className="text-[10px] text-slate-400 font-mono">
            Request ID: <span className="text-slate-600">{requestId}</span>
          </div>
        )}

        {(onRetry || onDismiss) && (
          <div className="pt-2 border-t border-slate-100 flex items-center justify-end space-x-2">
            {onDismiss && (
              <button
                onClick={onDismiss}
                className="py-1 px-2.5 rounded-lg border border-slate-200 hover:bg-slate-50 text-slate-600 text-xs font-medium transition-colors flex items-center space-x-1"
              >
                <X className="h-3 w-3" />
                <span>Dismiss</span>
              </button>
            )}

            {onRetry && (
              <button
                onClick={onRetry}
                className="py-1 px-2.5 rounded-lg bg-red-600 hover:bg-red-700 text-white text-xs font-semibold transition-colors flex items-center space-x-1 shadow-sm"
              >
                <RefreshCw className="h-3 w-3" />
                <span>Try Again</span>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

