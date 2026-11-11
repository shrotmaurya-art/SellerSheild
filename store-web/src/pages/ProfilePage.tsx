import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Globe, LogOut, ShieldCheck } from "lucide-react";
import { useAuth } from "../context/AuthContext";

export function ProfilePage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [language, setLanguage] = useState("English");

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  if (!user) return null;

  return (
    <div className="max-w-2xl mx-auto py-6 sm:py-10 space-y-6">
      <div className="bg-white rounded-3xl border border-slate-200/80 p-6 sm:p-8 space-y-6 shadow-card card-hover-lift animate-fade-in">
        <div className="flex items-center space-x-4 border-b border-slate-100 pb-6">
          <div className="h-16 w-16 rounded-2xl bg-teal-50 text-teal-700 flex items-center justify-center border border-teal-200/80 text-xl font-bold shadow-xs">
            {user.display_name?.charAt(0) || "U"}
          </div>
          <div>
            <h1 className="text-xl sm:text-2xl font-extrabold text-navy-950 tracking-tight">{user.display_name}</h1>
            <p className="text-xs text-slate-500 font-mono mt-0.5">{user.email}</p>
            <div className="flex items-center space-x-2 mt-2">
              <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-teal-50 text-teal-700 border border-teal-200 uppercase font-mono">
                {user.role}
              </span>
              {user.customer_id && (
                <span className="font-mono text-xs text-slate-500 bg-slate-100 px-2 py-0.5 rounded">
                  Customer {user.customer_id}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Preferences */}
        <div className="space-y-3">
          <h2 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider font-mono">
            Account Preferences
          </h2>

          <div className="flex items-center justify-between p-4 bg-slate-50/70 rounded-2xl border border-slate-200/80">
            <div className="flex items-center space-x-3">
              <div className="h-9 w-9 rounded-xl bg-white border border-slate-200 flex items-center justify-center text-slate-500">
                <Globe className="h-5 w-5" />
              </div>
              <div>
                <div className="text-xs sm:text-sm font-bold text-navy-950">Language Preference</div>
                <div className="text-[11px] text-slate-500">Select language for simulated support conversations</div>
              </div>
            </div>

            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="text-xs font-semibold bg-white border border-slate-200 rounded-xl px-3 py-2 focus:outline-none focus:border-teal-500 transition-colors shadow-xs"
            >
              <option value="English">English</option>
              <option value="Hindi">हिंदी (Hindi)</option>
              <option value="Tamil">தமிழ் (Tamil)</option>
            </select>
          </div>
        </div>

        {/* Session Security Details */}
        <div className="space-y-3">
          <h2 className="text-[11px] font-bold text-slate-400 uppercase tracking-wider font-mono">
            Security & Zero-Trust Architecture
          </h2>

          <div className="p-4 bg-emerald-50/50 rounded-2xl border border-emerald-200/80 space-y-2 text-xs text-slate-600">
            <div className="flex items-center space-x-2 text-emerald-800 font-bold">
              <ShieldCheck className="h-4 w-4 text-emerald-600" />
              <span>In-Memory Token Protection (Strict Golden Rule #8)</span>
            </div>
            <p className="text-slate-600 text-[11px] leading-relaxed">
              Your RS256 Bearer access token is strictly preserved in non-persisted client memory. Double-submit CSRF cookie protection and rotating httpOnly refresh cookies defend against credential exfiltration and cross-site tampering.
            </p>
          </div>
        </div>

        {/* Sign Out Action */}
        <div className="border-t border-slate-100 pt-6">
          <button
            onClick={handleLogout}
            className="w-full sm:w-auto inline-flex items-center justify-center space-x-2 px-5 py-2.5 rounded-xl bg-red-50 hover:bg-red-100 text-red-700 text-xs font-semibold border border-red-200/80 transition-all btn-tactile"
          >
            <LogOut className="h-4 w-4" />
            <span>Sign Out Session</span>
          </button>
        </div>
      </div>
    </div>
  );
}
