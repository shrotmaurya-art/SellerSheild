import React from "react";
import { Calendar, Package, ShoppingBag } from "lucide-react";
import { formatMoney } from "../../../utils/money";
import { formatISTWithRelative } from "../../../utils/date";

export interface OrderSummaryItem {
  itemId: string;
  name: string;
  qty: number;
  linePaidPaise: number;
  sku?: string;
}

export interface OrderSummaryCardProps {
  orderId: string;
  status: string;
  placedAt: string;
  items: OrderSummaryItem[];
  subtotalPaise: number;
  discountPaise?: number;
  shippingFeePaise: number;
  totalPaise: number;
  onViewOrder?: (orderId: string) => void;
}

export const OrderSummaryCard: React.FC<OrderSummaryCardProps> = ({
  orderId,
  status,
  placedAt,
  items,
  subtotalPaise,
  discountPaise = 0,
  shippingFeePaise,
  totalPaise,
  onViewOrder,
}) => {
  return (
    <div
      data-testid="order-summary-card"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      {/* Header */}
      <div className="bg-navy-900 text-white p-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="h-7 w-7 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
            <ShoppingBag className="h-4 w-4" />
          </div>
          <div>
            <div className="font-mono font-bold text-sm tracking-tight">{orderId}</div>
            <div className="text-[10px] text-slate-300 flex items-center space-x-1 mt-0.5">
              <Calendar className="h-3 w-3 text-slate-400" />
              <span>{formatISTWithRelative(placedAt, true)}</span>
            </div>
          </div>
        </div>

        <span className="capitalize px-2 py-0.5 rounded-full text-[10px] font-semibold bg-teal-900/80 text-teal-200 border border-teal-700">
          {status}
        </span>
      </div>

      {/* Items List */}
      <div className="p-3 divide-y divide-slate-100 bg-slate-50/50">
        {items.map((item) => (
          <div key={item.itemId} className="py-2 first:pt-0 last:pb-0 flex items-center justify-between">
            <div className="flex items-start space-x-2">
              <Package className="h-4 w-4 text-slate-400 mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-semibold text-slate-900 leading-tight">{item.name}</p>
                <p className="text-[10px] text-slate-400 mt-0.5">
                  Qty: {item.qty} {item.sku && `• SKU: ${item.sku}`}
                </p>
              </div>
            </div>
            <span className="font-medium text-slate-900">{formatMoney(item.linePaidPaise)}</span>
          </div>
        ))}
      </div>

      {/* Price Summary */}
      <div className="p-3 border-t border-slate-100 space-y-1.5 bg-white">
        <div className="flex justify-between text-slate-500 text-[11px]">
          <span>Subtotal</span>
          <span>{formatMoney(subtotalPaise)}</span>
        </div>
        {discountPaise > 0 && (
          <div className="flex justify-between text-emerald-600 text-[11px]">
            <span>Discount</span>
            <span>-{formatMoney(discountPaise)}</span>
          </div>
        )}
        <div className="flex justify-between text-slate-500 text-[11px]">
          <span>Shipping</span>
          <span>{shippingFeePaise === 0 ? "FREE" : formatMoney(shippingFeePaise)}</span>
        </div>
        <div className="flex justify-between font-bold text-navy-900 pt-1.5 border-t border-slate-100 text-xs">
          <span>Total</span>
          <span>{formatMoney(totalPaise)}</span>
        </div>
      </div>

      {/* Action footer */}
      {onViewOrder && (
        <div className="p-2.5 bg-slate-50 border-t border-slate-100">
          <button
            onClick={() => onViewOrder(orderId)}
            className="w-full py-1.5 px-3 rounded-lg bg-teal-600 hover:bg-teal-700 text-white font-medium text-center text-xs transition-colors"
          >
            View Order Details
          </button>
        </div>
      )}
    </div>
  );
};

