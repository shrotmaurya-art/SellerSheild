import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertCircle,
  ArrowLeft,
  Calendar,
  Clock,
  HelpCircle,
  MapPin,
  Package,
  RotateCcw,
  ShieldAlert,
  Truck,
} from "lucide-react";
import { apiClient } from "../api/client";
import type { paths } from "../api/schema";
import { ReturnModal } from "../components/ReturnModal";
import { formatISTWithRelative } from "../utils/date";
import { formatMoney } from "../utils/money";

type OrderDetail = paths["/api/v1/orders/{order_id}"]["get"]["responses"][200]["content"]["application/json"];
type ShipmentDetail = paths["/api/v1/orders/{order_id}/shipment"]["get"]["responses"][200]["content"]["application/json"];
type ReturnItem = paths["/api/v1/returns"]["get"]["responses"][200]["content"]["application/json"][0];

export function OrderDetailPage() {
  const { id: orderId } = useParams<{ id: string }>();

  const [order, setOrder] = useState<OrderDetail | null>(null);
  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [existingReturn, setExistingReturn] = useState<ReturnItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [shipmentError, setShipmentError] = useState<string | null>(null);
  const [isReturnModalOpen, setIsReturnModalOpen] = useState(false);

  useEffect(() => {
    async function loadOrder() {
      if (!orderId) return;
      setLoading(true);
      setError(null);
      setNotFound(false);
      setShipmentError(null);

      try {
        const { data: orderData, error: orderErr, response: orderRes } = await apiClient.GET(
          "/api/v1/orders/{order_id}",
          {
            params: {
              path: { order_id: orderId },
            },
          },
        );

        if (orderRes?.status === 404 || orderErr) {
          setNotFound(true);
          return;
        }

        if (orderData) {
          setOrder(orderData);

          // Attempt shipment fetch
          try {
            const { data: shpData, error: shpErr, response: shpRes } = await apiClient.GET(
              "/api/v1/orders/{order_id}/shipment",
              {
                params: {
                  path: { order_id: orderId },
                },
              },
            );

            if (shpRes?.status === 503 || shpErr) {
              setShipmentError("Carrier tracking is temporarily unavailable. Live carrier updates cannot be retrieved at this moment.");
            } else if (shpData) {
              setShipment(shpData);
            }
          } catch {
            setShipmentError("Carrier tracking service could not be reached.");
          }

          // Check for existing return for this order
          try {
            const { data: returnsData } = await apiClient.GET("/api/v1/returns");

            if (returnsData && Array.isArray(returnsData)) {
              const matched = returnsData.find((r) => r.order_id === orderId);
              if (matched) {
                setExistingReturn(matched);
              }
            }
          } catch {
            // Returns fetch error can fail gracefully
          }
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load order details");
      } finally {
        setLoading(false);
      }
    }

    loadOrder();
  }, [orderId]);

  // Loading skeleton
  if (loading) {
    return (
      <div className="space-y-6 animate-pulse">
        <div className="h-5 w-36 bg-slate-200 rounded"></div>
        <div className="bg-white rounded-2xl border border-slate-200/80 p-8 space-y-6 shadow-card">
          <div className="flex justify-between items-center">
            <div className="h-8 w-44 bg-slate-200 rounded"></div>
            <div className="h-6 w-24 bg-slate-200 rounded-full"></div>
          </div>
          <div className="h-32 bg-slate-100 rounded-xl"></div>
          <div className="h-24 bg-slate-100 rounded-xl"></div>
        </div>
      </div>
    );
  }

  // Uniform 404 Order Not Found Page
  if (notFound) {
    return (
      <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
        <div className="bg-white rounded-2xl border border-slate-200/80 p-8 shadow-card space-y-4">
          <div className="h-16 w-16 rounded-2xl bg-red-50 text-red-500 flex items-center justify-center mx-auto border border-red-100">
            <Package className="h-8 w-8" />
          </div>
          <div className="space-y-1">
            <h1 className="text-xl font-bold text-navy-950">Order not found</h1>
            <p className="text-xs sm:text-sm text-slate-500">
              The order identifier (<span className="font-mono font-semibold text-slate-700">{orderId}</span>) does not exist or you do not have permission to view it.
            </p>
          </div>
          <div className="pt-2">
            <Link
              to="/orders"
              className="inline-flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
            >
              <ArrowLeft className="h-4 w-4" />
              <span>Back to My Orders</span>
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (error || !order) {
    return (
      <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
        <div className="bg-white rounded-2xl border border-slate-200/80 p-8 shadow-card space-y-4">
          <AlertCircle className="h-12 w-12 text-red-500 mx-auto" />
          <h2 className="text-lg font-bold text-navy-950">Error Loading Order</h2>
          <p className="text-xs text-slate-500">{error || "Something went wrong"}</p>
          <Link
            to="/orders"
            className="inline-block text-xs text-teal-600 font-semibold hover:underline"
          >
            &larr; Return to orders list
          </Link>
        </div>
      </div>
    );
  }

  const scrollToShipment = () => {
    document.getElementById("shipment-section")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="space-y-6">
      {/* Navigation Breadcrumb */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-200/80">
        <div className="flex items-center space-x-2 text-xs">
          <Link
            to="/orders"
            className="inline-flex items-center space-x-1 font-semibold text-slate-500 hover:text-navy-950 transition-colors"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span>Orders</span>
          </Link>
          <span className="text-slate-300">/</span>
          <span className="font-mono font-bold text-navy-950">{order.order_id}</span>
        </div>

        <div className="text-[11px] text-slate-400 font-mono hidden sm:block">
          Placed {formatISTWithRelative(order.placed_at, false)}
        </div>
      </div>

      {/* Order Header Card */}
      <div className="bg-white rounded-2xl border border-slate-200/80 p-6 sm:p-8 space-y-6 shadow-card card-hover-lift">
        <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between border-b border-slate-100 pb-5 gap-4">
          <div>
            <div className="flex items-center space-x-3">
              <h1 className="text-2xl sm:text-3xl font-extrabold font-mono text-navy-950 tracking-tight">
                {order.order_id}
              </h1>
              <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider bg-teal-50 text-teal-800 border border-teal-200">
                {order.status}
              </span>
            </div>

            <p className="text-xs text-slate-500 mt-1.5 flex items-center space-x-1.5">
              <Calendar className="h-3.5 w-3.5 text-slate-400" />
              <span>Placed on {formatISTWithRelative(order.placed_at, true)}</span>
            </p>

            {/* Active Return Banner Chip */}
            {existingReturn && (
              <div className="mt-3 flex items-center space-x-2" data-testid="active-return-chip">
                <span className="font-mono text-xs font-bold text-slate-800 bg-slate-100 px-2 py-0.5 rounded">
                  {existingReturn.return_id}
                </span>
                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-800 border border-amber-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500 mr-1.5 status-dot-pulse"></span>
                  {existingReturn.status === "requested"
                    ? "Waiting for seller review"
                    : existingReturn.status === "approved"
                    ? "Approved"
                    : existingReturn.status.replace(/_/g, " ")}
                </span>
              </div>
            )}
          </div>

          {/* Action Buttons */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={scrollToShipment}
              className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-semibold transition-all btn-tactile"
            >
              <Truck className="h-4 w-4 text-teal-600" />
              <span>Track Delivery</span>
            </button>

            {existingReturn ? (
              <Link
                to="/returns"
                className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-amber-50 hover:bg-amber-100 text-amber-900 border border-amber-200 text-xs font-semibold transition-all btn-tactile"
              >
                <RotateCcw className="h-4 w-4 text-amber-600" />
                <span>Return ({existingReturn.return_id})</span>
              </Link>
            ) : (
              <button
                onClick={() => setIsReturnModalOpen(true)}
                disabled={order.status !== "delivered"}
                title={
                  order.status === "delivered"
                    ? "Start a return for this order"
                    : "Returns are only available once the order is delivered"
                }
                className={`inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl text-xs font-semibold transition-all btn-tactile ${
                  order.status === "delivered"
                    ? "bg-slate-100 hover:bg-slate-200 text-slate-800 border border-slate-200"
                    : "bg-slate-100 text-slate-400 cursor-not-allowed opacity-60"
                }`}
              >
                <RotateCcw className="h-4 w-4 text-teal-600" />
                <span>Request Return</span>
              </button>
            )}

            <button
              onClick={() => {
                const btn = document.querySelector<HTMLButtonElement>("button[aria-label='Open SellerShield Assistant chat']");
                btn?.click();
              }}
              className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
            >
              <HelpCircle className="h-4 w-4" />
              <span>Ask Support</span>
            </button>
          </div>
        </div>

        {/* Items Breakdown Table */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
              Purchased Items ({order.items.length})
            </h2>
          </div>

          <div className="divide-y divide-slate-100 border border-slate-200/80 rounded-xl overflow-hidden bg-slate-50/40">
            {order.items.map((item) => (
              <div
                key={item.item_id}
                className="p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 text-xs sm:text-sm hover:bg-white transition-colors"
              >
                <div className="space-y-1">
                  <div className="font-bold text-navy-950">{item.name_snapshot}</div>
                  <div className="text-[11px] text-slate-500 font-mono flex items-center space-x-2">
                    <span>SKU: {item.sku}</span>
                    <span>&bull;</span>
                    <span className="capitalize">{item.category_snapshot}</span>
                    <span>&bull;</span>
                    <span className="font-semibold text-slate-700">Quantity: {item.qty}</span>
                  </div>
                </div>

                <div className="sm:text-right space-y-0.5">
                  <div className="font-bold font-mono tabular-nums text-navy-950 text-sm sm:text-base">
                    {formatMoney(item.line_paid_paise)}
                  </div>
                  {item.discount_alloc_paise > 0 && (
                    <div className="text-[11px] text-emerald-600 font-semibold font-mono">
                      Discount: -{formatMoney(item.discount_alloc_paise)}
                    </div>
                  )}
                  <div className="text-[11px] text-slate-400 font-mono">
                    {formatMoney(item.unit_price_paise)} / unit
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Price Summary Breakdown */}
        <div className="border-t border-slate-100 pt-4 flex flex-col sm:items-end">
          <div className="w-full sm:w-80 space-y-2 text-xs sm:text-sm text-slate-600">
            <div className="flex justify-between">
              <span>Subtotal</span>
              <span className="font-mono font-medium text-slate-900">{formatMoney(order.subtotal_paise)}</span>
            </div>
            {order.discount_paise > 0 && (
              <div className="flex justify-between text-emerald-600 font-semibold font-mono">
                <span>Promotional Discount</span>
                <span>-{formatMoney(order.discount_paise)}</span>
              </div>
            )}
            <div className="flex justify-between">
              <span>Shipping & Logistics</span>
              <span className="font-mono">{order.shipping_fee_paise === 0 ? "FREE" : formatMoney(order.shipping_fee_paise)}</span>
            </div>
            <div className="flex justify-between border-t border-slate-200 pt-3 text-base font-bold text-navy-950 font-mono">
              <span>Total Paid</span>
              <span className="tabular-nums text-lg text-teal-800">{formatMoney(order.total_paise)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Shipment Timeline Section */}
      <div id="shipment-section" className="bg-white rounded-2xl border border-slate-200/80 p-6 sm:p-8 space-y-6 shadow-card">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between border-b border-slate-100 pb-4 gap-2">
          <div className="flex items-center space-x-2.5">
            <div className="h-8 w-8 rounded-lg bg-teal-50 text-teal-700 flex items-center justify-center">
              <Truck className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-navy-950">Shipment & Carrier Log</h2>
              <p className="text-[11px] text-slate-400">Chronological transit checkpoint records</p>
            </div>
          </div>

          {shipment?.delayed && (
            <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-bold bg-amber-100 text-amber-900 border border-amber-300 shadow-sm animate-pulse-subtle">
              <ShieldAlert className="h-3.5 w-3.5 mr-1 text-amber-600" />
              Delayed Shipment Notice
            </span>
          )}
        </div>

        {shipmentError ? (
          <div
            className="rounded-xl bg-amber-50 border border-amber-200 p-4 text-xs text-amber-800 flex items-start space-x-3"
            data-testid="carrier-error-banner"
          >
            <AlertCircle className="h-4 w-4 text-amber-600 mt-0.5 flex-shrink-0" />
            <div className="flex-1 space-y-1">
              <p className="font-semibold text-amber-900">Carrier Service Notice</p>
              <p className="text-amber-700">{shipmentError}</p>
              <button
                onClick={async () => {
                  if (!orderId) return;
                  const { data: retryShp, error: retryErr, response: retryRes } = await apiClient.GET(
                    "/api/v1/orders/{order_id}/shipment",
                    {
                      params: {
                        path: { order_id: orderId },
                      },
                    },
                  );
                  if (retryRes?.status === 503 || retryErr) {
                    setShipmentError("Carrier tracking is temporarily unavailable. Live carrier updates cannot be retrieved at this moment.");
                  } else if (retryShp) {
                    setShipment(retryShp);
                    setShipmentError(null);
                  }
                }}
                className="mt-2 inline-flex items-center space-x-1 px-3 py-1.5 rounded-lg bg-amber-200 hover:bg-amber-300 text-amber-950 font-semibold transition-all btn-tactile text-[11px]"
              >
                <span>Retry Tracking</span>
              </button>
            </div>
          </div>
        ) : shipment ? (
          <div className="space-y-6">
            {/* Shipment meta */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 bg-slate-50/70 p-4 rounded-xl border border-slate-200/80 text-xs">
              <div>
                <span className="text-slate-400 block font-semibold uppercase tracking-wider text-[10px]">
                  Fulfillment Carrier
                </span>
                <span className="font-bold text-navy-950 text-sm mt-0.5 block">
                  {shipment.carrier}
                </span>
              </div>

              <div>
                <span className="text-slate-400 block font-semibold uppercase tracking-wider text-[10px]">
                  Waybill / Tracking No.
                </span>
                <span className="font-mono font-bold text-navy-950 text-sm mt-0.5 block">
                  {shipment.tracking_no}
                </span>
              </div>

              <div>
                <span className="text-slate-400 block font-semibold uppercase tracking-wider text-[10px]">
                  Estimated Delivery ETA
                </span>
                <span className="font-bold text-navy-950 text-sm mt-0.5 block tabular-nums">
                  {shipment.eta_at ? formatISTWithRelative(shipment.eta_at, false) : "Pending Calculation"}
                </span>
              </div>
            </div>

            {/* Events Timeline */}
            <div className="relative pl-6 space-y-6 border-l-2 border-slate-200 ml-3">
              {shipment.events.map((ev, idx) => (
                <div key={ev.event_id} className="relative group">
                  {/* Timeline bullet icon */}
                  <div
                    className={`absolute -left-[31px] top-0.5 h-4 w-4 rounded-full border-2 bg-white flex items-center justify-center ${
                      idx === shipment.events.length - 1
                        ? "border-teal-600 text-teal-600 shadow-sm"
                        : "border-slate-300"
                    }`}
                  >
                    {idx === shipment.events.length - 1 && (
                      <div className="h-1.5 w-1.5 rounded-full bg-teal-600 status-dot-pulse"></div>
                    )}
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-center space-x-2 text-xs text-slate-500 font-mono">
                      <Clock className="h-3.5 w-3.5 text-slate-400" />
                      <span>{formatISTWithRelative(ev.ts, true)}</span>
                    </div>

                    <div className="text-sm font-semibold text-navy-950 flex items-center space-x-2">
                      <span>{ev.note}</span>
                      <span className="capitalize px-2 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px] font-mono">
                        {ev.status.replace("_", " ")}
                      </span>
                    </div>

                    <div className="text-xs text-slate-500 flex items-center space-x-1">
                      <MapPin className="h-3.5 w-3.5 text-slate-400" />
                      <span>{ev.location}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="text-center py-8 text-xs text-slate-500">
            Shipment label has not yet been generated for this order.
          </div>
        )}
      </div>

      {/* Return Initiation Modal */}
      {order && (
        <ReturnModal
          order={order}
          isOpen={isReturnModalOpen}
          onClose={() => setIsReturnModalOpen(false)}
          onSuccess={(returnId) => {
            setExistingReturn({
              return_id: returnId,
              order_id: order.order_id,
              customer_id: order.customer_id,
              status: "requested",
              reason_code: "defective",
              reason_text: "Item return requested",
              resolution: "refund",
              requested_via: "web",
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              version: 1,
              items: [],
            });
          }}
        />
      )}
    </div>
  );
}
