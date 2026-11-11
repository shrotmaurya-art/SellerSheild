import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Clock,
  Inbox,
  LogOut,
  Menu,
  Package,
  RefreshCw,
  ShoppingBag,
  Store,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { formatISTShort } from "../utils/date";
import { ChatWidget } from "./ChatWidget";

// ---------------------------------------------------------------------------
// Error Boundary
// ---------------------------------------------------------------------------

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class RouteErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  public override componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Uncaught route error:", error, errorInfo);
  }

  public override render() {
    if (this.state.hasError) {
      return (
        <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
          <div className="bg-white rounded-2xl border border-slate-200/80 p-8 shadow-card space-y-5">
            <div className="h-14 w-14 rounded-2xl bg-red-50 text-red-600 flex items-center justify-center mx-auto border border-red-100">
              <AlertTriangle className="h-7 w-7" />
            </div>
            <div>
              <h2 className="text-lg font-bold text-navy-900 tracking-tight">Something went wrong</h2>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                {this.state.error?.message || "An unexpected interface error occurred."}
              </p>
            </div>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              className="inline-flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Retry Action</span>
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

// ---------------------------------------------------------------------------
// Protected Route / Role Guard
// ---------------------------------------------------------------------------

export function ProtectedRoute({
  children,
  allowedRoles,
}: {
  children: ReactNode;
  allowedRoles?: string[];
}) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <div className="flex flex-col items-center space-y-3">
          <div className="h-8 w-8 rounded-full border-2 border-teal-600 border-t-transparent animate-spin"></div>
          <span className="text-xs font-medium text-slate-400">Restoring session...</span>
        </div>
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (allowedRoles && !allowedRoles.includes(user.role)) {
    return (
      <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
        <div className="bg-white rounded-2xl border border-amber-200/80 p-8 shadow-card space-y-4">
          <div className="h-12 w-12 rounded-xl bg-amber-50 text-amber-600 flex items-center justify-center mx-auto">
            <AlertTriangle className="h-6 w-6" />
          </div>
          <h2 className="text-lg font-bold text-navy-900">Access Restricted</h2>
          <p className="text-xs text-slate-500 leading-relaxed">
            Your role (<code className="font-mono text-slate-700 font-semibold">{user.role}</code>) does not have permission to view this section.
          </p>
          <div className="pt-2">
            <Link
              to={user.role.startsWith("seller") ? "/seller" : "/orders"}
              className="inline-flex items-center space-x-1 text-xs text-teal-600 hover:text-teal-700 font-semibold underline underline-offset-4"
            >
              <span>Return to authorized portal</span>
              <span>&rarr;</span>
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

// ---------------------------------------------------------------------------
// 404 Page
// ---------------------------------------------------------------------------

export function NotFoundPage() {
  return (
    <div className="max-w-md mx-auto py-20 px-4 text-center animate-fade-in">
      <div className="bg-white rounded-2xl border border-slate-200/80 p-8 shadow-card space-y-4">
        <div className="h-16 w-16 rounded-2xl bg-slate-100 text-slate-400 flex items-center justify-center mx-auto">
          <Package className="h-8 w-8" />
        </div>
        <div className="space-y-1">
          <h1 className="text-3xl font-extrabold text-navy-900 tracking-tight font-mono">404</h1>
          <p className="text-sm font-semibold text-slate-700">Page not found</p>
          <p className="text-xs text-slate-500">The requested link does not exist or was moved.</p>
        </div>
        <div className="pt-2">
          <Link
            to="/orders"
            className="inline-flex items-center space-x-1.5 px-4 py-2.5 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
          >
            <span>&larr; Back to My Orders</span>
          </Link>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Layout
// ---------------------------------------------------------------------------

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
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
    setMobileMenuOpen(false);
  }, [location.pathname]);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  // If on a /seller route, render seller layout directly
  if (location.pathname.startsWith("/seller")) {
    return <RouteErrorBoundary>{children}</RouteErrorBoundary>;
  }

  const isActive = (path: string) => {
    if (path === "/orders" && (location.pathname === "/orders" || location.pathname.startsWith("/orders/"))) {
      return true;
    }
    return location.pathname === path;
  };

  const navLinkClass = (path: string) =>
    `relative px-3 py-2 text-xs font-medium rounded-lg transition-all ${
      isActive(path)
        ? "text-teal-700 font-semibold bg-teal-50/80"
        : "text-slate-600 hover:text-navy-900 hover:bg-slate-100/70"
    }`;

  // User initials avatar
  const initials = user?.display_name
    ? user.display_name
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : "U";

  return (
    <div className="min-h-screen flex flex-col bg-[#F8FAFC] text-slate-800 antialiased selection:bg-teal-500 selection:text-white">
      {/* Simulation Banner Strip */}
      <div className="bg-slate-900 text-slate-300 text-[11px] font-medium border-b border-slate-800">
        <div className="max-w-6xl mx-auto px-4 h-7 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <span className="flex h-2 w-2 relative">
              <span className="status-dot-pulse absolute inline-flex h-full w-full rounded-full bg-teal-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-teal-500"></span>
            </span>
            <span className="tracking-wide uppercase text-[10px] font-bold text-teal-400">Simulated Sandbox</span>
            <span className="text-slate-600 hidden sm:inline">&bull;</span>
            <span className="text-slate-400 hidden sm:inline">Mock catalog & payments &bull; No real money</span>
          </div>

          <div className="flex items-center space-x-3 text-slate-400">
            {simulatedTime && (
              <div className="flex items-center space-x-1 tabular-nums font-mono text-[11px] text-slate-300 bg-slate-800/80 px-2 py-0.5 rounded">
                <Clock className="h-3 w-3 text-teal-400" />
                <span>IST {simulatedTime}</span>
              </div>
            )}
            <span className="text-slate-500 text-[10px] font-mono uppercase bg-slate-800 px-1.5 py-0.5 rounded">Stage 13</span>
          </div>
        </div>
      </div>

      {/* Main Glass Header */}
      <header className="sticky top-0 z-30 backdrop-blur-md bg-white/90 border-b border-slate-200/80 transition-shadow">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          {/* Brand Mark */}
          <Link to="/orders" className="flex items-center space-x-2.5 group">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-teal-600 to-teal-700 text-white flex items-center justify-center shadow-sm group-hover:scale-105 transition-transform duration-150">
              <ShoppingBag className="h-5 w-5" />
            </div>
            <div className="flex flex-col">
              <span className="font-extrabold text-navy-950 text-base tracking-tight leading-none">NestKart</span>
              <span className="text-[10px] font-semibold uppercase tracking-wider text-teal-600 leading-tight">Storefront</span>
            </div>
          </Link>

          {/* Desktop Nav */}
          <nav className="hidden md:flex items-center space-x-1">
            {user ? (
              <>
                {user.role === "customer" && (
                  <>
                    <Link to="/orders" className={navLinkClass("/orders")}>
                      My Orders
                    </Link>
                    <Link to="/returns" className={navLinkClass("/returns")}>
                      Returns & Refunds
                    </Link>
                    <Link to="/inbox" className={navLinkClass("/inbox")}>
                      <span className="inline-flex items-center space-x-1.5">
                        <Inbox className="h-3.5 w-3.5" />
                        <span>Inbox</span>
                      </span>
                    </Link>
                  </>
                )}

                {user.role.startsWith("seller") && (
                  <Link
                    to="/seller"
                    className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-navy-900 text-white hover:bg-navy-800 shadow-sm transition-all mr-2"
                  >
                    <Store className="h-3.5 w-3.5 text-teal-400" />
                    <span>Seller Console</span>
                  </Link>
                )}

                <div className="h-4 w-px bg-slate-200 mx-2" />

                {/* User Capsule */}
                <Link
                  to="/profile"
                  className="flex items-center space-x-2 px-2.5 py-1.5 rounded-lg hover:bg-slate-100/80 transition-colors"
                >
                  <div className="h-7 w-7 rounded-full bg-teal-100 text-teal-800 text-[11px] font-bold flex items-center justify-center border border-teal-200">
                    {initials}
                  </div>
                  <div className="flex flex-col text-left leading-none">
                    <span className="text-xs font-semibold text-slate-900">{user.display_name}</span>
                    <span className="text-[10px] text-slate-400 capitalize">{user.role}</span>
                  </div>
                </Link>

                {/* Logout Button */}
                <button
                  onClick={handleLogout}
                  className="p-2 rounded-lg text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors ml-1 btn-tactile"
                  title="Sign out"
                  aria-label="Sign out"
                >
                  <LogOut className="h-4 w-4" />
                </button>
              </>
            ) : (
              <Link
                to="/login"
                className="px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
              >
                Sign In
              </Link>
            )}
          </nav>

          {/* Mobile Menu Button */}
          <div className="md:hidden flex items-center space-x-2">
            {user && (
              <div className="h-7 w-7 rounded-full bg-teal-100 text-teal-800 text-xs font-bold flex items-center justify-center">
                {initials}
              </div>
            )}
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="p-2 rounded-lg text-slate-600 hover:bg-slate-100"
              aria-label="Toggle navigation menu"
            >
              {mobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
            </button>
          </div>
        </div>

        {/* Mobile Dropdown Menu */}
        {mobileMenuOpen && (
          <div className="md:hidden bg-white border-b border-slate-200 px-4 py-3 space-y-2 animate-fade-in shadow-lg">
            {user ? (
              <>
                <div className="pb-2 border-b border-slate-100 flex items-center justify-between">
                  <div>
                    <p className="text-xs font-bold text-slate-900">{user.display_name}</p>
                    <p className="text-[11px] text-slate-500 font-mono">{user.email}</p>
                  </div>
                  <span className="text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full bg-teal-50 text-teal-700 border border-teal-200">
                    {user.role}
                  </span>
                </div>

                {user.role === "customer" && (
                  <div className="space-y-1">
                    <Link to="/orders" className="block px-3 py-2 text-xs font-medium rounded-lg text-slate-700 hover:bg-slate-50">
                      My Orders
                    </Link>
                    <Link to="/returns" className="block px-3 py-2 text-xs font-medium rounded-lg text-slate-700 hover:bg-slate-50">
                      Returns & Refunds
                    </Link>
                    <Link to="/inbox" className="block px-3 py-2 text-xs font-medium rounded-lg text-slate-700 hover:bg-slate-50">
                      Inbox
                    </Link>
                    <Link to="/profile" className="block px-3 py-2 text-xs font-medium rounded-lg text-slate-700 hover:bg-slate-50">
                      Profile
                    </Link>
                  </div>
                )}

                {user.role.startsWith("seller") && (
                  <Link
                    to="/seller"
                    className="block px-3 py-2 text-xs font-semibold rounded-lg bg-navy-900 text-white"
                  >
                    Go to Seller Console
                  </Link>
                )}

                <div className="pt-2 border-t border-slate-100">
                  <button
                    onClick={handleLogout}
                    className="w-full flex items-center space-x-2 px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50 rounded-lg"
                  >
                    <LogOut className="h-4 w-4" />
                    <span>Sign Out</span>
                  </button>
                </div>
              </>
            ) : (
              <Link
                to="/login"
                className="block w-full text-center px-4 py-2.5 rounded-xl bg-teal-600 text-white text-xs font-semibold shadow-sm"
              >
                Sign In
              </Link>
            )}
          </div>
        )}
      </header>

      {/* Main Content Area with max-width and soft breathing room */}
      <main className="flex-1 w-full max-w-6xl mx-auto px-4 sm:px-6 py-6 md:py-8 animate-fade-in-up">
        <RouteErrorBoundary>{children}</RouteErrorBoundary>
      </main>

      {/* Floating Chat Widget */}
      <ChatWidget />

      {/* Human-crafted Footer */}
      <footer className="bg-white border-t border-slate-200/80 mt-auto py-8">
        <div className="max-w-6xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between text-xs text-slate-400 space-y-4 sm:space-y-0">
          <div className="flex items-center space-x-2 text-slate-600 font-medium">
            <span className="font-bold text-navy-950">NestKart Playground</span>
            <span>&bull;</span>
            <span className="text-slate-500">Autonomous SellerShield Verification Environment</span>
          </div>

          <div className="flex items-center space-x-4 text-[11px] text-slate-400">
            <span>Money in integer paise</span>
            <span>&bull;</span>
            <span>SimPay Gateway</span>
            <span>&bull;</span>
            <span className="font-mono text-slate-500">Stage 13 Release</span>
          </div>
        </div>
      </footer>
    </div>
  );
}
