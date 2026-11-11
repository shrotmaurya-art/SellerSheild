import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  RefreshCw,
  Save,
  Sliders,
} from "lucide-react";
import { apiClient, authFetch } from "../../api/client";
import type { components } from "../../api/schema";
import { formatMoney } from "../../utils/money";
import { formatISTWithRelative } from "../../utils/date";

type StoreSettings = components["schemas"]["StoreSettingsResponse"];

export function SellerSettingsPage() {
  const [settings, setSettings] = useState<StoreSettings | null>(null);
  const [autonomyCapRupees, setAutonomyCapRupees] = useState<string>("500");
  const [dailyCapRupees, setDailyCapRupees] = useState<string>("5000");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const fetchSettings = async () => {
    setLoading(true);
    setError(null);
    setSuccessMessage(null);
    try {
      const { data, error: apiErr } = await apiClient.GET("/api/v1/admin/settings");
      if (apiErr || !data) {
        throw new Error("Failed to load store settings");
      }
      setSettings(data);
      setAutonomyCapRupees((data.autonomy_cap_paise / 100).toString());
      setDailyCapRupees((data.autonomy_daily_cap_paise / 100).toString());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSettings();
  }, []);

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccessMessage(null);

    const capPaise = Math.round(parseFloat(autonomyCapRupees) * 100);
    const dailyPaise = Math.round(parseFloat(dailyCapRupees) * 100);

    if (isNaN(capPaise) || isNaN(dailyPaise) || capPaise <= 0 || dailyPaise <= 0) {
      setError("Please enter valid positive rupee amounts.");
      setSaving(false);
      return;
    }

    try {
      const idemKey = `settings-update-${Date.now()}`;
      const res = await authFetch("/api/v1/admin/settings", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
        body: JSON.stringify({
          autonomy_cap_paise: capPaise,
          autonomy_daily_cap_paise: dailyPaise,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error?.message || `Failed to update settings (${res.status})`);
      }

      const updated = await res.json();
      setSettings(updated);
      setSuccessMessage("Autonomy guardrails successfully updated and enforced.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-3xl space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h1 className="text-2xl font-bold text-white tracking-tight">Store Guardrails & Caps</h1>
            <span className="text-[10px] font-mono font-semibold uppercase bg-purple-900/60 text-purple-300 px-2 py-0.5 rounded border border-purple-500/30">
              OWNER ONLY
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1">
            Configure financial safety backstops for autonomous agent actions and staff authorizations.
          </p>
        </div>

        <button
          onClick={fetchSettings}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Messages */}
      {successMessage && (
        <div className="p-3.5 rounded-xl bg-teal-950/40 border border-teal-500/40 text-teal-300 text-xs font-medium flex items-center space-x-2">
          <CheckCircle2 className="h-4 w-4 text-teal-400 flex-shrink-0" />
          <span>{successMessage}</span>
        </div>
      )}

      {error && (
        <div className="p-3.5 rounded-xl bg-red-950/40 border border-red-500/40 text-red-300 text-xs font-medium flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Form Card */}
      {loading ? (
        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-6 space-y-4 animate-pulse">
          <div className="h-5 bg-navy-800 rounded w-1/3"></div>
          <div className="h-10 bg-navy-800 rounded w-full"></div>
          <div className="h-10 bg-navy-800 rounded w-full"></div>
        </div>
      ) : (
        <form
          onSubmit={handleSaveSettings}
          className="bg-navy-900 border border-navy-800/80 rounded-2xl p-6 space-y-6 shadow-sm"
        >
          <div className="space-y-4">
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-300 flex items-center space-x-2">
              <Sliders className="h-4 w-4 text-teal-400" />
              <span>Money Safety Guardrails</span>
            </h2>

            {/* Per-Refund Autonomy Cap */}
            <div className="p-4 rounded-xl bg-navy-950/60 border border-navy-800 space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-xs font-bold text-white">
                  Per-Action Autonomy Cap (₹)
                </label>
                <span className="text-[11px] font-mono text-teal-400">
                  {settings ? formatMoney(settings.autonomy_cap_paise) : "₹0"}
                </span>
              </div>
              <p className="text-[11px] text-slate-400">
                Maximum refund release or return approval permitted without stored seller authorization.
                Releases above this require explicit seller sign-off.
              </p>
              <div className="relative mt-2">
                <span className="absolute left-3 top-2.5 text-xs text-slate-400">₹</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={autonomyCapRupees}
                  onChange={(e) => setAutonomyCapRupees(e.target.value)}
                  data-testid="input-autonomy-cap"
                  className="w-full pl-7 pr-4 py-2 bg-navy-900 border border-navy-800 rounded-xl text-xs text-white focus:outline-none focus:ring-2 focus:ring-teal-500 font-mono"
                  required
                />
              </div>
            </div>

            {/* Daily Autonomy Cap */}
            <div className="p-4 rounded-xl bg-navy-950/60 border border-navy-800 space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-xs font-bold text-white">
                  Daily Autonomous Total Cap (₹)
                </label>
                <span className="text-[11px] font-mono text-teal-400">
                  {settings ? formatMoney(settings.autonomy_daily_cap_paise) : "₹0"}
                </span>
              </div>
              <p className="text-[11px] text-slate-400">
                Cumulative monetary limit on all autonomous agent-issued releases per simulated IST day.
                Exceeding this yields an immediate 409 limit_exceeded.
              </p>
              <div className="relative mt-2">
                <span className="absolute left-3 top-2.5 text-xs text-slate-400">₹</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={dailyCapRupees}
                  onChange={(e) => setDailyCapRupees(e.target.value)}
                  data-testid="input-daily-cap"
                  className="w-full pl-7 pr-4 py-2 bg-navy-900 border border-navy-800 rounded-xl text-xs text-white focus:outline-none focus:ring-2 focus:ring-teal-500 font-mono"
                  required
                />
              </div>
            </div>
          </div>

          {/* Audit footer */}
          {settings && (
            <div className="pt-2 text-[11px] text-slate-500 border-t border-navy-800 flex items-center justify-between">
              <span>Updated by: {settings.updated_by}</span>
              <span>{formatISTWithRelative(settings.updated_at, true)}</span>
            </div>
          )}

          {/* Submit */}
          <div className="pt-2 flex justify-end">
            <button
              type="submit"
              disabled={saving}
              data-testid="save-settings-button"
              className="inline-flex items-center space-x-1.5 px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors"
            >
              <Save className="h-4 w-4" />
              <span>{saving ? "Saving Changes..." : "Save Guardrails"}</span>
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
