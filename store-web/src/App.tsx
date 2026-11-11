import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { Layout, NotFoundPage, ProtectedRoute } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { OrdersPage } from "./pages/OrdersPage";
import { OrderDetailPage } from "./pages/OrderDetailPage";
import { ReturnsPage } from "./pages/ReturnsPage";
import { InboxPage } from "./pages/InboxPage";
import { ProfilePage } from "./pages/ProfilePage";
import { DevCardsPage } from "./pages/DevCardsPage";
import { OwnerOnlyRoute, SellerLayout } from "./components/SellerLayout";
import { SellerDashboardPage } from "./pages/seller/SellerDashboardPage";
import { SellerOrdersPage } from "./pages/seller/SellerOrdersPage";
import { SellerReturnsPage } from "./pages/seller/SellerReturnsPage";
import { SellerCustomersPage } from "./pages/seller/SellerCustomersPage";
import { SellerCarrierPage } from "./pages/seller/SellerCarrierPage";
import { SellerMailboxPage } from "./pages/seller/SellerMailboxPage";
import { SellerSettingsPage } from "./pages/seller/SellerSettingsPage";
import { SellerDemoPage } from "./pages/seller/SellerDemoPage";
import { SellerShieldPlaceholderPage } from "./pages/seller/SellerShieldPlaceholderPage";
import { Clock as ClockIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { apiClient } from "./api/client";
import type { paths } from "./api/schema";

export { LoginPage } from "./pages/LoginPage";

type ClockResponse = paths["/api/v1/clock"]["get"]["responses"][200]["content"]["application/json"];

export function ClockPage() {
  const [clockData, setClockData] = useState<ClockResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchClock() {
      try {
        const { data, error: apiError } = await apiClient.GET("/api/v1/clock");
        if (apiError) {
          setError(typeof apiError === "string" ? apiError : "Failed to fetch clock data");
        } else if (data) {
          setClockData(data);
        }
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : "Network error");
      } finally {
        setLoading(false);
      }
    }

    fetchClock();
  }, []);

  return (
    <div className="max-w-3xl mx-auto py-12 px-4 sm:px-6">
      <div className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
        <div className="bg-navy-900 text-white p-6 sm:p-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">NestKart Playground</h1>
            <p className="text-teal-100 text-sm mt-1">Stage 13 — Seller Console & Demo Controls</p>
          </div>
          <div className="h-12 w-12 rounded-lg bg-teal-600/30 flex items-center justify-center border border-teal-500/40">
            <ClockIcon className="h-6 w-6 text-teal-100" />
          </div>
        </div>

        <div className="p-6 sm:p-8 space-y-6">
          <div className="border border-slate-100 rounded-lg p-5 bg-slate-50">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-500 mb-4">
              Live Store Clock (via /api/v1/clock)
            </h2>

            {loading && (
              <div className="flex items-center space-x-3 text-slate-600 animate-pulse py-4">
                <div className="h-4 w-4 rounded-full bg-teal-500"></div>
                <span className="text-sm">Connecting to Store API...</span>
              </div>
            )}

            {error && (
              <div className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-700">
                <p className="font-medium">Error loading clock</p>
                <p className="mt-1 text-xs text-red-600">{error}</p>
              </div>
            )}

            {clockData && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <span className="text-xs font-medium text-slate-400">Current UTC Time</span>
                  <p className="text-lg font-mono font-semibold text-navy-900 mt-1" data-testid="now-utc">
                    {clockData.now_utc}
                  </p>
                </div>
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <span className="text-xs font-medium text-slate-400">Clock Mode</span>
                  <div className="mt-1 flex items-center space-x-2">
                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-teal-100 text-teal-800" data-testid="clock-mode">
                      {clockData.mode}
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function HomeRedirect() {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return <Navigate to="/login" replace />;
  if (user.role.startsWith("seller")) return <Navigate to="/seller" replace />;
  return <Navigate to="/orders" replace />;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Layout>
          <Routes>
            {/* Customer & Common Routes */}
            <Route path="/" element={<HomeRedirect />} />
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/orders"
              element={
                <ProtectedRoute allowedRoles={["customer"]}>
                  <OrdersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/orders/:orderId"
              element={
                <ProtectedRoute allowedRoles={["customer"]}>
                  <OrderDetailPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/returns"
              element={
                <ProtectedRoute allowedRoles={["customer"]}>
                  <ReturnsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/inbox"
              element={
                <ProtectedRoute allowedRoles={["customer"]}>
                  <InboxPage />
                </ProtectedRoute>
              }
            />
            <Route path="/dev/cards" element={<DevCardsPage />} />
            <Route
              path="/profile"
              element={
                <ProtectedRoute>
                  <ProfilePage />
                </ProtectedRoute>
              }
            />
            <Route path="/clock" element={<ClockPage />} />

            {/* Seller Console Routes under /seller */}
            <Route
              path="/seller"
              element={
                <SellerLayout>
                  <SellerDashboardPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/orders"
              element={
                <SellerLayout>
                  <SellerOrdersPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/returns"
              element={
                <SellerLayout>
                  <SellerReturnsPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/customers"
              element={
                <SellerLayout>
                  <SellerCustomersPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/carrier"
              element={
                <SellerLayout>
                  <SellerCarrierPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/mailbox"
              element={
                <SellerLayout>
                  <SellerMailboxPage />
                </SellerLayout>
              }
            />
            <Route
              path="/seller/settings"
              element={
                <SellerLayout>
                  <OwnerOnlyRoute>
                    <SellerSettingsPage />
                  </OwnerOnlyRoute>
                </SellerLayout>
              }
            />
            <Route
              path="/seller/demo"
              element={
                <SellerLayout>
                  <OwnerOnlyRoute>
                    <SellerDemoPage />
                  </OwnerOnlyRoute>
                </SellerLayout>
              }
            />
            <Route
              path="/seller/shield/*"
              element={
                <SellerLayout>
                  <SellerShieldPlaceholderPage />
                </SellerLayout>
              }
            />

            {/* Fallback 404 */}
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Layout>
      </AuthProvider>
    </BrowserRouter>
  );
}
