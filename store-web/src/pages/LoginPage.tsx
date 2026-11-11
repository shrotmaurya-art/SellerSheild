import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, Lock, Mail, ShieldAlert, ShoppingBag, Sparkles } from "lucide-react";
import { useAuth } from "../context/AuthContext";

const DEMO_CUSTOMERS = [
  { name: "Aarav Mehta", email: "aarav.mehta@example.test", role: "Customer (C001)", initial: "AM" },
  { name: "Diya Nair", email: "diya.nair@example.test", role: "Customer (C002)", initial: "DN" },
  { name: "Rohan Das", email: "rohan.das@example.test", role: "Customer (C003)", initial: "RD" },
  { name: "Meera Iyer", email: "meera.iyer@example.test", role: "Customer (C004)", initial: "MI" },
  { name: "Kabir Joshi", email: "kabir.joshi@example.test", role: "Customer (C005)", initial: "KJ" },
];

const DEMO_SELLERS = [
  { name: "Priya Sharma", email: "priya.sharma@nestkart.test", role: "Seller Owner (S001)", initial: "PS" },
  { name: "Kiran Rao", email: "kiran.rao@nestkart.test", role: "Seller Staff (S002)", initial: "KR" },
];

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [activePersonaTab, setActivePersonaTab] = useState<"customers" | "sellers">("customers");

  // Read demo mode feature flag from env
  const isDemoMode = import.meta.env.VITE_DEMO_MODE === "true" || import.meta.env.DEV;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const res = await login(email, password);
      if (res.success) {
        if (email.endsWith("@nestkart.test")) {
          navigate("/seller");
        } else {
          navigate("/orders");
        }
      } else {
        setError(res.error || "Login failed");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleQuickLogin = async (demoEmail: string) => {
    setError(null);
    setEmail(demoEmail);
    setPassword("Demo@12345");
    setSubmitting(true);

    try {
      const res = await login(demoEmail, "Demo@12345");
      if (res.success) {
        if (demoEmail.endsWith("@nestkart.test")) {
          navigate("/seller");
        } else {
          navigate("/orders");
        }
      } else {
        setError(res.error || "Login failed");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-xl mx-auto py-8 px-4 sm:px-6">
      <div className="bg-white rounded-3xl border border-slate-200/80 shadow-card p-6 sm:p-10 space-y-8 animate-fade-in">
        {/* Header Branding */}
        <div className="text-center space-y-2">
          <div className="h-12 w-12 rounded-2xl bg-gradient-to-br from-teal-500 to-teal-700 text-white flex items-center justify-center mx-auto mb-3 shadow-sm">
            <ShoppingBag className="h-6 w-6" />
          </div>
          <h1 className="text-2xl sm:text-3xl font-extrabold text-navy-950 tracking-tight">
            Sign in to NestKart
          </h1>
          <p className="text-xs sm:text-sm text-slate-500">
            Simulated e-commerce store & autonomous SellerShield testbed
          </p>
        </div>

        {error && (
          <div className="rounded-2xl bg-red-50 border border-red-200 p-4 flex items-start space-x-3 text-red-800 text-xs sm:text-sm animate-fade-in">
            <AlertCircle className="h-5 w-5 text-red-600 flex-shrink-0 mt-0.5" />
            <div className="flex-1">
              <p className="font-semibold">{error}</p>
            </div>
          </div>
        )}

        {/* Credentials Form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-[11px] font-bold text-slate-700 uppercase tracking-wider mb-1.5 font-mono">
              Email Address
            </label>
            <div className="relative">
              <Mail className="h-4 w-4 text-slate-400 absolute left-3.5 top-3.5 pointer-events-none" />
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="aarav.mehta@example.test"
                className="w-full text-xs sm:text-sm pl-10 pr-4 py-3 rounded-xl border border-slate-200 bg-slate-50/50 focus:bg-white focus:outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-500/20 transition-all font-mono"
                disabled={submitting}
              />
            </div>
          </div>

          <div>
            <label className="block text-[11px] font-bold text-slate-700 uppercase tracking-wider mb-1.5 font-mono">
              Password
            </label>
            <div className="relative">
              <Lock className="h-4 w-4 text-slate-400 absolute left-3.5 top-3.5 pointer-events-none" />
              <input
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full text-xs sm:text-sm pl-10 pr-4 py-3 rounded-xl border border-slate-200 bg-slate-50/50 focus:bg-white focus:outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-500/20 transition-all font-mono"
                disabled={submitting}
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full py-3 px-4 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-slate-300 text-white text-xs sm:text-sm font-semibold shadow-sm transition-all btn-tactile flex items-center justify-center space-x-2"
          >
            {submitting ? (
              <span className="flex items-center space-x-2">
                <span className="h-4 w-4 rounded-full border-2 border-white border-t-transparent animate-spin"></span>
                <span>Authenticating...</span>
              </span>
            ) : (
              <span>Sign In with Password</span>
            )}
          </button>
        </form>

        {/* DEMO MODE Quick-Login Persona Selector */}
        {isDemoMode && (
          <div className="pt-6 border-t border-slate-100 space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-1.5 text-xs font-bold uppercase tracking-wider text-teal-700 font-mono">
                <Sparkles className="h-3.5 w-3.5 text-teal-600" />
                <span>One-Click Persona Switcher</span>
              </div>
              <span className="text-[10px] font-mono text-slate-400 bg-slate-100 px-2 py-0.5 rounded">
                Password: Demo@12345
              </span>
            </div>

            {/* Persona Tabs */}
            <div className="flex rounded-xl bg-slate-100 p-1 text-xs">
              <button
                type="button"
                onClick={() => setActivePersonaTab("customers")}
                className={`flex-1 py-1.5 rounded-lg font-semibold transition-all ${
                  activePersonaTab === "customers"
                    ? "bg-white text-navy-950 shadow-xs"
                    : "text-slate-500 hover:text-slate-800"
                }`}
              >
                Customers (5)
              </button>
              <button
                type="button"
                onClick={() => setActivePersonaTab("sellers")}
                className={`flex-1 py-1.5 rounded-lg font-semibold transition-all ${
                  activePersonaTab === "sellers"
                    ? "bg-white text-navy-950 shadow-xs"
                    : "text-slate-500 hover:text-slate-800"
                }`}
              >
                Seller Staff (2)
              </button>
            </div>

            {/* Persona Cards Grid */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {activePersonaTab === "customers" &&
                DEMO_CUSTOMERS.map((cust) => (
                  <button
                    key={cust.email}
                    onClick={() => handleQuickLogin(cust.email)}
                    disabled={submitting}
                    className="flex items-center space-x-3 text-left p-2.5 rounded-xl bg-slate-50 hover:bg-teal-50/70 border border-slate-200/80 hover:border-teal-300 text-xs transition-all btn-tactile disabled:opacity-50 group"
                  >
                    <div className="h-8 w-8 rounded-lg bg-teal-100 text-teal-800 font-bold text-xs flex items-center justify-center flex-shrink-0 group-hover:bg-teal-600 group-hover:text-white transition-colors">
                      {cust.initial}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="font-semibold text-navy-950 truncate">{cust.name}</div>
                      <div className="text-[10px] text-slate-400 font-mono truncate">{cust.role}</div>
                    </div>
                  </button>
                ))}

              {activePersonaTab === "sellers" &&
                DEMO_SELLERS.map((seller) => (
                  <button
                    key={seller.email}
                    onClick={() => handleQuickLogin(seller.email)}
                    disabled={submitting}
                    className="flex items-center space-x-3 text-left p-2.5 rounded-xl bg-slate-50 hover:bg-navy-50/80 border border-slate-200/80 hover:border-navy-400 text-xs transition-all btn-tactile disabled:opacity-50 group"
                  >
                    <div className="h-8 w-8 rounded-lg bg-navy-800 text-white font-bold text-xs flex items-center justify-center flex-shrink-0 group-hover:bg-teal-600 transition-colors">
                      {seller.initial}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="font-semibold text-navy-950 truncate">{seller.name}</div>
                      <div className="text-[10px] text-slate-400 font-mono truncate">{seller.role}</div>
                    </div>
                  </button>
                ))}
            </div>

            <div className="rounded-xl bg-slate-50 border border-slate-200/60 p-3 text-[11px] text-slate-500 flex items-center space-x-2">
              <ShieldAlert className="h-4 w-4 text-teal-600 flex-shrink-0" />
              <span>
                Demo sandbox: sessions use rotating httpOnly refresh cookies with memory-only RS256 access tokens.
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
