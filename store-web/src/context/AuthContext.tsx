import React, { createContext, useContext, useEffect, useState } from "react";
import {
  apiClient,
  getAccessToken,
  refreshAccessToken,
  setAccessToken,
} from "../api/client";
import type { paths } from "../api/schema";

export type AuthUser = paths["/api/v1/auth/me"]["get"]["responses"][200]["content"]["application/json"];

interface AuthContextType {
  user: AuthUser | null;
  loading: boolean;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<{ success: boolean; error?: string }>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  // Fetch current user details with access token
  const refreshUser = async () => {
    const token = getAccessToken();
    if (!token) {
      setUser(null);
      return;
    }

    try {
      const { data, error } = await apiClient.GET("/api/v1/auth/me");
      if (error || !data) {
        setUser(null);
        setAccessToken(null);
      } else {
        setUser(data);
      }
    } catch {
      setUser(null);
      setAccessToken(null);
    }
  };

  // Silent session restore on initial page load / refresh
  useEffect(() => {
    let mounted = true;

    async function initAuth() {
      try {
        const token = await refreshAccessToken();
        if (mounted && token) {
          const { data } = await apiClient.GET("/api/v1/auth/me");
          if (mounted && data) {
            setUser(data);
          }
        }
      } catch {
        if (mounted) {
          setUser(null);
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    initAuth();

    return () => {
      mounted = false;
    };
  }, []);

  const login = async (email: string, password: string) => {
    try {
      const { data, error, response } = await apiClient.POST("/api/v1/auth/login", {
        body: { email, password },
      });

      if (error || !data) {
        let msg = "Invalid email or password";
        if (response?.status === 429) {
          msg = "Too many attempts. Account locked for 15 minutes.";
        } else if (typeof error === "object" && error && "error" in error) {
          msg = (error as { error: { message: string } }).error.message || msg;
        }
        return { success: false, error: msg };
      }

      setAccessToken(data.access_token);

      // Fetch user profile
      const meRes = await apiClient.GET("/api/v1/auth/me");
      if (meRes.data) {
        setUser(meRes.data);
      }

      return { success: true };
    } catch (err) {
      return {
        success: false,
        error: err instanceof Error ? err.message : "Connection failed",
      };
    }
  };

  const logout = async () => {
    try {
      await apiClient.POST("/api/v1/auth/logout");
    } finally {
      setAccessToken(null);
      setUser(null);
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isAuthenticated: !!user,
        login,
        logout,
        refreshUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

