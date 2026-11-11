import React from "react";
import { Clock, MapPin, ShieldAlert, Truck } from "lucide-react";
import { formatISTWithRelative } from "../../../utils/date";

export interface ShipmentEventItem {
  eventId: string;
  ts: string;
  status: string;
  location: string;
  note: string;
}

export interface ShipmentTimelineCardProps {
  orderId: string;
  carrier: string;
  trackingNo: string;
  etaAt?: string | null;
  delayed?: boolean;
  events: ShipmentEventItem[];
}

export const ShipmentTimelineCard: React.FC<ShipmentTimelineCardProps> = ({
  orderId,
  carrier,
  trackingNo,
  etaAt,
  delayed = false,
  events,
}) => {
  return (
    <div
      data-testid="shipment-timeline-card"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      {/* Header */}
      <div className="bg-navy-900 text-white p-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="h-7 w-7 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
            <Truck className="h-4 w-4" />
          </div>
          <div>
            <div className="font-bold text-sm leading-tight">Shipment Tracking</div>
            <div className="text-[10px] text-slate-300 font-mono mt-0.5">{orderId}</div>
          </div>
        </div>

        {delayed && (
          <span className="flex items-center space-x-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-100 text-amber-900 border border-amber-300">
            <ShieldAlert className="h-3 w-3 text-amber-600" />
            <span>Delayed</span>
          </span>
        )}
      </div>

      {/* Meta Grid */}
      <div className="p-3 bg-slate-50 border-b border-slate-100 grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <span className="text-slate-400 block text-[10px] uppercase font-semibold">Carrier</span>
          <span className="font-semibold text-slate-900">{carrier}</span>
        </div>
        <div>
          <span className="text-slate-400 block text-[10px] uppercase font-semibold">Tracking #</span>
          <span className="font-mono font-semibold text-slate-900">{trackingNo}</span>
        </div>
        <div className="col-span-2 pt-1 border-t border-slate-200/60 flex justify-between items-center">
          <span className="text-slate-500">Estimated Delivery:</span>
          <span className="font-semibold text-navy-900">
            {etaAt ? formatISTWithRelative(etaAt, false) : "Pending update"}
          </span>
        </div>
      </div>

      {/* Events Timeline */}
      <div className="p-3.5 space-y-3">
        <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Activity History</p>
        <div className="relative pl-4 space-y-3 border-l-2 border-slate-200 ml-1.5">
          {events.map((ev, idx) => {
            const isLatest = idx === events.length - 1;
            return (
              <div key={ev.eventId} className="relative">
                <div
                  className={`absolute -left-[21px] top-0.5 h-3 w-3 rounded-full border-2 bg-white flex items-center justify-center ${
                    isLatest ? "border-teal-600 text-teal-600" : "border-slate-300"
                  }`}
                >
                  {isLatest && <div className="h-1 w-1 rounded-full bg-teal-600"></div>}
                </div>

                <div className="space-y-0.5">
                  <div className="flex items-center space-x-1.5 text-[10px] text-slate-400">
                    <Clock className="h-3 w-3" />
                    <span>{formatISTWithRelative(ev.ts, true)}</span>
                  </div>
                  <div className="font-semibold text-slate-900 text-xs">{ev.note}</div>
                  <div className="text-[10px] text-slate-500 flex items-center space-x-1">
                    <MapPin className="h-2.5 w-2.5 text-slate-400" />
                    <span>{ev.location}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

