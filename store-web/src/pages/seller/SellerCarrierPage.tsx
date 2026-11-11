import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  FastForward,
  MapPin,
  Play,
  RefreshCw,
  Search,
  Truck,
} from "lucide-react";
import { apiClient, authFetch } from "../../api/client";
import type { components } from "../../api/schema";
import { formatISTWithRelative } from "../../utils/date";

type ShipmentDetail = components["schemas"]["ShipmentResponse"];

export function SellerCarrierPage() {
  const [targetShipmentId, setTargetShipmentId] = useState<string>("SHP-1048");
  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [selectedScript, setSelectedScript] = useState<string>("normal_delivery");
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const fetchShipment = useCallback(async (id: string) => {
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      // Use order shipment route or direct lookup
      const ordId = id.startsWith("ORD-") ? id : id.replace("SHP-", "ORD-");
      const { data, error: apiErr } = await apiClient.GET("/api/v1/orders/{order_id}/shipment", {
        params: { path: { order_id: ordId } },
      });

      if (apiErr || !data) {
        throw new Error("Shipment not found for this identifier.");
      }

      setShipment(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load shipment");
      setShipment(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchShipment(targetShipmentId);
  }, [fetchShipment, targetShipmentId]);

  const handleAdvance = async () => {
    if (!targetShipmentId) return;
    setActionLoading(true);
    setError(null);
    setMessage(null);

    try {
      const res = await authFetch(`/api/v1/sim/carrier/shipments/${targetShipmentId}/advance`, {
        method: "POST",
        credentials: "include",
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Advance failed (${res.status})`);
      }

      const data = await res.json();
      setShipment(data);
      setMessage(`Shipment advanced to: ${data.status.replace("_", " ")}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to advance shipment");
    } finally {
      setActionLoading(false);
    }
  };

  const handlePlayScenario = async () => {
    if (!targetShipmentId) return;
    setActionLoading(true);
    setError(null);
    setMessage(null);

    try {
      const res = await authFetch(`/api/v1/sim/carrier/scenarios/${selectedScript}/play`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          shipment_id: targetShipmentId,
          script_id: selectedScript,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Scenario play failed (${res.status})`);
      }

      const data = await res.json();
      setShipment(data);
      setMessage(`Played '${selectedScript}' script to completion!`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to play carrier scenario");
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h1 className="text-2xl font-bold text-white tracking-tight">Carrier Simulator Console</h1>
            <span className="text-[10px] font-mono font-semibold uppercase bg-amber-900/60 text-amber-300 px-2 py-0.5 rounded border border-amber-500/30">
              SIMULATED
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1">
            Manually step shipments forward or execute end-to-end delivery scripts (SwiftShip Carrier).
          </p>
        </div>

        <button
          onClick={() => fetchShipment(targetShipmentId)}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Target Shipment Selector */}
      <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
        <h2 className="text-xs font-bold uppercase tracking-wider text-slate-400">
          Select Target Shipment
        </h2>

        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
            <input
              type="text"
              value={targetShipmentId}
              onChange={(e) => setTargetShipmentId(e.target.value)}
              placeholder="Shipment ID (e.g. SHP-1048) or Order ID (e.g. ORD-1048)"
              className="w-full pl-9 pr-4 py-2 bg-navy-950 border border-navy-800 rounded-xl text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-teal-500"
            />
          </div>

          <button
            onClick={() => fetchShipment(targetShipmentId)}
            className="px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-colors"
          >
            Load Shipment
          </button>
        </div>

        {/* Quick select presets */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <span className="text-[11px] text-slate-500">Quick presets:</span>
          {["SHP-1048", "SHP-2037", "SHP-2031", "SHP-1042"].map((preset) => (
            <button
              key={preset}
              onClick={() => {
                setTargetShipmentId(preset);
                fetchShipment(preset);
              }}
              className="px-2.5 py-1 rounded-lg bg-navy-950 hover:bg-navy-800 border border-navy-800 text-[11px] font-mono text-teal-300 transition-colors"
            >
              {preset}
            </button>
          ))}
        </div>
      </div>

      {/* Simulation Controls & Status Feedback */}
      {message && (
        <div className="p-3.5 rounded-xl bg-teal-950/40 border border-teal-500/40 text-teal-300 text-xs font-medium flex items-center space-x-2">
          <CheckCircle2 className="h-4 w-4 text-teal-400 flex-shrink-0" />
          <span>{message}</span>
        </div>
      )}

      {error && (
        <div className="p-3.5 rounded-xl bg-red-950/40 border border-red-500/40 text-red-300 text-xs font-medium flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Main Grid: Controls + Shipment Details */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left: Simulation Actions */}
        <div className="lg:col-span-5 space-y-4">
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-5">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">
              Carrier Controls
            </h3>

            {/* Action 1: Advance 1 step */}
            <div className="p-4 rounded-xl bg-navy-950/60 border border-navy-800 space-y-2">
              <div className="font-semibold text-xs text-slate-200">Single Step Advance</div>
              <p className="text-[11px] text-slate-400">
                Moves the selected shipment to its next progression milestone (e.g. picked up &rarr; in transit &rarr; out for delivery &rarr; delivered).
              </p>
              <button
                onClick={handleAdvance}
                disabled={actionLoading || !shipment}
                data-testid="carrier-advance-button"
                className="mt-2 w-full py-2 px-3 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors flex items-center justify-center space-x-1.5"
              >
                <FastForward className="h-4 w-4" />
                <span>{actionLoading ? "Advancing..." : "Advance 1 Step"}</span>
              </button>
            </div>

            {/* Action 2: Play full scenario script */}
            <div className="p-4 rounded-xl bg-navy-950/60 border border-navy-800 space-y-3">
              <div className="font-semibold text-xs text-slate-200">Play Script Scenario</div>
              <p className="text-[11px] text-slate-400">
                Executes all milestones of a scripted progression to completion.
              </p>

              <select
                value={selectedScript}
                onChange={(e) => setSelectedScript(e.target.value)}
                className="w-full bg-navy-900 border border-navy-700 text-slate-200 rounded-xl px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-teal-500"
              >
                <option value="normal_delivery">normal_delivery (Happy path delivery)</option>
                <option value="delayed_exception">delayed_exception (Weather delay exception)</option>
                <option value="lost_in_transit">lost_in_transit (Lost package in transit)</option>
                <option value="rto">rto (Return to origin)</option>
              </select>

              <button
                onClick={handlePlayScenario}
                disabled={actionLoading || !shipment}
                data-testid="carrier-play-button"
                className="w-full py-2 px-3 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors flex items-center justify-center space-x-1.5"
              >
                <Play className="h-4 w-4" />
                <span>{actionLoading ? "Playing..." : `Play Scenario (${selectedScript})`}</span>
              </button>
            </div>
          </div>
        </div>

        {/* Right: Shipment Detail & Timeline */}
        <div className="lg:col-span-7">
          {loading ? (
            <div className="bg-navy-900 border border-navy-800 rounded-2xl p-6 space-y-4 animate-pulse">
              <div className="h-5 bg-navy-800 rounded w-1/3"></div>
              <div className="h-4 bg-navy-800 rounded w-1/2"></div>
              <div className="h-24 bg-navy-800 rounded w-full"></div>
            </div>
          ) : shipment ? (
            <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-6 space-y-5">
              {/* Header */}
              <div className="flex items-center justify-between pb-3 border-b border-navy-800">
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="font-mono font-bold text-white text-base">
                      {shipment.shipment_id}
                    </span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                        shipment.status === "delivered"
                          ? "bg-teal-900/60 text-teal-300 border border-teal-500/30"
                          : shipment.status === "lost"
                          ? "bg-red-900/60 text-red-300 border border-red-500/30"
                          : "bg-blue-900/60 text-blue-300 border border-blue-500/30"
                      }`}
                    >
                      {shipment.status.replace("_", " ")}
                    </span>
                    {shipment.delayed && (
                      <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-900/60 text-amber-300 border border-amber-500/40">
                        <AlertTriangle className="h-3 w-3" />
                        <span>DELAYED</span>
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-slate-400 mt-1">
                    Order: <span className="font-mono text-teal-300">{shipment.order_id}</span>
                  </p>
                </div>

                <div className="text-right">
                  <span className="text-[10px] uppercase font-bold text-slate-500 block">
                    Carrier Tracking
                  </span>
                  <span className="font-mono text-xs font-semibold text-slate-200">
                    {shipment.tracking_no}
                  </span>
                </div>
              </div>

              {/* Meta info */}
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 p-3.5 rounded-xl bg-navy-950/60 border border-navy-800 text-xs">
                <div>
                  <span className="text-[10px] uppercase text-slate-500 block">Carrier</span>
                  <span className="font-semibold text-slate-200">{shipment.carrier}</span>
                </div>
                <div>
                  <span className="text-[10px] uppercase text-slate-500 block">Estimated ETA</span>
                  <span className="font-semibold text-slate-200">
                    {shipment.eta_at ? formatISTWithRelative(shipment.eta_at, false) : "Pending"}
                  </span>
                </div>
                <div>
                  <span className="text-[10px] uppercase text-slate-500 block">Script Loaded</span>
                  <span className="font-mono text-teal-300 text-[11px]">
                    {shipment.script_id || "default"}
                  </span>
                </div>
              </div>

              {/* Events timeline */}
              <div className="space-y-3 pt-2">
                <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400">
                  Tracking Event Log ({shipment.events?.length || 0})
                </h4>

                <div className="space-y-4 pl-4 border-l-2 border-navy-700 ml-2">
                  {shipment.events?.map((ev, idx) => (
                    <div key={ev.event_id || idx} className="relative group text-xs space-y-1">
                      <div className="absolute -left-[23px] top-1 h-3 w-3 rounded-full border-2 border-teal-500 bg-navy-900"></div>

                      <div className="flex items-center space-x-2 text-slate-400 text-[11px]">
                        <span>{formatISTWithRelative(ev.ts, true)}</span>
                        <span>&bull;</span>
                        <span className="text-slate-300 flex items-center space-x-1">
                          <MapPin className="h-3 w-3" />
                          <span>{ev.location}</span>
                        </span>
                      </div>

                      <div className="font-semibold text-slate-200 flex items-center space-x-2">
                        <span>{ev.note}</span>
                        <span className="px-1.5 py-0.2 rounded bg-navy-950 text-[10px] text-teal-300 border border-navy-800 uppercase">
                          {ev.status}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-12 text-center text-slate-500 space-y-2">
              <Truck className="h-8 w-8 mx-auto text-slate-700" />
              <p className="text-sm font-semibold text-slate-400">No shipment loaded</p>
              <p className="text-xs">Enter a shipment or order ID above to control its tracking.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
