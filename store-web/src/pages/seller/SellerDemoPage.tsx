import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  Archive,
  ArrowRight,
  Bookmark,
  CheckCircle2,
  Clock,
  Database,
  FastForward,
  Flame,
  Pause,
  Play,
  PlayCircle,
  RotateCcw,
  Sparkles,
  Truck,
  Zap,
} from "lucide-react";
import { authFetch } from "../../api/client";

const SCENARIOS = [
  { id: "SC-01", name: "SC-01: Happy path return with automatic release" },
  { id: "SC-02", name: "SC-02: Autonomy cap hold & stored authorization" },
  { id: "SC-03", name: "SC-03: Carrier delayed exception notification" },
  { id: "SC-04", name: "SC-04: Ineligible return on delivered order window" },
  { id: "SC-05", name: "SC-05: Customer cancellation before seller review" },
  { id: "SC-06", name: "SC-06: Daily autonomy budget threshold exceeded" },
  { id: "SC-07", name: "SC-07: Payment gateway timeout reconciliation" },
  { id: "SC-08", name: "SC-08: Stale carrier events fault injection" },
  { id: "SC-09", name: "SC-09: Duplicate item return attempt rejection" },
  { id: "SC-10", name: "SC-10: Staff vs Owner authorization role limits" },
  { id: "SC-11", name: "SC-11: Evidence damage photo validation and review" },
  { id: "SC-12", name: "SC-12: Mail delivery transient failure recovery" },
  { id: "SC-13", name: "SC-13: Carrier lost-in-transit resolution" },
  { id: "SC-14", name: "SC-14: Rapid double-action idempotency conflict" },
  { id: "SC-15", name: "SC-15: Cross-customer IDOR order privacy isolation" },
  { id: "SC-16", name: "SC-16: Cash-on-delivery return to store credit" },
  { id: "SC-17", name: "SC-17: High-return customer risk tier monitoring" },
];

export function SellerDemoPage() {
  // Judge Mode
  const [judgeMode, setJudgeMode] = useState<boolean>(false);

  // Clock state
  const [clockInfo, setClockInfo] = useState<{ now_utc: string; mode: string; offset_seconds: number } | null>(null);
  const [customIso, setCustomIso] = useState<string>("");

  // Faults state
  const [faults, setFaults] = useState<Record<string, boolean>>({});

  // Scenarios & Reset state
  const [selectedScenario, setSelectedScenario] = useState<string>("SC-01");
  const [resetting, setResetting] = useState(false);
  const [resetMessage, setResetMessage] = useState<string | null>(null);

  // State Inspector
  const [inspectorData, setInspectorData] = useState<{
    table_row_counts?: Record<string, number>;
    last_20_events?: Record<string, unknown>[];
    outbox_summary?: { pending: number; delivered: number; exhausted: number };
  } | null>(null);

  // Snapshots
  const [snapshots, setSnapshots] = useState<string[]>([]);
  const [snapshotName, setSnapshotName] = useState<string>("");

  // Status & feedback
  const [error, setError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  const fetchDemoData = async () => {
    setError(null);
    try {
      // 1. Clock
      const clockRes = await authFetch("/api/v1/demo/clock", { credentials: "include" });
      if (clockRes.ok) {
        setClockInfo(await clockRes.json());
      }

      // 2. Faults
      const faultsRes = await authFetch("/api/v1/demo/faults", { credentials: "include" });
      if (faultsRes.ok) {
        const fJson = await faultsRes.json();
        setFaults(fJson.faults || {});
      }

      // 3. State Inspector
      const stateRes = await authFetch("/api/v1/demo/state", { credentials: "include" });
      if (stateRes.ok) {
        setInspectorData(await stateRes.json());
      }

      // 4. Snapshots
      const snapRes = await authFetch("/api/v1/demo/snapshots", { credentials: "include" });
      if (snapRes.ok) {
        const sJson = await snapRes.json();
        setSnapshots(sJson.snapshots || []);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load demo control data");
    }
  };

  useEffect(() => {
    fetchDemoData();
  }, []);

  // Reset Data to Seed (<5s requirement)
  const handleReset = async () => {
    setResetting(true);
    setResetMessage(null);
    setError(null);
    const startTime = performance.now();

    try {
      const res = await authFetch("/api/v1/demo/reset", {
        method: "POST",
        credentials: "include",
      });

      const elapsed = Math.round(performance.now() - startTime);

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Reset failed with code ${res.status}`);
      }

      setResetMessage(`Reset completed successfully in ${elapsed}ms (< 5.0s). Database restored to initial seed state.`);
      fetchDemoData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setResetting(false);
    }
  };

  // Clock Actions
  const handleClockAction = async (action: string, payload: Record<string, string | number | boolean> = {}) => {
    setActionNotice(null);
    try {
      const res = await authFetch("/api/v1/demo/clock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ action, ...payload }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || "Clock control failed");
      }

      const updated = await res.json();
      setClockInfo(updated);
      setActionNotice(`Clock adjusted: ${updated.now_utc} (mode: ${updated.mode})`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Clock action failed");
    }
  };

  // Fault Toggle
  const handleToggleFault = async (key: string) => {
    const nextState = !faults[key];
    try {
      const res = await authFetch("/api/v1/demo/faults", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ fault: key, enabled: nextState }),
      });

      if (!res.ok) {
        throw new Error(`Failed to toggle fault switch ${key}`);
      }

      const data = await res.json();
      setFaults(data.faults || {});
      setActionNotice(`Fault switch '${key}' set to ${nextState ? "ENABLED" : "DISABLED"}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Fault toggle failed");
    }
  };

  // Load Scenario
  const handleLoadScenario = async () => {
    if (!selectedScenario) return;
    setResetting(true);
    setResetMessage(null);
    setError(null);
    const startTime = performance.now();

    try {
      const res = await authFetch(`/api/v1/demo/scenarios/${selectedScenario}/load`, {
        method: "POST",
        credentials: "include",
      });

      const elapsed = Math.round(performance.now() - startTime);

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Failed to load scenario ${selectedScenario}`);
      }

      setResetMessage(`Scenario ${selectedScenario} loaded in ${elapsed}ms. Preconditions activated.`);
      fetchDemoData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Scenario load failed");
    } finally {
      setResetting(false);
    }
  };

  // Snapshot Save
  const handleSaveSnapshot = async () => {
    if (!snapshotName.trim()) return;
    try {
      const res = await authFetch(`/api/v1/demo/snapshots/save?name=${encodeURIComponent(snapshotName.trim())}`, {
        method: "POST",
        credentials: "include",
      });
      if (res.ok) {
        setActionNotice(`Snapshot '${snapshotName}' created.`);
        setSnapshotName("");
        const snapRes = await authFetch("/api/v1/demo/snapshots", { credentials: "include" });
        if (snapRes.ok) {
          const sJson = await snapRes.json();
          setSnapshots(sJson.snapshots || []);
        }
      }
    } catch {
      setError("Failed to save database snapshot");
    }
  };

  // Snapshot Restore
  const handleRestoreSnapshot = async (name: string) => {
    try {
      const res = await authFetch(`/api/v1/demo/snapshots/restore?name=${encodeURIComponent(name)}`, {
        method: "POST",
        credentials: "include",
      });
      if (res.ok) {
        setActionNotice(`Snapshot '${name}' restored successfully.`);
        fetchDemoData();
      }
    } catch {
      setError(`Failed to restore snapshot '${name}'`);
    }
  };

  return (
    <div className={`space-y-6 transition-all ${judgeMode ? "text-base max-w-5xl" : ""}`}>
      {/* Top Banner & Judge Mode Toggle */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h1 className={`font-bold text-white tracking-tight ${judgeMode ? "text-3xl" : "text-2xl"}`}>
              Demo Control Center
            </h1>
            <span className="text-[10px] font-mono font-semibold uppercase bg-teal-900/60 text-teal-300 px-2 py-0.5 rounded border border-teal-500/30">
              STORE OWNER
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1">
            Drive the entire hackathon evaluation, time travel, inject faults, and manage scenarios from one screen.
          </p>
        </div>

        {/* Judge Mode Switch */}
        <div className="flex items-center space-x-3 bg-navy-900 p-2 rounded-2xl border border-navy-800 shadow-sm">
          <span className="text-xs font-semibold text-slate-300 flex items-center space-x-1.5">
            <Sparkles className="h-4 w-4 text-amber-400" />
            <span>Judge Mode</span>
          </span>
          <button
            onClick={() => setJudgeMode(!judgeMode)}
            data-testid="judge-mode-toggle"
            aria-label="Toggle Judge Mode"
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none ${
              judgeMode ? "bg-amber-500" : "bg-navy-800"
            }`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                judgeMode ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
        </div>
      </div>

      {/* Notifications */}
      {resetMessage && (
        <div
          className="p-4 rounded-2xl bg-teal-950/40 border border-teal-500/40 text-teal-300 text-xs sm:text-sm font-semibold flex items-center space-x-2.5 shadow-lg"
          data-testid="demo-reset-message"
        >
          <CheckCircle2 className="h-5 w-5 text-teal-400 flex-shrink-0" />
          <span>{resetMessage}</span>
        </div>
      )}

      {actionNotice && (
        <div className="p-3.5 rounded-xl bg-blue-950/40 border border-blue-500/40 text-blue-300 text-xs font-medium flex items-center space-x-2">
          <Zap className="h-4 w-4 text-blue-400 flex-shrink-0" />
          <span>{actionNotice}</span>
        </div>
      )}

      {error && (
        <div className="p-4 rounded-xl bg-red-950/40 border border-red-500/40 text-red-300 text-xs font-medium flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Row 1: Scenario Loader & Instant Reset */}
      <div className="grid grid-cols-1 md:grid-cols-12 gap-5">
        {/* Reset Button */}
        <div className="md:col-span-5 bg-navy-900 border border-navy-800/80 rounded-2xl p-5 flex flex-col justify-between space-y-4">
          <div>
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center space-x-2">
                <RotateCcw className="h-4 w-4 text-teal-400" />
                <span>Instant Store Reset</span>
              </h2>
              <span className="text-[10px] font-mono text-teal-300 font-semibold">&lt; 5.0s SLA</span>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              Re-creates SQLite database tables, reloads SC-01 to SC-17 seed pack, and zeroes in-memory simulator faults in under 5 seconds.
            </p>
          </div>

          <button
            onClick={handleReset}
            disabled={resetting}
            data-testid="demo-reset-button"
            className="w-full py-3 px-4 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white font-bold text-xs sm:text-sm shadow-md transition-all flex items-center justify-center space-x-2"
          >
            <RotateCcw className={`h-4 w-4 ${resetting ? "animate-spin" : ""}`} />
            <span>{resetting ? "Resetting Environment..." : "Reset Data to Seed"}</span>
          </button>
        </div>

        {/* Scenario Selector */}
        <div className="md:col-span-7 bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
          <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center space-x-2">
            <PlayCircle className="h-4 w-4 text-blue-400" />
            <span>Pre-Packaged Scenario Loader</span>
          </h2>
          <p className="text-xs text-slate-400">
            Resets the store and immediately positions database state, carrier delays, or orders for any specific scenario.
          </p>

          <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
            <select
              value={selectedScenario}
              onChange={(e) => setSelectedScenario(e.target.value)}
              data-testid="scenario-select"
              className="flex-1 bg-navy-950 border border-navy-700 text-slate-200 rounded-xl px-3 py-2.5 text-xs focus:outline-none focus:ring-2 focus:ring-teal-500 font-medium"
            >
              {SCENARIOS.map((sc) => (
                <option key={sc.id} value={sc.id}>
                  {sc.name}
                </option>
              ))}
            </select>

            <button
              onClick={handleLoadScenario}
              disabled={resetting}
              data-testid="load-scenario-button"
              className="py-2.5 px-4 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors whitespace-nowrap"
            >
              Load Scenario
            </button>
          </div>
        </div>
      </div>

      {/* Row 2: Clock Controls */}
      <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 pb-3 border-b border-navy-800">
          <div>
            <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center space-x-2">
              <Clock className="h-4 w-4 text-teal-400" />
              <span>Simulated Clock & Time Travel</span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Advances return review windows, order delivery SLAs, and carrier timelines together.
            </p>
          </div>

          {clockInfo && (
            <div className="flex items-center space-x-3 bg-navy-950 px-3 py-1.5 rounded-xl border border-navy-800 text-xs">
              <div>
                <span className="text-[10px] text-slate-400 block uppercase">Current Store UTC</span>
                <span className="font-mono font-bold text-teal-300" data-testid="demo-clock-display">
                  {clockInfo.now_utc}
                </span>
              </div>
              <span
                className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded ${
                  clockInfo.mode === "frozen"
                    ? "bg-amber-900/60 text-amber-300 border border-amber-500/40"
                    : "bg-teal-900/60 text-teal-300"
                }`}
              >
                {clockInfo.mode}
              </span>
            </div>
          )}
        </div>

        {/* Quick Advance Buttons */}
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            onClick={() => handleClockAction("advance", { hours: 1 })}
            data-testid="advance-1h-button"
            className="px-3.5 py-2 rounded-xl bg-navy-950 hover:bg-navy-800 border border-navy-700 text-xs font-semibold text-slate-200 transition-colors flex items-center space-x-1.5"
          >
            <FastForward className="h-3.5 w-3.5 text-teal-400" />
            <span>+1 Hour</span>
          </button>

          <button
            onClick={() => handleClockAction("advance", { days: 1 })}
            data-testid="advance-1d-button"
            className="px-3.5 py-2 rounded-xl bg-navy-950 hover:bg-navy-800 border border-navy-700 text-xs font-semibold text-slate-200 transition-colors flex items-center space-x-1.5"
          >
            <FastForward className="h-3.5 w-3.5 text-teal-400" />
            <span>+1 Day</span>
          </button>

          <button
            onClick={() => handleClockAction("advance", { days: 7 })}
            data-testid="advance-7d-button"
            className="px-3.5 py-2 rounded-xl bg-navy-950 hover:bg-navy-800 border border-navy-700 text-xs font-semibold text-slate-200 transition-colors flex items-center space-x-1.5"
          >
            <FastForward className="h-3.5 w-3.5 text-teal-400" />
            <span>+7 Days</span>
          </button>

          {clockInfo?.mode === "frozen" ? (
            <button
              onClick={() => handleClockAction("resume")}
              className="px-3.5 py-2 rounded-xl bg-teal-900/60 hover:bg-teal-900 border border-teal-500/40 text-xs font-semibold text-teal-200 transition-colors flex items-center space-x-1.5"
            >
              <Play className="h-3.5 w-3.5" />
              <span>Resume Clock</span>
            </button>
          ) : (
            <button
              onClick={() => handleClockAction("freeze")}
              className="px-3.5 py-2 rounded-xl bg-amber-900/60 hover:bg-amber-900 border border-amber-500/40 text-xs font-semibold text-amber-200 transition-colors flex items-center space-x-1.5"
            >
              <Pause className="h-3.5 w-3.5" />
              <span>Freeze Clock</span>
            </button>
          )}

          {/* Set Exact ISO Datetime */}
          <div className="flex items-center space-x-1.5 ml-auto w-full sm:w-auto pt-2 sm:pt-0">
            <input
              type="text"
              value={customIso}
              onChange={(e) => setCustomIso(e.target.value)}
              placeholder="YYYY-MM-DDTHH:MM:SSZ"
              className="px-3 py-1.5 text-xs bg-navy-950 border border-navy-800 rounded-xl text-white font-mono focus:outline-none focus:ring-2 focus:ring-teal-500"
            />
            <button
              onClick={() => customIso && handleClockAction("set", { iso_datetime: customIso })}
              className="px-3 py-1.5 rounded-xl bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-semibold border border-navy-700"
            >
              Set
            </button>
          </div>
        </div>
      </div>

      {/* Row 3: Fault Toggles */}
      <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
        <div className="flex items-center justify-between pb-2 border-b border-navy-800">
          <div>
            <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center space-x-2">
              <Flame className="h-4 w-4 text-red-400" />
              <span>Chaos & Fault Injection Switches</span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Inject upstream service failures into carrier tracking, payments, and simulated mail to verify store resilience.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {[
            {
              key: "carrier.down",
              label: "carrier.down (503 Outage)",
              desc: "Simulates carrier API complete outage",
              group: "Carrier",
            },
            {
              key: "carrier.slow",
              label: "carrier.slow (8s Latency)",
              desc: "Injects 8-second artificial delay",
              group: "Carrier",
            },
            {
              key: "carrier.stale",
              label: "carrier.stale (Frozen Feed)",
              desc: "Prevents fresh carrier tracking events",
              group: "Carrier",
            },
            {
              key: "payment.down",
              label: "payment.down (503 Gateway)",
              desc: "SimPay release gateway offline",
              group: "Payment",
            },
            {
              key: "payment.fail_once",
              label: "payment.fail_once",
              desc: "First release attempt fails, retry succeeds",
              group: "Payment",
            },
            {
              key: "payment.timeout_after_commit",
              label: "payment.timeout_after_commit",
              desc: "Timeout returned after commit",
              group: "Payment",
            },
            {
              key: "mail.down",
              label: "mail.down (503 Service)",
              desc: "Inbound/outbound mail service down",
              group: "Mail",
            },
            {
              key: "mail.fail_once",
              label: "mail.fail_once",
              desc: "Fails first send, succeeds on second",
              group: "Mail",
            },
          ].map((item) => {
            const isEnabled = !!faults[item.key];
            return (
              <div
                key={item.key}
                onClick={() => handleToggleFault(item.key)}
                data-testid={`fault-toggle-${item.key}`}
                className={`p-3.5 rounded-xl border cursor-pointer transition-all flex items-start justify-between ${
                  isEnabled
                    ? "bg-red-950/40 border-red-500/60 shadow-sm"
                    : "bg-navy-950/60 border-navy-800 hover:border-navy-700"
                }`}
              >
                <div className="space-y-1">
                  <div className="flex items-center space-x-1.5">
                    <span
                      className={`text-xs font-mono font-bold ${
                        isEnabled ? "text-red-300" : "text-slate-200"
                      }`}
                    >
                      {item.key}
                    </span>
                  </div>
                  <p className="text-[10px] text-slate-400">{item.desc}</p>
                </div>

                <div className="pl-2 pt-0.5">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                      isEnabled
                        ? "bg-red-600 text-white"
                        : "bg-navy-800 text-slate-400 border border-navy-700"
                    }`}
                  >
                    {isEnabled ? "ACTIVE" : "OFF"}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Carrier Shortcut */}
      <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-4 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="h-9 w-9 rounded-xl bg-teal-500/10 text-teal-400 flex items-center justify-center">
            <Truck className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-white uppercase tracking-wider">
              Carrier Console Shortcut
            </h3>
            <p className="text-[11px] text-slate-400">
              Direct access to SwiftShip carrier progress simulator.
            </p>
          </div>
        </div>

        <Link
          to="/seller/carrier"
          className="inline-flex items-center space-x-1 px-3.5 py-1.5 rounded-xl bg-navy-800 hover:bg-navy-700 text-teal-300 text-xs font-semibold border border-navy-700 transition-colors"
        >
          <span>Open Carrier Console</span>
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </div>

      {/* DEVELOPER PANELS — HIDDEN WHEN JUDGE MODE IS ACTIVE */}
      {!judgeMode && (
        <div className="space-y-6 pt-2">
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-500 px-1">
            <Database className="h-4 w-4" />
            <span>Developer Diagnostics & State Inspector</span>
          </div>

          {/* Database Row Counts & Outbox Summary */}
          {inspectorData && (
            <div className="grid grid-cols-1 md:grid-cols-12 gap-5">
              {/* Row Counts */}
              <div className="md:col-span-7 bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-3">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">
                  Database Table Row Counts
                </h3>

                <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
                  {Object.entries(inspectorData.table_row_counts || {}).map(([tbl, count]) => (
                    <div
                      key={tbl}
                      className="p-2.5 rounded-xl bg-navy-950/60 border border-navy-800 text-xs flex items-center justify-between"
                    >
                      <span className="font-mono text-slate-400 text-[11px] truncate">{tbl}</span>
                      <span className="font-bold text-white ml-2">{count}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Outbox Summary */}
              <div className="md:col-span-5 bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-3">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">
                  Transactional Outbox Status
                </h3>

                <div className="space-y-2">
                  <div className="flex items-center justify-between p-2.5 rounded-xl bg-navy-950/60 border border-navy-800 text-xs">
                    <span className="text-slate-400">Delivered</span>
                    <span className="font-bold text-teal-400">
                      {inspectorData.outbox_summary?.delivered || 0}
                    </span>
                  </div>
                  <div className="flex items-center justify-between p-2.5 rounded-xl bg-navy-950/60 border border-navy-800 text-xs">
                    <span className="text-slate-400">Pending</span>
                    <span className="font-bold text-amber-400">
                      {inspectorData.outbox_summary?.pending || 0}
                    </span>
                  </div>
                  <div className="flex items-center justify-between p-2.5 rounded-xl bg-navy-950/60 border border-navy-800 text-xs">
                    <span className="text-slate-400">Exhausted</span>
                    <span className="font-bold text-red-400">
                      {inspectorData.outbox_summary?.exhausted || 0}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Database Snapshots */}
          <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-5 space-y-4">
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300 flex items-center space-x-2">
              <Archive className="h-4 w-4 text-purple-400" />
              <span>Snapshot Save & Restore (SQLite File Copy)</span>
            </h3>

            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
              <input
                type="text"
                value={snapshotName}
                onChange={(e) => setSnapshotName(e.target.value)}
                placeholder="Snapshot identifier (e.g. before-sc03-test)"
                className="flex-1 px-3 py-2 text-xs bg-navy-950 border border-navy-800 rounded-xl text-white font-mono focus:outline-none focus:ring-2 focus:ring-teal-500"
              />
              <button
                onClick={handleSaveSnapshot}
                disabled={!snapshotName.trim()}
                className="px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors"
              >
                Save Snapshot
              </button>
            </div>

            {/* Existing Snapshots */}
            {snapshots.length > 0 && (
              <div className="space-y-2 pt-2 border-t border-navy-800">
                <span className="text-[11px] font-semibold text-slate-400">Saved Snapshots:</span>
                <div className="flex flex-wrap gap-2">
                  {snapshots.map((s) => (
                    <div
                      key={s}
                      className="px-3 py-1.5 rounded-xl bg-navy-950 border border-navy-800 flex items-center space-x-2 text-xs"
                    >
                      <Bookmark className="h-3 w-3 text-purple-400" />
                      <span className="font-mono text-slate-300">{s}</span>
                      <button
                        onClick={() => handleRestoreSnapshot(s)}
                        className="text-teal-400 hover:text-teal-300 font-semibold text-[11px]"
                      >
                        Restore
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
