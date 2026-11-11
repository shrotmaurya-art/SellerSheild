import { useEffect, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Cpu,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import { fetchDecisionTrace, type DecisionTrace } from "../../api/sellerShield";
import { formatIST } from "../../utils/date";

interface DecisionTraceDrawerProps {
  requestId: string | null;
  onClose: () => void;
}

export function DecisionTraceDrawer({ requestId, onClose }: DecisionTraceDrawerProps) {
  const [trace, setTrace] = useState<DecisionTrace | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!requestId) {
      setTrace(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchDecisionTrace(requestId)
      .then((data) => {
        if (!cancelled) setTrace(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load decision trace");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [requestId]);

  useEffect(() => {
    if (!requestId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [requestId, onClose]);

  if (!requestId) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-slate-950/75 backdrop-blur-sm flex justify-end animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="decision-trace-title"
    >
      <div className="w-full max-w-2xl bg-[#0B1426] border-l border-navy-800 h-full flex flex-col shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b border-navy-800 flex items-center justify-between bg-navy-950/70">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-wider text-teal-400 font-semibold">
              Deterministic Audit Telemetry
            </div>
            <h2 id="decision-trace-title" className="text-base font-bold text-white flex items-center space-x-2 mt-0.5">
              <span>Decision Trace</span>
              <code className="text-xs font-mono text-teal-300 bg-teal-950/60 border border-teal-500/30 px-2 py-0.5 rounded">
                {requestId}
              </code>
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-xl text-slate-400 hover:text-white hover:bg-navy-800 transition-colors"
            aria-label="Close Decision Trace"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6 custom-scrollbar text-xs">
          {loading && (
            <div className="py-16 flex flex-col items-center justify-center space-y-3 text-slate-400">
              <div className="h-8 w-8 rounded-full border-2 border-teal-500 border-t-transparent animate-spin" />
              <span className="font-mono">Assembling verifiable trace...</span>
            </div>
          )}

          {error && (
            <div className="bg-red-950/40 border border-red-500/40 rounded-xl p-4 text-red-200 flex items-start space-x-2.5">
              <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0 mt-0.5" />
              <div>
                <div className="font-semibold">Unable to load trace</div>
                <p className="text-red-300/90 mt-0.5">{error}</p>
              </div>
            </div>
          )}

          {trace && !loading && (
            <>
              {/* Summary Strip */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="bg-navy-900 border border-navy-800 rounded-xl p-3">
                  <div className="text-[10px] font-mono text-slate-400 uppercase">Timeline Steps</div>
                  <div className="text-lg font-bold text-white mt-0.5">{trace.audit_events.length}</div>
                </div>
                <div className="bg-navy-900 border border-navy-800 rounded-xl p-3">
                  <div className="text-[10px] font-mono text-slate-400 uppercase">LLM Calls</div>
                  <div className="text-lg font-bold text-teal-300 mt-0.5">{trace.llm_calls.length}</div>
                </div>
                <div className="bg-navy-900 border border-navy-800 rounded-xl p-3">
                  <div className="text-[10px] font-mono text-slate-400 uppercase">Tool Calls</div>
                  <div className="text-lg font-bold text-white mt-0.5">{trace.tool_calls.length}</div>
                </div>
                <div className="bg-navy-900 border border-navy-800 rounded-xl p-3">
                  <div className="text-[10px] font-mono text-slate-400 uppercase">Policy Evals</div>
                  <div className="text-lg font-bold text-amber-300 mt-0.5">{trace.policy_decisions.length}</div>
                </div>
              </div>

              {/* Stage Timeline */}
              <section className="space-y-3">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono flex items-center space-x-2">
                  <Activity className="h-3.5 w-3.5 text-teal-400" />
                  <span>Canonical Stage Timeline</span>
                </h3>
                {trace.audit_events.length === 0 ? (
                  <p className="text-slate-500">No timeline events recorded.</p>
                ) : (
                  <div className="space-y-2.5">
                    {trace.audit_events.map((ev) => (
                      <div
                        key={ev.event_id}
                        className="bg-navy-900/90 border border-navy-800 rounded-xl p-3.5 space-y-1.5"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center space-x-2">
                            <span className="px-2 py-0.5 rounded bg-teal-950 text-teal-300 border border-teal-500/30 font-mono text-[10px] font-bold uppercase">
                              {ev.stage}
                            </span>
                            <span className="font-semibold text-white">{ev.action}</span>
                          </div>
                          <span
                            className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase ${
                              ev.outcome === "success"
                                ? "bg-emerald-950 text-emerald-300 border border-emerald-500/30"
                                : "bg-red-950 text-red-300 border border-red-500/30"
                            }`}
                          >
                            {ev.outcome}
                          </span>
                        </div>
                        <div className="text-[11px] font-mono text-slate-400 flex flex-wrap gap-x-4 gap-y-1">
                          <span>Actor: {ev.actor}</span>
                          {ev.resource_id && <span>Resource: {ev.resource_id}</span>}
                          <span>{formatIST(ev.created_at)}</span>
                        </div>
                        {Object.keys(ev.details || {}).length > 0 && (
                          <pre className="mt-1.5 p-2.5 rounded-lg bg-navy-950/90 border border-navy-800/80 text-[11px] font-mono text-slate-300 overflow-x-auto">
                            {JSON.stringify(ev.details, null, 2)}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {/* LLM Calls */}
              {trace.llm_calls.length > 0 && (
                <section className="space-y-2.5">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono flex items-center space-x-2">
                    <Cpu className="h-3.5 w-3.5 text-teal-400" />
                    <span>LLM Calls</span>
                  </h3>
                  <div className="space-y-2">
                    {trace.llm_calls.map((lc, i) => (
                      <div
                        key={String(lc.call_id || i)}
                        className="bg-navy-900 border border-navy-800 rounded-xl p-3 font-mono text-[11px] text-slate-300 flex flex-wrap justify-between gap-2"
                      >
                        <div>
                          <span className="text-teal-300 font-semibold">{String(lc.stage || "llm")}</span>
                          {" • "}
                          <span>{String(lc.provider || "")}/{String(lc.model || "")}</span>
                        </div>
                        <div className="text-slate-400">
                          {String(lc.tokens_in ?? 0)} in / {String(lc.tokens_out ?? 0)} out • {String(lc.latency_ms ?? 0)}ms
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {/* Tool Calls */}
              {trace.tool_calls.length > 0 && (
                <section className="space-y-2.5">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono flex items-center space-x-2">
                    <Wrench className="h-3.5 w-3.5 text-teal-400" />
                    <span>Tool Invocations (Redacted Args)</span>
                  </h3>
                  <div className="space-y-2">
                    {trace.tool_calls.map((tc, i) => (
                      <div
                        key={String(tc.call_id || i)}
                        className="bg-navy-900 border border-navy-800 rounded-xl p-3 space-y-1.5"
                      >
                        <div className="flex items-center justify-between font-mono text-[11px]">
                          <span className="font-bold text-white">{String(tc.tool || "")}</span>
                          <span className="text-slate-400">
                            {String(tc.status || "")} • {String(tc.latency_ms ?? 0)}ms
                          </span>
                        </div>
                        <pre className="p-2 rounded bg-navy-950 text-[11px] font-mono text-slate-300 overflow-x-auto">
                          {JSON.stringify(tc.args_redacted || {}, null, 2)}
                        </pre>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {/* Policy Decisions */}
              {trace.policy_decisions.length > 0 && (
                <section className="space-y-2.5">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono flex items-center space-x-2">
                    <ShieldCheck className="h-3.5 w-3.5 text-teal-400" />
                    <span>Deterministic Policy Decisions</span>
                  </h3>
                  <div className="space-y-2">
                    {trace.policy_decisions.map((pd, i) => (
                      <div
                        key={String(pd.decision_id || i)}
                        className="bg-navy-900 border border-navy-800 rounded-xl p-3 space-y-1.5"
                      >
                        <div className="flex items-center justify-between font-mono text-[11px]">
                          <span className="text-teal-300 font-semibold flex items-center space-x-1.5">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            <span>{String(pd.decision_id || "")} • Policy {String(pd.policy_version || "")}</span>
                          </span>
                          <span className="text-slate-400">Order {String(pd.order_id || "—")}</span>
                        </div>
                        <pre className="p-2 rounded bg-navy-950 text-[11px] font-mono text-slate-300 overflow-x-auto max-h-56">
                          {JSON.stringify(pd.decision || {}, null, 2)}
                        </pre>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
