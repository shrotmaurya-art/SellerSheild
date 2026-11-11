import React, { useEffect, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { Link, useNavigate } from "react-router-dom";
import {
  AlertCircle,
  Bot,
  HelpCircle,
  Loader2,
  LogIn,
  MessageSquare,
  Package,
  RefreshCw,
  Send,
  UserCheck,
  X,
  Zap,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useAuth } from "../context/AuthContext";
import { useChatStream } from "../hooks/useChatStream";
import {
  CaseCreatedCard,
  ConfirmationCard,
  OrderSummaryCard,
  ReturnPreviewCard,
  ShipmentTimelineCard,
} from "./chat/cards";

export function ChatWidget() {
  const navigate = useNavigate();
  const [isOpen, setIsOpen] = useState(false);
  const [inputText, setInputText] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { user } = useAuth();
  const currentCustomerId = user?.customer_id || null;
  const prevCustomerIdRef = useRef<string | null>(currentCustomerId);

  const {
    messages,
    isStreaming,
    isGuidedMode,
    sessionId,
    chips,
    toolStatus,
    sendMessage,
    createSession,
    resetSession,
  } = useChatStream();

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    if (isOpen) {
      scrollToBottom();
    }
  }, [messages, isOpen]);

  // When active customer changes (login, logout, switch user), isolate chat sessions
  useEffect(() => {
    if (prevCustomerIdRef.current !== currentCustomerId) {
      prevCustomerIdRef.current = currentCustomerId;
      resetSession();
      if (isOpen && currentCustomerId) {
        createSession();
      }
    }
  }, [currentCustomerId, isOpen, resetSession, createSession]);

  // Create a session when opening widget as authenticated customer
  useEffect(() => {
    if (isOpen && currentCustomerId && !sessionId) {
      createSession();
    }
  }, [isOpen, currentCustomerId, sessionId, createSession]);

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!inputText.trim() || isStreaming) return;
    const textToSend = inputText;
    setInputText("");
    await sendMessage(textToSend);
  };

  const handleChipAction = async (chip: { label: string; action: string; order_id?: string }) => {
    if (isStreaming) return;
    await sendMessage(chip.label, { name: chip.action, order_id: chip.order_id });
  };

  /** Map tool names to friendly display labels. */
  const toolDisplayName = (name: string): string => {
    const map: Record<string, string> = {
      get_order: "Fetching order details",
      get_shipping_status: "Checking shipment status",
      list_my_orders: "Loading your orders",
      get_refund_status: "Checking refund status",
      check_return_eligibility: "Evaluating return eligibility",
      get_policy_summary: "Loading store policy",
    };
    return map[name] || `Running ${name}`;
  };

  // Derive quick action list — use server chips if available, else static defaults
  const quickActions =
    chips.length > 0
      ? chips
      : [
        { label: "Track order", action: "track_order" },
        { label: "Start a return", action: "start_return" },
        { label: "Refund status", action: "refund_status" },
        { label: "Talk to human", action: "talk_to_human" },
      ];

  const quickActionIcons: Record<string, React.ReactNode> = {
    track_order: <Package className="h-3.5 w-3.5 text-teal-600" />,
    start_return: <RefreshCw className="h-3.5 w-3.5 text-teal-600" />,
    refund_status: <HelpCircle className="h-3.5 w-3.5 text-teal-600" />,
    policy_info: <HelpCircle className="h-3.5 w-3.5 text-teal-600" />,
    talk_to_human: <UserCheck className="h-3.5 w-3.5 text-teal-600" />,
  };

  return (
    <div className="fixed bottom-6 right-6 z-50">
      {/* Floating Toggle Button */}
      {!isOpen && (
        <button
          onClick={() => setIsOpen(true)}
          className="relative h-14 w-14 rounded-2xl bg-gradient-to-br from-teal-500 to-teal-700 text-white shadow-2xl flex items-center justify-center transition-all hover:scale-105 active:scale-95 focus:outline-none focus:ring-4 focus:ring-teal-500/30 btn-tactile group"
          aria-label="Open SellerShield Assistant chat"
        >
          <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5">
            <span className="status-dot-pulse absolute inline-flex h-full w-full rounded-full bg-teal-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3.5 w-3.5 bg-teal-300 border-2 border-white"></span>
          </span>
          <MessageSquare className="h-6 w-6 group-hover:scale-110 transition-transform duration-150" />
        </button>
      )}

      {/* Floating Chat Modal */}
      {isOpen && (
        <div className="bg-white rounded-3xl shadow-2xl border border-slate-200/90 w-[92vw] sm:w-[420px] h-[580px] flex flex-col overflow-hidden animate-scale-in">
          {/* Header */}
          <div className="bg-[#0B1426] text-white p-4 flex items-center justify-between border-b border-slate-800">
            <div className="flex items-center space-x-3">
              <div className="h-9 w-9 rounded-xl bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
                <Bot className="h-5 w-5" />
              </div>
              <div>
                <div className="flex items-center space-x-2">
                  <h3 className="font-extrabold text-sm tracking-tight">SellerShield Assistant</h3>
                  <span className="bg-teal-950 text-teal-300 text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border border-teal-700/60">
                    AI AGENT
                  </span>
                </div>
                <p className="text-[11px] text-slate-400 font-mono">
                  {isGuidedMode ? "Guided Mode" : "Live Agent Connected"}
                </p>
              </div>
            </div>

            <button
              onClick={() => setIsOpen(false)}
              className="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800 transition-colors"
              aria-label="Close chat window"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Guided Mode Banner */}
          {isGuidedMode && (
            <div className="bg-amber-50 border-b border-amber-200/80 px-4 py-2 text-xs text-amber-900 flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <AlertCircle className="h-4 w-4 text-amber-600 flex-shrink-0" />
                <span className="font-semibold">Guided mode active</span>
                <span className="text-amber-700 hidden sm:inline">&bull; Agent server standing by</span>
              </div>
            </div>
          )}

          {/* Tool Status Indicator */}
          {toolStatus && isStreaming && (
            <div className="bg-blue-50 border-b border-blue-200/80 px-4 py-2 text-xs text-blue-900 flex items-center space-x-2">
              <Loader2 className="h-3.5 w-3.5 text-blue-600 animate-spin flex-shrink-0" />
              <span className="font-medium">{toolDisplayName(toolStatus.tool_name)}</span>
              <span className="text-blue-600 font-mono text-[10px]">{toolStatus.status}</span>
            </div>
          )}

          {/* Messages Area */}
          <div
            className="flex-1 overflow-y-auto p-4 space-y-3 bg-[#F8FAFC] custom-scrollbar"
            aria-live="polite"
            role="log"
          >
            {messages.length === 0 && (
              <div className="text-center py-8 px-4 animate-fade-in">
                <div className="h-12 w-12 rounded-2xl bg-teal-50 text-teal-600 flex items-center justify-center mx-auto mb-3 border border-teal-100">
                  <Bot className="h-6 w-6" />
                </div>
                <h4 className="text-sm font-bold text-navy-950">How may I assist you?</h4>
                <p className="text-xs text-slate-500 mt-1 max-w-xs mx-auto leading-relaxed">
                  Query order tracking, check return policies, initiate claims, or choose an action below.
                </p>
              </div>
            )}

            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex flex-col ${msg.sender === "customer" ? "items-end" : "items-start"
                  } animate-fade-in`}
              >
                <div
                  className={`max-w-[85%] rounded-2xl px-4 py-3 text-xs sm:text-sm leading-relaxed shadow-sm ${msg.sender === "customer"
                      ? "bg-teal-700 text-white rounded-br-none whitespace-pre-wrap break-words"
                      : "bg-white text-slate-900 border border-slate-200/80 rounded-bl-none"
                    }`}
                >
                  {msg.sender === "customer" ? (
                    // Customer text is strictly rendered as plain escaped text
                    <span>{msg.text}</span>
                  ) : (
                    // Assistant response is sanitized with DOMPurify
                    <div className="prose prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-li:my-0.5 text-slate-800">
                      <ReactMarkdown>
                        {DOMPurify.sanitize(msg.text)}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>

                {/* Render agent cards if present */}
                {msg.sender === "assistant" && msg.cards && msg.cards.length > 0 && (
                  <div className="mt-2 max-w-[95%] space-y-2">
                    {msg.cards.map((card, idx) => {
                      const cardType = (card.card_type || card.type) as string;
                      const p = (card.payload || card) as Record<string, any>;

                      if (cardType === "order_summary") {
                        return (
                          <OrderSummaryCard
                            key={idx}
                            orderId={p.orderId || p.order_id || ""}
                            status={p.status || ""}
                            placedAt={p.placedAt || p.created_at || ""}
                            items={
                              Array.isArray(p.items)
                                ? p.items.map((it: any) => ({
                                  itemId: it.itemId || it.item_id || "",
                                  name: it.name || it.title || "Item",
                                  qty: it.qty || it.quantity || 1,
                                  linePaidPaise: it.linePaidPaise ?? it.price_paise ?? 0,
                                  sku: it.sku || "",
                                }))
                                : []
                            }
                            subtotalPaise={p.subtotalPaise ?? p.total_paise ?? 0}
                            discountPaise={p.discountPaise ?? 0}
                            shippingFeePaise={p.shippingFeePaise ?? 0}
                            totalPaise={p.totalPaise ?? p.total_paise ?? 0}
                            onViewOrder={(oid) => {
                              setIsOpen(false);
                              navigate(`/orders/${oid}`);
                            }}
                          />
                        );
                      }

                      if (cardType === "shipment_timeline") {
                        const rawEv = p.events || p.timeline || [];
                        return (
                          <ShipmentTimelineCard
                            key={idx}
                            orderId={p.orderId || p.order_id || ""}
                            carrier={p.carrier || "Courier"}
                            trackingNo={p.trackingNo || p.tracking_no || ""}
                            etaAt={p.etaAt || p.eta || null}
                            delayed={Boolean(p.delayed)}
                            events={
                              Array.isArray(rawEv)
                                ? rawEv.map((ev: any, i: number) => ({
                                  eventId: ev.eventId || ev.id || `ev-${i}`,
                                  ts: ev.ts || ev.timestamp || ev.occurred_at || "",
                                  status: ev.status || "",
                                  location: ev.location || "",
                                  note: ev.note || ev.description || "",
                                }))
                                : []
                            }
                          />
                        );
                      }

                      if (cardType === "return_preview") {
                        return (
                          <ReturnPreviewCard
                            key={idx}
                            orderId={p.orderId || p.order_id || ""}
                            items={
                              Array.isArray(p.items)
                                ? p.items.map((it: any) => ({
                                  name: it.name || it.title || "Item",
                                  qty: it.qty || it.quantity || 1,
                                  linePaidPaise: it.linePaidPaise || it.price_paise || 0,
                                }))
                                : []
                            }
                            reasonCode={p.reasonCode || p.reason_code || "standard_return"}
                            reasonText={p.reasonText || p.reason_text || "Standard return request"}
                            resolution={p.resolution || "refund"}
                            refundAmountPaise={
                              p.estimatedRefundPaise ?? p.total_refund_paise ?? 0
                            }
                            shippingFeeNote={p.shippingFeeNote || "Free return pickup"}
                            policyNote={p.policyNote}
                          />
                        );
                      }

                      if (cardType === "confirmation") {
                        return (
                          <ConfirmationCard
                            key={idx}
                            title={
                              p.title ||
                              `Confirm ${p.type === "cancel" ? "Cancellation" : "Return"}`
                            }
                            description={
                              p.description ||
                              `Please confirm return for order ${p.orderId || p.order_id || ""}`
                            }
                            details={
                              Array.isArray(p.details)
                                ? p.details
                                : [
                                  {
                                    label: "Order ID",
                                    value: p.orderId || p.order_id || "",
                                  },
                                  {
                                    label: "Estimated Refund",
                                    value: `₹${(
                                      (p.estimatedRefundPaise ||
                                        p.refund_amount_paise ||
                                        0) /
                                      100
                                    ).toFixed(2)}`,
                                  },
                                ]
                            }
                            expiresAt={p.expiresAt || p.expires_at}
                            onConfirm={() =>
                              handleChipAction({
                                label: "Confirm",
                                action: "confirm",
                                order_id: p.orderId || p.order_id,
                              })
                            }
                            onCancel={() =>
                              handleChipAction({
                                label: "Cancel",
                                action: "cancel",
                                order_id: p.orderId || p.order_id,
                              })
                            }
                          />
                        );
                      }

                      if (cardType === "case_created") {
                        return (
                          <CaseCreatedCard
                            key={idx}
                            caseId={p.caseId || p.case_id || ""}
                            orderId={p.orderId || p.order_id}
                            priority={p.priority || "medium"}
                            status={p.status || "Open"}
                            subject={
                              p.subject || p.summary || p.category || "Support Case"
                            }
                            expectedResponse={p.slaDueAt || p.sla_due_at || "Within 24 hours"}
                          />
                        );
                      }

                      if (cardType === "chips" && Array.isArray(p.chips || p.items)) {
                        const chipItems = (p.chips || p.items) as {
                          label: string;
                          action: string;
                          order_id?: string;
                        }[];
                        return (
                          <div
                            key={idx}
                            className="flex flex-wrap gap-1.5 p-2 bg-white rounded-xl border border-slate-200"
                          >
                            {chipItems.map((item, i) => (
                              <button
                                key={i}
                                onClick={() => handleChipAction(item)}
                                className="px-2.5 py-1 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 text-[11px] font-semibold transition-colors"
                              >
                                {item.label}
                              </button>
                            ))}
                          </div>
                        );
                      }

                      return null;
                    })}
                  </div>
                )}

                <span className="text-[10px] text-slate-400 font-mono mt-1 px-1">
                  {msg.timestamp.toLocaleTimeString("en-IN", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              </div>
            ))}

            {isStreaming && !toolStatus && (
              <div className="flex items-center space-x-2 text-slate-400 text-xs italic px-2 animate-fade-in">
                <div className="h-2 w-2 rounded-full bg-teal-500 status-dot-pulse"></div>
                <span className="font-mono text-[11px]">SellerShield is formulating response...</span>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>

          {/* Quick Actions Bar & Input Form OR Sign-in Prompt */}
          {!currentCustomerId ? (
            <div className="p-4 bg-white border-t border-slate-200/80 text-center space-y-2">
              <p className="text-xs text-slate-600">
                Please sign in to check your orders or chat with SellerShield support.
              </p>
              <Link
                to="/login"
                onClick={() => setIsOpen(false)}
                className="inline-flex items-center space-x-1.5 px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-all"
              >
                <LogIn className="h-4 w-4" />
                <span>Sign in to your account</span>
              </Link>
            </div>
          ) : (
            <>
              {/* Quick Actions Bar */}
              <div className="bg-white border-t border-slate-200/80 px-3 py-2 flex items-center space-x-2 overflow-x-auto scrollbar-none">
                {quickActions.map((qa) => (
                  <button
                    key={qa.action}
                    onClick={() => handleChipAction(qa)}
                    className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-semibold whitespace-nowrap transition-all btn-tactile"
                  >
                    {quickActionIcons[qa.action] || <Zap className="h-3.5 w-3.5 text-teal-600" />}
                    <span>{qa.label}</span>
                  </button>
                ))}
              </div>

              {/* Input & Character Counter Form */}
              <form
                onSubmit={handleSend}
                className="p-3 bg-white border-t border-slate-200/80 flex flex-col space-y-1.5"
              >
                <div className="relative flex items-center">
                  <input
                    type="text"
                    value={inputText}
                    maxLength={1000}
                    onChange={(e) => setInputText(e.target.value)}
                    placeholder="Ask SellerShield a question..."
                    className="w-full text-xs sm:text-sm pl-3.5 pr-10 py-2.5 rounded-xl border border-slate-200 bg-slate-50/50 focus:bg-white focus:outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-500/20 transition-all"
                    disabled={isStreaming}
                  />
                  <button
                    type="submit"
                    disabled={!inputText.trim() || isStreaming}
                    className="absolute right-1.5 p-1.5 rounded-lg bg-teal-600 hover:bg-teal-700 disabled:bg-slate-200 text-white disabled:text-slate-400 transition-all btn-tactile"
                    aria-label="Send message"
                  >
                    <Send className="h-4 w-4" />
                  </button>
                </div>

                <div className="flex justify-between items-center px-1 text-[10px] text-slate-400 font-mono">
                  <span>Press Enter to send</span>
                  <span className={inputText.length >= 950 ? "text-amber-600 font-semibold" : ""}>
                    {inputText.length} / 1000
                  </span>
                </div>
              </form>
            </>
          )}
        </div>
      )}
    </div>
  );
}
