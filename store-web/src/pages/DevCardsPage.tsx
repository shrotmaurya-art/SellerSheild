import React, { useState } from "react";
import {
  CaseCreatedCard,
  ConfirmationCard,
  ErrorBannerCard,
  OrderSummaryCard,
  ReturnPreviewCard,
  ShipmentTimelineCard,
} from "../components/chat/cards";
import { Layers, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";

export const DevCardsPage: React.FC = () => {
  const isDemo = import.meta.env.VITE_DEMO_MODE === "true";

  const [confirmStatus, setConfirmStatus] = useState<string | null>(null);

  if (!isDemo) {
    return (
      <div className="max-w-md mx-auto py-16 px-4 text-center">
        <div className="bg-white rounded-2xl border border-slate-200 p-8 shadow-sm">
          <h2 className="text-lg font-bold text-navy-900">Developer Preview Unavailable</h2>
          <p className="text-xs text-slate-500 mt-2">
            This card showcase is only available when VITE_DEMO_MODE=true.
          </p>
          <Link to="/orders" className="mt-4 inline-block text-xs text-teal-600 font-semibold">
            &larr; Back to My Orders
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto py-8 px-4 sm:px-6 space-y-8">
      {/* Page Header */}
      <div className="bg-navy-900 text-white rounded-2xl p-6 sm:p-8 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border border-navy-800 shadow-sm">
        <div>
          <div className="flex items-center space-x-2 text-teal-400 text-xs font-semibold uppercase tracking-wider">
            <Sparkles className="h-4 w-4" />
            <span>Developer Card Showcase (Stage 10)</span>
          </div>
          <h1 className="text-2xl font-bold tracking-tight text-white mt-1">
            SellerShield Chat Card Components
          </h1>
          <p className="text-slate-300 text-xs sm:text-sm mt-1 max-w-2xl">
            Live preview of all six chat cards designed for agent integration. All components render with sample data and live interactive timers.
          </p>
        </div>

        <div className="flex items-center space-x-2">
          <span className="px-3 py-1 rounded-full text-xs font-mono font-semibold bg-teal-500/20 text-teal-300 border border-teal-500/30">
            DEMO_MODE ACTIVE
          </span>
        </div>
      </div>

      {/* Grid of Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 items-start">
        {/* Card 1: Order Summary */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>1. Order Summary Card</span>
          </div>
          <OrderSummaryCard
            orderId="ORD-1042"
            status="delivered"
            placedAt="2026-11-07T10:00:00Z"
            items={[
              {
                itemId: "ITEM-1042-1",
                name: "Non-stick Cookware Set",
                qty: 1,
                linePaidPaise: 120000,
                sku: "P-101",
              },
            ]}
            subtotalPaise={120000}
            discountPaise={0}
            shippingFeePaise={0}
            totalPaise={120000}
            onViewOrder={(id) => alert(`Navigating to order: ${id}`)}
          />
        </div>

        {/* Card 2: Shipment Timeline */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>2. Shipment Timeline Card</span>
          </div>
          <ShipmentTimelineCard
            orderId="ORD-1048"
            carrier="SwiftShip Express"
            trackingNo="SS1048001928"
            etaAt="2026-11-12T18:00:00Z"
            delayed={true}
            events={[
              {
                eventId: "EV-1",
                ts: "2026-11-09T08:30:00Z",
                status: "picked_up",
                location: "Bengaluru Hub",
                note: "Package picked up from seller warehouse",
              },
              {
                eventId: "EV-2",
                ts: "2026-11-09T14:15:00Z",
                status: "in_transit",
                location: "Hosur Sorting Center",
                note: "In transit to destination delivery center",
              },
              {
                eventId: "EV-3",
                ts: "2026-11-10T09:00:00Z",
                status: "exception",
                location: "Chennai Sorting Facility",
                note: "Address verification pending with recipient",
              },
            ]}
          />
        </div>

        {/* Card 3: Return Preview */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>3. Return Preview Card</span>
          </div>
          <ReturnPreviewCard
            orderId="ORD-1042"
            items={[
              {
                name: "Non-stick Cookware Set",
                qty: 1,
                linePaidPaise: 120000,
              },
            ]}
            reasonCode="defective"
            reasonText="Cookware handle cracked on first use."
            resolution="refund"
            refundAmountPaise={120000}
            shippingFeeNote="Free return pickup (seller policy for defective items)"
            policyNote="Refund will be credited back via SimPay once seller inspects the package."
          />
        </div>

        {/* Card 4: Confirmation Card with 10-minute expiry */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>4. Confirmation Card (10 Min Expiry)</span>
          </div>
          <ConfirmationCard
            title="Confirm Return Submission"
            description="Authorize return of Non-stick Cookware Set for ORD-1042. This confirmation token is valid for 10 minutes."
            details={[
              { label: "Order ID", value: "ORD-1042" },
              { label: "Item", value: "Non-stick Cookware Set (1x)" },
              { label: "Reason", value: "Defective item" },
              { label: "Estimated Refund", value: "₹1,200.00" },
            ]}
            onConfirm={async () => {
              await new Promise((resolve) => setTimeout(resolve, 800));
              setConfirmStatus("Action successfully confirmed by user!");
            }}
            onCancel={() => {
              setConfirmStatus("Action cancelled by user.");
            }}
          />
          {confirmStatus && (
            <p className="text-[11px] text-teal-700 bg-teal-50 border border-teal-200 p-2 rounded-lg font-mono">
              Callback status: {confirmStatus}
            </p>
          )}
        </div>

        {/* Card 5: Case Created */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>5. Escalation Case Created Card</span>
          </div>
          <CaseCreatedCard
            caseId="CASE-1042"
            orderId="ORD-1042"
            priority="medium"
            status="Open"
            subject="Damage photo verification required by seller"
            expectedResponse="Within 24 hours"
            assignedTeam="Priya Sharma (Seller Support)"
          />
        </div>

        {/* Card 6: Error Banner */}
        <div className="space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
            <Layers className="h-3.5 w-3.5 text-teal-600" />
            <span>6. Error Banner Card</span>
          </div>
          <ErrorBannerCard
            title="Return Submission Failed"
            message="An active return request (RET-1042) is already in progress for this item."
            code="duplicate"
            requestId="req-8fa943c2"
            onRetry={() => alert("Retrying request...")}
            onDismiss={() => alert("Error dismissed")}
          />
        </div>
      </div>
    </div>
  );
};
