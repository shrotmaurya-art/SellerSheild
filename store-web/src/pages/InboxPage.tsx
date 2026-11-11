import { authFetch } from "../api/client";
import { useEffect, useState } from "react";
import {
  AlertCircle,
  Inbox,
  MessageSquare,
  RefreshCw,
  Send,
  ShieldCheck,
} from "lucide-react";
import type { components } from "../api/schema";
import { formatISTWithRelative } from "../utils/date";

type MailMessage = components["schemas"]["MailMessageResponse"];

export function InboxPage() {
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [threadMessages, setThreadMessages] = useState<MailMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);

  const fetchInbox = async () => {
    setLoading(true);
    setError(null);
    try {
      // Memory-only access token attached automatically by authFetch
      const res = await authFetch("/api/v1/mail/inbox", {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(`Failed to load messages (${res.status})`);
      }
      const data = await res.json();
      setMessages(data.messages || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load messages");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchInbox();
  }, []);

  const loadThread = async (threadId: string) => {
    setSelectedThreadId(threadId);
    setLoadingThread(true);
    try {
      const res = await authFetch(`/api/v1/mail/threads/${threadId}`, {
        credentials: "include",
      });
      if (res.ok) {
        const data = await res.json();
        setThreadMessages(data.messages || []);
      }
    } catch {
      // fallback to filtering current inbox messages
      setThreadMessages(messages.filter((m) => m.thread_id === threadId));
    } finally {
      setLoadingThread(false);
    }
  };

  const markAsRead = async (msgId: string) => {
    try {
      await authFetch(`/api/v1/mail/${msgId}/read`, {
        method: "POST",
        credentials: "include",
      });
      setMessages((prev) =>
        prev.map((m) => (m.message_id === msgId ? { ...m, status: "read" } : m)),
      );
    } catch {
      // silent fail
    }
  };

  const handleSendReply = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!replyText.trim() || !selectedThreadId) return;

    setSending(true);
    try {
      const currentThread = threadMessages[0] || messages.find((m) => m.thread_id === selectedThreadId);
      const recipient = currentThread ? currentThread.from_addr : "support@nestkart.test";

      const idemKey = `mail-reply-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const res = await authFetch("/api/v1/mail/send", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
        body: JSON.stringify({
          to_addr: recipient,
          subject: currentThread ? `Re: ${currentThread.subject}` : "Support Inquiry",
          body_text: replyText,
          channel: "email",
          thread_id: selectedThreadId,
        }),
      });

      if (res.ok) {
        const sent = await res.json();
        setThreadMessages((prev) => [...prev, sent]);
        setReplyText("");
        fetchInbox();
      }
    } catch {
      // handle error
    } finally {
      setSending(false);
    }
  };

  const unreadCount = messages.filter((m) => m.status === "unread").length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between pb-4 border-b border-slate-200/80 gap-4">
        <div>
          <div className="flex items-center space-x-2 text-xs font-semibold text-teal-600 uppercase tracking-wider mb-1">
            <Inbox className="h-3.5 w-3.5" />
            <span>Store Communications</span>
          </div>
          <div className="flex items-center space-x-3">
            <h1 className="text-2xl sm:text-3xl font-extrabold text-navy-950 tracking-tight">Customer Inbox</h1>
            <span
              className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-100 text-amber-900 border border-amber-300 font-mono"
              title="All communications in this environment are simulated"
            >
              SIMULATED
            </span>
            {unreadCount > 0 && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-teal-100 text-teal-800">
                {unreadCount} unread
              </span>
            )}
          </div>
          <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-xl">
            Simulated messages from the store, carrier milestone notices, and SellerShield support threads.
          </p>
        </div>

        <button
          onClick={fetchInbox}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl border border-slate-200/80 bg-white hover:bg-slate-50 text-slate-700 text-xs font-semibold transition-all btn-tactile shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-teal-600" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Main Grid: Messages list + Thread Detail */}
      <div className="grid grid-cols-1 md:grid-cols-12 gap-6">
        {/* Messages List */}
        <div className="md:col-span-5 space-y-3">
          <div className="flex items-center justify-between text-xs font-bold text-slate-400 uppercase tracking-wider px-1 font-mono">
            <span>Conversations</span>
            <span>{messages.length}</span>
          </div>

          {loading ? (
            <div className="space-y-3">
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="p-4 rounded-2xl border border-slate-200/80 bg-white space-y-2 animate-pulse shadow-card"
                >
                  <div className="h-4 bg-slate-200 rounded w-2/3"></div>
                  <div className="h-3 bg-slate-100 rounded w-1/2"></div>
                  <div className="h-3 bg-slate-100 rounded w-full"></div>
                </div>
              ))}
            </div>
          ) : error ? (
            <div className="p-6 rounded-2xl border border-red-200 bg-red-50 text-center space-y-3 shadow-card animate-fade-in">
              <AlertCircle className="h-6 w-6 text-red-500 mx-auto" />
              <p className="text-xs text-red-700 font-semibold">{error}</p>
              <button
                onClick={fetchInbox}
                className="px-3.5 py-1.5 rounded-xl bg-red-600 hover:bg-red-700 text-white text-xs font-semibold shadow-sm transition-all btn-tactile"
              >
                Try again
              </button>
            </div>
          ) : messages.length === 0 ? (
            <div className="p-10 rounded-2xl border border-slate-200/80 bg-white text-center space-y-3 shadow-card">
              <Inbox className="h-8 w-8 text-slate-300 mx-auto" />
              <p className="text-xs text-slate-500 font-medium">No messages in your mailbox.</p>
            </div>
          ) : (
            <div className="space-y-2.5">
              {messages.map((msg) => {
                const isSelected = selectedThreadId === msg.thread_id;
                const isUnread = msg.status === "unread";
                return (
                  <div
                    key={msg.message_id}
                    onClick={() => {
                      loadThread(msg.thread_id);
                      if (isUnread) markAsRead(msg.message_id);
                    }}
                    className={`p-4 rounded-2xl border cursor-pointer transition-all card-hover-lift ${
                      isSelected
                        ? "border-teal-500 bg-teal-50/50 shadow-sm"
                        : isUnread
                        ? "border-slate-300 bg-white hover:border-slate-400 font-medium shadow-card"
                        : "border-slate-200/80 bg-white/70 hover:bg-white text-slate-600 shadow-card"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex items-center space-x-2">
                        {isUnread && (
                          <span className="h-2 w-2 rounded-full bg-teal-600 flex-shrink-0 status-dot-pulse" />
                        )}
                        <span className="text-xs font-bold text-navy-950 truncate font-mono">
                          {msg.from_addr}
                        </span>
                      </div>
                      <span className="text-[10px] text-slate-400 font-mono whitespace-nowrap">
                        {formatISTWithRelative(msg.created_at, false)}
                      </span>
                    </div>

                    <p className="text-xs font-semibold text-navy-950 mt-1.5 line-clamp-1">
                      {msg.subject}
                    </p>
                    <p className="text-[11px] text-slate-500 mt-0.5 line-clamp-2 leading-relaxed">
                      {msg.body_text}
                    </p>

                    <div className="mt-2.5 flex items-center justify-between text-[10px] text-slate-400">
                      <span className="uppercase tracking-wider font-mono bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded font-semibold">
                        {msg.channel}
                      </span>
                      {msg.verified_sender && (
                        <span className="flex items-center text-teal-600 space-x-1 font-semibold">
                          <ShieldCheck className="h-3 w-3" />
                          <span>Verified Sender</span>
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Thread Detail View */}
        <div className="md:col-span-7">
          {selectedThreadId ? (
            <div className="bg-white rounded-2xl border border-slate-200/80 shadow-card overflow-hidden flex flex-col min-h-[480px]">
              {/* Thread Header */}
              <div className="p-4 border-b border-slate-100 bg-slate-50/70 flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-bold text-navy-950">
                    {threadMessages[0]?.subject || "Message Thread"}
                  </h3>
                  <p className="text-[11px] text-slate-400 font-mono mt-0.5">
                    Thread ID: {selectedThreadId}
                  </p>
                </div>
                <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold bg-amber-100 text-amber-900 border border-amber-200 font-mono">
                  SIMULATED
                </span>
              </div>

              {/* Messages Dialogue */}
              <div className="flex-1 p-5 space-y-4 overflow-y-auto max-h-[380px] custom-scrollbar">
                {loadingThread ? (
                  <div className="flex items-center justify-center py-16 text-slate-400 text-xs">
                    <RefreshCw className="h-4 w-4 animate-spin mr-2 text-teal-600" />
                    <span>Loading conversation history...</span>
                  </div>
                ) : (
                  threadMessages.map((m) => {
                    const isFromMe = m.from_addr.includes("example.test");
                    return (
                      <div
                        key={m.message_id}
                        className={`flex flex-col ${isFromMe ? "items-end" : "items-start"} animate-fade-in`}
                      >
                        <div className="flex items-center space-x-1.5 text-[11px] text-slate-400 mb-1 px-1 font-mono">
                          <span className="font-semibold text-slate-700">{m.from_addr}</span>
                          <span>&bull;</span>
                          <span>{formatISTWithRelative(m.created_at, true)}</span>
                        </div>

                        <div
                          className={`max-w-[85%] rounded-2xl p-4 text-xs shadow-sm leading-relaxed ${
                            isFromMe
                              ? "bg-teal-700 text-white rounded-br-none"
                              : "bg-slate-100 text-slate-900 rounded-bl-none border border-slate-200/80"
                          }`}
                        >
                          <p className="whitespace-pre-wrap">{m.body_text}</p>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>

              {/* Reply Form */}
              <form onSubmit={handleSendReply} className="p-3 border-t border-slate-100 bg-slate-50/70">
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    value={replyText}
                    onChange={(e) => setReplyText(e.target.value)}
                    placeholder="Type a simulated reply message..."
                    className="flex-1 px-3.5 py-2.5 text-xs rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-500 transition-all"
                  />
                  <button
                    type="submit"
                    disabled={sending || !replyText.trim()}
                    className="inline-flex items-center space-x-1.5 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-all btn-tactile flex-shrink-0"
                  >
                    <Send className="h-3.5 w-3.5" />
                    <span>{sending ? "Sending..." : "Send"}</span>
                  </button>
                </div>
              </form>
            </div>
          ) : (
            <div className="bg-slate-50/80 rounded-2xl border border-dashed border-slate-200 p-12 text-center text-slate-400 flex flex-col items-center justify-center min-h-[400px]">
              <MessageSquare className="h-10 w-10 text-slate-300 mb-2" />
              <p className="text-sm font-bold text-navy-950">Select a conversation</p>
              <p className="text-xs text-slate-400 mt-1 max-w-xs">
                Pick a message from the list to read the full simulated thread and send replies.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
