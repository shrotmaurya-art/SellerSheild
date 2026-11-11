import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Bot,
  Clock,
  Gauge,
  Inbox,
  LogOut,
  Menu,
  Package,
  RotateCcw,
  Settings,
  ShieldAlert,
  ShoppingBag,
  Sliders,
  Store,
  Truck,
  Users,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { formatISTShort } from "../utils/date";

export function OwnerOnlyRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user || user.role !== "seller_owner") {
    return (
      <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
        <div className="bg-slate-900 rounded-2xl border border-red-500/30 p-8 shadow-xl space-y-4">
          <div className="h-14 w-14 rounded-2xl bg-red-500/10 text-red-400 flex items-center justify-center mx-auto border border-red-500/20">
            <ShieldAlert className="h-7 w-7" />
          </div>
          <h2 className="text-lg font-bold text-white tracking-tight">Owner Authorization Required</h2>
          <p className="text-xs text-slate-400 leading-relaxed">
            Access to this configuration module is restricted to the store owner (<code className="font-mono text-amber-300">seller_owner</code>). Staff accounts
            (<code className="font-mono text-slate-300">seller_staff</code>) do not have privileges to view or adjust store policies.
          </p>
          <div className="pt-2">
            <Link
              to="/seller"
              className="inline-flex items-center space-x-1.5 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold transition-all btn-tactile shadow-sm"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Back to Dashboard</span>
            </Link>
          </div>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

export function SellerLayout({ children }: { children: ReactNode }) {
  const { user, loading, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [simulatedTime, setSimulatedTime] = useState<string | null>(null);

  // Poll clock endpoint to show current simulated IST time in header
  useEffect(() => {
    let mounted = true;
    const fetchClock = async () => {
      try {
        const { data } = await apiClient.GET("/api/v1/clock");
        if (mounted && data) {
          setSimulatedTime(formatISTShort(data.now_utc));
        }
      } catch {
        // Silent catch for clock ticker
      }
    };
    fetchClock();
    const interval = setInterval(fetchClock, 30000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  // Close mobile drawer on route change
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  if (loading) {
    return (
      <div className="min-h-screen bg-[#070D18] flex items-center justify-center text-teal-400">
        <div className="flex flex-col items-center space-y-3">
          <div className="h-8 w-8 rounded-full border-2 border-teal-500 border-t-transparent animate-spin"></div>
          <span className="text-xs font-mono text-slate-400">Loading Seller Cockpit...</span>
        </div>
      </div>
    );
  }

  // Not logged in -> redirect to login
  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Customer -> redirect away to orders
  if (user.role === "customer") {
    return <Navigate to="/orders" replace />;
  }

  const isOwner = user.role === "seller_owner";
  const isDemoMode = import.meta.env.VITE_DEMO_MODE !== "false";

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const navItemClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center space-x-3 px-3 py-2.5 rounded-xl text-xs font-medium transition-all ${
      isActive
        ? "bg-teal-500/15 text-teal-300 font-semibold border-l-2 border-teal-400 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]"
        : "text-slate-400 hover:text-slate-100 hover:bg-slate-800/60"
    }`;

  return (
    <div className="min-h-screen bg-[#070D18] text-slate-100 flex flex-col md:flex-row antialiased selection:bg-teal-500 selection:text-white">
      {/* Mobile Top Bar */}
      <div className="md:hidden bg-[#0B1426] border-b border-slate-800 px-4 py-3 flex items-center justify-between sticky top-0 z-40">
        <div className="flex items-center space-x-2.5">
          <div className="h-7 w-7 rounded-lg bg-teal-600 flex items-center justify-center text-white">
            <Store className="h-4 w-4" />
          </div>
          <span className="font-bold text-white text-sm">NestKart Seller</span>
        </div>
        <button
          onClick={() => setMobileNavOpen(!mobileNavOpen)}
          className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800"
          aria-label="Toggle Navigation"
        >
          {mobileNavOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
      </div>

      {/* Sidebar Navigation */}
      <aside
        className={`${
          mobileNavOpen ? "block" : "hidden"
        } md:flex w-full md:w-64 bg-[#0B1426] border-b md:border-b-0 md:border-r border-slate-800/80 flex-col justify-between flex-shrink-0 z-30`}
      >
        <div>
          {/* Brand header */}
          <div className="p-5 border-b border-slate-800/80 hidden md:flex items-center justify-between">
            <Link to="/seller" className="flex items-center space-x-2.5 group">
              <div className="h-8 w-8 rounded-xl bg-gradient-to-br from-teal-500 to-teal-700 flex items-center justify-center text-white shadow-sm group-hover:scale-105 transition-transform duration-150">
                <Store className="h-4 w-4" />
              </div>
              <div>
                <span className="font-extrabold text-white text-sm tracking-tight block">NestKart</span>
                <span className="text-[10px] font-mono text-teal-400 uppercase tracking-wider block font-semibold">
                  Seller Cockpit
                </span>
              </div>
            </Link>
          </div>

          {/* User badge */}
          <div className="px-4 py-3 bg-[#080E1B]/60 border-b border-slate-800/60 flex items-center justify-between">
            <div className="truncate mr-2">
              <div className="text-xs font-semibold text-slate-200 truncate">{user.display_name}</div>
              <div className="text-[10px] font-mono text-slate-400 truncate">{user.email}</div>
            </div>
            <span
              className={`text-[9px] font-mono font-bold uppercase px-2 py-0.5 rounded tracking-wider flex-shrink-0 ${
                isOwner
                  ? "bg-purple-950/80 text-purple-300 border border-purple-500/40"
                  : "bg-slate-800 text-slate-300 border border-slate-700"
              }`}
            >
              {isOwner ? "Owner" : "Staff"}
            </span>
          </div>

          {/* Nav Items Grouped */}
          <nav className="p-3 space-y-1">
            <div className="px-2 pt-2 pb-1 text-[10px] font-bold uppercase tracking-wider text-slate-500 font-mono">
              Fulfillment
            </div>

            <NavLink to="/seller" end className={navItemClass}>
              <Gauge className="h-4 w-4 text-teal-400" />
              <span>Dashboard</span>
            </NavLink>

            <NavLink to="/seller/orders" className={navItemClass}>
              <Package className="h-4 w-4 text-slate-400" />
              <span>Orders Management</span>
            </NavLink>

            <NavLink to="/seller/returns" className={navItemClass}>
              <RotateCcw className="h-4 w-4 text-amber-400" />
              <span>Returns & Claims</span>
            </NavLink>

            <NavLink to="/seller/customers" className={navItemClass}>
              <Users className="h-4 w-4 text-slate-400" />
              <span>Customer Trust & Risk</span>
            </NavLink>

            <div className="pt-3 pb-1">
              <div className="border-t border-slate-800/80 px-2 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-500 font-mono">
                Logistics Simulator
              </div>
            </div>

            <NavLink to="/seller/carrier" className={navItemClass}>
              <Truck className="h-4 w-4 text-slate-400" />
              <span>Carrier Simulator</span>
            </NavLink>

            <NavLink to="/seller/mailbox" className={navItemClass}>
              <Inbox className="h-4 w-4 text-slate-400" />
              <span>Store Mailbox</span>
            </NavLink>

            <div className="pt-3 pb-1">
              <div className="border-t border-slate-800/80 px-2 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-500 font-mono">
                Administration
              </div>
            </div>

            {/* Owner Only: Settings */}
            {isOwner && (
              <NavLink to="/seller/settings" className={navItemClass}>
                <Settings className="h-4 w-4 text-slate-400" />
                <span>Store Caps & Policies</span>
              </NavLink>
            )}

            {/* Owner Only & DEMO_MODE: Demo Control Center */}
            {isOwner && isDemoMode && (
              <NavLink to="/seller/demo" className={navItemClass}>
                <Sliders className="h-4 w-4 text-teal-400" />
                <span className="flex items-center justify-between w-full">
                  <span>Demo & Chaos Controls</span>
                  <span className="text-[9px] font-mono bg-teal-950 text-teal-300 px-1.5 py-0.5 rounded border border-teal-500/30">
                    LIVE
                  </span>
                </span>
              </NavLink>
            )}

            <NavLink to="/seller/shield" className={navItemClass}>
              <Bot className="h-4 w-4 text-teal-400" />
              <span>SellerShield AI</span>
            </NavLink>
          </nav>
        </div>

        {/* Bottom actions */}
        <div className="p-3 border-t border-slate-800/80 space-y-1.5">
          <Link
            to="/orders"
            className="flex items-center space-x-2 px-3 py-2 rounded-xl text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-colors"
          >
            <ShoppingBag className="h-4 w-4 text-slate-500" />
            <span>Switch to Customer View</span>
          </Link>

          <button
            onClick={handleLogout}
            className="w-full flex items-center space-x-2 px-3 py-2 rounded-xl text-xs text-red-400 hover:text-red-300 hover:bg-red-950/20 transition-colors btn-tactile"
          >
            <LogOut className="h-4 w-4" />
            <span>Sign Out</span>
          </button>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
        {/* Cockpit Top Status Bar */}
        <div className="bg-[#0B1426]/70 backdrop-blur-sm border-b border-slate-800/80 px-4 sm:px-6 py-2.5 flex items-center justify-between text-xs">
          <div className="flex items-center space-x-2 text-slate-400 font-mono text-[11px]">
            <span className="h-2 w-2 rounded-full bg-emerald-500 status-dot-pulse"></span>
            <span className="text-slate-300 font-semibold">NESTKART SELLER CONSOLE</span>
            <span className="text-slate-600 hidden sm:inline">&bull;</span>
            <span className="text-slate-400 hidden sm:inline">Role: {user.role}</span>
          </div>

          <div className="flex items-center space-x-3 text-slate-400">
            {simulatedTime && (
              <div className="flex items-center space-x-1 tabular-nums font-mono text-[11px] text-teal-300 bg-teal-950/60 border border-teal-800/40 px-2 py-0.5 rounded">
                <Clock className="h-3 w-3 text-teal-400" />
                <span>IST {simulatedTime}</span>
              </div>
            )}
            <span className="text-[10px] font-mono text-slate-400 bg-slate-800 px-2 py-0.5 rounded border border-slate-700">
              WAL DB
            </span>
          </div>
        </div>

        {/* Page Inner Content */}
        <main className="flex-1 p-4 sm:p-6 lg:p-8 max-w-7xl w-full mx-auto animate-fade-in-up">
          {children}
        </main>
      </div>
    </div>
  );
}
