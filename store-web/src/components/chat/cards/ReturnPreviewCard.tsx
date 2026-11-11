import React from "react";
import { CheckCircle2, Package, RotateCcw, ShieldCheck } from "lucide-react";
import { formatMoney } from "../../../utils/money";

export interface ReturnPreviewItem {
  name: string;
  qty: number;
  linePaidPaise: number;
}

export interface ReturnPreviewCardProps {
  orderId: string;
  items: ReturnPreviewItem[];
  reasonCode: string;
  reasonText: string;
  resolution: "refund" | "store_credit" | string;
  refundAmountPaise: number;
  shippingFeeNote?: string;
  policyNote?: string;
}

export const ReturnPreviewCard: React.FC<ReturnPreviewCardProps> = ({
  orderId,
  items,
  reasonCode,
  reasonText,
  resolution,
  refundAmountPaise,
  shippingFeeNote = "Free return pickup (seller policy)",
  policyNote = "Refund will be issued to original payment method once inspection is completed.",
}) => {
  return (
    <div
      data-testid="return-preview-card"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      {/* Header */}
      <div className="bg-navy-900 text-white p-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="h-7 w-7 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
            <RotateCcw className="h-4 w-4" />
          </div>
          <div>
            <div className="font-bold text-sm leading-tight">Return Request Preview</div>
            <div className="text-[10px] text-slate-300 font-mono mt-0.5">{orderId}</div>
          </div>
        </div>

        <span className="capitalize px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/20 text-amber-200 border border-amber-400/30">
          Preview
        </span>
      </div>

      {/* Items to Return */}
      <div className="p-3 bg-slate-50/60 border-b border-slate-100 space-y-2">
        <div className="text-[10px] uppercase font-bold text-slate-400">Items to Return</div>
        {items.map((item, idx) => (
          <div key={idx} className="flex items-center justify-between text-xs">
            <div className="flex items-center space-x-2">
              <Package className="h-3.5 w-3.5 text-slate-400" />
              <span className="font-medium text-slate-900">{item.name}</span>
              <span className="text-slate-400">×{item.qty}</span>
            </div>
            <span className="font-semibold text-slate-900">{formatMoney(item.linePaidPaise)}</span>
          </div>
        ))}
      </div>

      {/* Reason & Resolution details */}
      <div className="p-3 space-y-2 bg-white text-[11px]">
        <div className="grid grid-cols-2 gap-2">
          <div>
            <span className="text-slate-400 block text-[10px] uppercase font-semibold">Reason</span>
            <span className="font-medium text-slate-900 capitalize">{reasonCode.replace(/_/g, " ")}</span>
          </div>
          <div>
            <span className="text-slate-400 block text-[10px] uppercase font-semibold">Resolution</span>
            <span className="font-medium text-slate-900 capitalize">
              {resolution === "refund" ? "Original Payment" : "Store Credit"}
            </span>
          </div>
        </div>

        {reasonText && (
          <div className="bg-slate-50 p-2 rounded-lg border border-slate-100 text-slate-600 italic text-[11px]">
            &ldquo;{reasonText}&rdquo;
          </div>
        )}

        <div className="pt-2 border-t border-slate-100 flex items-center justify-between text-xs">
          <span className="font-medium text-slate-600">Calculated Refund:</span>
          <span className="font-bold text-navy-900 text-sm">{formatMoney(refundAmountPaise)}</span>
        </div>
      </div>

      {/* Return Shipping & Policy */}
      <div className="p-3 bg-teal-50/50 border-t border-teal-100/60 space-y-1.5 text-[10px] text-teal-900">
        <div className="flex items-center space-x-1.5 font-medium">
          <CheckCircle2 className="h-3.5 w-3.5 text-teal-600 flex-shrink-0" />
          <span>{shippingFeeNote}</span>
        </div>
        <div className="flex items-start space-x-1.5 text-slate-500">
          <ShieldCheck className="h-3.5 w-3.5 text-teal-600 flex-shrink-0 mt-0.5" />
          <span>{policyNote}</span>
        </div>
      </div>
    </div>
  );
};

