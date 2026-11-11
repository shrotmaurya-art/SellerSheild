import { useCallback, useRef, useState } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { getAccessToken } from "../api/client";

export interface ChatMessage {
  id: string;
  sender: "customer" | "assistant" | "system";
  text: string;
  timestamp: Date;
  status?: "sending" | "sent" | "error";
  quickActions?: string[];
  /** Rich cards emitted by the agent (order summary, timeline, confirmation). */
  cards?: Record<string, unknown>[];
}

export interface Chip {
  label: string;
  action: string;
  order_id?: string;
}

export function useChatStream() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isGuidedMode, setIsGuidedMode] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chips, setChips] = useState<Chip[]>([]);
  const [toolStatus, setToolStatus] = useState<{ tool_name: string; status: string } | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const msgCounterRef = useRef(0);

  const abort = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((prev) => [...prev, message]);
  }, []);

  /**
   * Create a chat session with the agent on mount.
   * POST /agent/v1/chat/sessions -> { session_id, chips }
   */
  const createSession = useCallback(async () => {
    const token = getAccessToken();
    if (!token) {
      return null;
    }
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    };

    try {
      const res = await fetch("/agent/v1/chat/sessions", {
        method: "POST",
        headers,
        credentials: "include",
      });

      if (!res.ok) {
        // Agent unavailable — switch to guided mode
        setIsGuidedMode(true);
        return null;
      }

      const data = await res.json();
      setIsGuidedMode(false);
      setSessionId(data.session_id);
      if (data.chips && Array.isArray(data.chips)) {
        setChips(data.chips);
      }
      return data.session_id as string;
    } catch {
      setIsGuidedMode(true);
      return null;
    }
  }, []);

  /** Generate a stable client message id for idempotency. */
  const nextClientMsgId = useCallback(() => {
    msgCounterRef.current += 1;
    return `web-${Date.now()}-${msgCounterRef.current}`;
  }, []);

  /**
   * Send a chat message or guided action.
   * Attempts SSE stream to POST /agent/v1/chat/sessions/{session_id}/messages.
   * Falls back to Guided mode when the agent is unreachable.
   */
  const sendMessage = useCallback(
    async (text: string, action?: { name: string; order_id?: string }) => {
      const trimmed = text.trim();
      if (!trimmed && !action) return;

      // Add customer bubble immediately
      const customerMsgId = `cust-${Date.now()}`;
      const customerMsg: ChatMessage = {
        id: customerMsgId,
        sender: "customer",
        text: trimmed || (action ? action.name : ""),
        timestamp: new Date(),
        status: "sent",
      };
      setMessages((prev) => [...prev, customerMsg]);
      setIsStreaming(true);
      setToolStatus(null);

      const assistantMsgId = `asst-${Date.now()}`;
      let accumulatedText = "";
      const accumulatedCards: Record<string, unknown>[] = [];

      // If guided mode, use local responses
      if (isGuidedMode) {
        setTimeout(() => {
          const reply = getGuidedResponse(trimmed);
          setMessages((prev) => [
            ...prev,
            {
              id: assistantMsgId,
              sender: "assistant",
              text: reply,
              timestamp: new Date(),
            },
          ]);
          setIsStreaming(false);
        }, 400);
        return;
      }

      // Ensure we have a session
      let activeSession = sessionId;
      if (!activeSession) {
        activeSession = await createSession();
        if (!activeSession) {
          // Agent down — guided mode
          const reply = getGuidedResponse(trimmed);
          setMessages((prev) => [
            ...prev,
            {
              id: assistantMsgId,
              sender: "assistant",
              text: reply,
              timestamp: new Date(),
            },
          ]);
          setIsStreaming(false);
          return;
        }
      }

      // Prepare SSE request
      abort();
      const ctrl = new AbortController();
      abortControllerRef.current = ctrl;

      const token = getAccessToken();
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }

      const clientMsgId = nextClientMsgId();
      const body: Record<string, unknown> = {
        client_msg_id: clientMsgId,
      };
      if (action) {
        body.action = action;
      } else {
        body.text = trimmed;
      }

      try {
        await fetchEventSource(
          `/agent/v1/chat/sessions/${activeSession}/messages`,
          {
            method: "POST",
            headers,
            body: JSON.stringify(body),
            signal: ctrl.signal,
            async onopen(response) {
              if (!response.ok) {
                throw new Error(`Agent unavailable (${response.status})`);
              }
            },
            onmessage(msg) {
              if (!msg.event || !msg.data) return;

              try {
                const parsed = JSON.parse(msg.data);

                switch (msg.event) {
                  case "message.delta": {
                    const delta = parsed.delta || "";
                    accumulatedText += delta;

                    setMessages((prev) => {
                      const existing = prev.find((m) => m.id === assistantMsgId);
                      if (existing) {
                        return prev.map((m) =>
                          m.id === assistantMsgId
                            ? { ...m, text: accumulatedText }
                            : m,
                        );
                      } else {
                        return [
                          ...prev,
                          {
                            id: assistantMsgId,
                            sender: "assistant" as const,
                            text: accumulatedText,
                            timestamp: new Date(),
                          },
                        ];
                      }
                    });
                    break;
                  }

                  case "tool.status": {
                    setToolStatus({
                      tool_name: parsed.tool_name || "unknown",
                      status: parsed.status || "running",
                    });
                    break;
                  }

                  case "card": {
                    accumulatedCards.push(parsed);
                    // Update the assistant message with cards
                    setMessages((prev) =>
                      prev.map((m) =>
                        m.id === assistantMsgId
                          ? { ...m, cards: [...accumulatedCards] }
                          : m,
                      ),
                    );
                    break;
                  }

                  case "final": {
                    // Replace text with full verified text from server
                    const fullText = parsed.full_text || accumulatedText;
                    setMessages((prev) =>
                      prev.map((m) =>
                        m.id === assistantMsgId
                          ? {
                              ...m,
                              text: fullText,
                              cards:
                                accumulatedCards.length > 0
                                  ? [...accumulatedCards]
                                  : m.cards,
                            }
                          : m,
                      ),
                    );
                    setToolStatus(null);
                    setIsStreaming(false);
                    break;
                  }
                }
              } catch {
                // Non-JSON data — append raw
                accumulatedText += msg.data;
                setMessages((prev) => {
                  const existing = prev.find((m) => m.id === assistantMsgId);
                  if (existing) {
                    return prev.map((m) =>
                      m.id === assistantMsgId
                        ? { ...m, text: accumulatedText }
                        : m,
                    );
                  } else {
                    return [
                      ...prev,
                      {
                        id: assistantMsgId,
                        sender: "assistant" as const,
                        text: accumulatedText,
                        timestamp: new Date(),
                      },
                    ];
                  }
                });
              }
            },
            onclose() {
              setToolStatus(null);
              setIsStreaming(false);
            },
            onerror(err) {
              // Agent unreachable — activate guided mode fallback
              setIsGuidedMode(true);
              const fallbackReply = getGuidedResponse(trimmed);
              setMessages((prev) => {
                const withoutEmpty = prev.filter(
                  (m) => m.id !== assistantMsgId,
                );
                return [
                  ...withoutEmpty,
                  {
                    id: assistantMsgId,
                    sender: "assistant" as const,
                    text: fallbackReply,
                    timestamp: new Date(),
                  },
                ];
              });
              setToolStatus(null);
              setIsStreaming(false);
              // Throw to prevent infinite reconnection attempts
              throw err;
            },
          },
        );
      } catch {
        if (!isGuidedMode) {
          setIsGuidedMode(true);
        }
        setToolStatus(null);
        setIsStreaming(false);
      }
    },
    [abort, isGuidedMode, sessionId, createSession, nextClientMsgId],
  );

  /** Reset session (e.g. after a demo reset clears the DB or customer changes). */
  const resetSession = useCallback(() => {
    abort();
    setSessionId(null);
    setMessages([]);
    setChips([]);
    setIsGuidedMode(false);
    setToolStatus(null);
  }, [abort]);

  return {
    messages,
    isStreaming,
    isGuidedMode,
    sessionId,
    chips,
    toolStatus,
    sendMessage,
    addMessage,
    abort,
    setIsGuidedMode,
    createSession,
    resetSession,
  };
}

/**
 * Deterministic local replies for Guided Mode when the live AI Agent is unavailable.
 */
export function getGuidedResponse(intentOrText: string): string {
  const lower = intentOrText.toLowerCase();

  if (lower.includes("track") || lower.includes("where is my order")) {
    return (
      "**Order Tracking**\n\n" +
      "You can view live shipment milestones for your orders directly on your **My Orders** page. " +
      "Click on any order to view its real-time carrier timeline and estimated delivery date."
    );
  }

  if (lower.includes("return") || lower.includes("start a return")) {
    return (
      "**Returns Policy**\n\n" +
      "• **7-Day Window**: Eligible items can be returned within 7 days of delivery.\n" +
      "• **Defective Items**: Covered with free reverse shipping.\n" +
      "• **Hygiene & Final Sale**: Personal care and hygiene items cannot be returned.\n\n" +
      "Self-service return requests can be initiated from your order details page once delivered."
    );
  }

  if (lower.includes("refund") || lower.includes("refund status")) {
    return (
      "**Refund Status**\n\n" +
      "All approved refunds are processed back to your original payment method via **SimPay** within 3–5 business days. " +
      "You will receive a simulated confirmation email in your Inbox once released."
    );
  }

  if (
    lower.includes("human") ||
    lower.includes("talk to a human") ||
    lower.includes("agent")
  ) {
    return (
      "**Escalation to Support Specialist**\n\n" +
      "I have flagged your inquiry for priority human review. A support specialist typically replies within **2 hours** during business hours (9:00 AM – 6:00 PM IST)."
    );
  }

  return (
    "I have received your request. In **Guided Mode**, you can use the quick action buttons below " +
    "to track shipments, review return guidelines, check refund timelines, or request human support."
  );
}
