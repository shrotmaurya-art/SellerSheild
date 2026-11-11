import { authFetch } from "../../api/client";
import { useEffect, useState } from "react";
import {
  AlertCircle,
  Inbox,
  MessageSquare,
  RefreshCw,
  Send,
  ShieldCheck,
} from "lucide-react";
import type { components } from "../../api/schema";
import { formatISTWithRelative } from "../../utils/date";

type MailMessage = components["schemas"]["MailMessageResponse"];

export function SellerMailboxPage() {
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [filterType, setFilterType] = useState<"all" | "inbound" | "outbound">("all");
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [threadMessages, setThreadMessages] = useState<MailMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);

  const fetchMailbox = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch("/api/v1/mail/inbox", {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(`Failed to load mailbox messages (${res.status})`);
      }
      const data = await res.json();
      setMessages(data.messages || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load mailbox");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMailbox();
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
      setThreadMessages(messages.filter((m) => m.thread_id === threadId));
    } finally {
      setLoadingThread(false);
    }
  };

  const handleSendReply = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!replyText.trim() || !selectedThreadId) return;

    setSending(true);
    try {
      const currentThread = threadMessages[0] || messages.find((m) => m.thread_id === selectedThreadId);
      const recipient = currentThread ? currentThread.from_addr : "customer@example.test";

      const idemKey = `seller-reply-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const res = await authFetch("/api/v1/mail/send", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey,
        },
        credentials: "include",
        body: JSON.stringify({
          to_addr: recipient,
          subject: currentThread ? `Re: ${currentThread.subject}` : "Store Customer Notice",
          body_text: replyText,
          channel: "email",
          thread_id: selectedThreadId,
        }),
      });

      if (res.ok) {
        const sent = await res.json();
        setThreadMessages((prev) => [...prev, sent]);
        setReplyText("");
        fetchMailbox();
      }
    } catch {
      // handle error
    } finally {
      setSending(false);
    }
  };

  const filteredMessages = messages.filter((m) => {
    const isInbound = m.from_addr.includes("example.test");
    if (filterType === "inbound") return isInbound;
    if (filterType === "outbound") return !isInbound;
    return true;
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h1 className="text-2xl font-bold text-white tracking-tight">Store Communications</h1>
            <span className="text-[10px] font-mono font-semibold uppercase bg-amber-900/60 text-amber-300 px-2 py-0.5 rounded border border-amber-500/30">
              SIMULATED
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1">
            Simulated inbound customer inquiries and outbound notifications across Email and WhatsApp.
          </p>
        </div>

        <button
          onClick={fetchMailbox}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-navy-700 bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium transition-colors shadow-sm self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Filter Tabs */}
      <div className="flex items-center space-x-2 border-b border-navy-800 pb-3 text-xs">
        <button
          onClick={() => setFilterType("all")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            filterType === "all"
              ? "bg-teal-500/20 text-teal-300 border border-teal-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          All ({messages.length})
        </button>
        <button
          onClick={() => setFilterType("inbound")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            filterType === "inbound"
              ? "bg-blue-500/20 text-blue-300 border border-blue-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          Inbound from Customers
        </button>
        <button
          onClick={() => setFilterType("outbound")}
          className={`px-3 py-1.5 rounded-xl font-medium transition-colors ${
            filterType === "outbound"
              ? "bg-purple-500/20 text-purple-300 border border-purple-500/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          Outbound Notices
        </button>
      </div>

      {/* Grid: Message List + Thread Viewer */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Messages List */}
        <div className="lg:col-span-5 space-y-3">
          {loading ? (
            <div className="space-y-3">
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="p-4 rounded-xl border border-navy-800 bg-navy-900 space-y-2 animate-pulse"
                >
                  <div className="h-4 bg-navy-800 rounded w-2/3"></div>
                  <div className="h-3 bg-navy-800 rounded w-1/2"></div>
                </div>
              ))}
            </div>
          ) : error ? (
            <div className="p-6 rounded-2xl border border-red-500/30 bg-red-950/20 text-center space-y-3">
              <AlertCircle className="h-6 w-6 text-red-400 mx-auto" />
              <p className="text-xs text-red-300 font-medium">{error}</p>
              <button
                onClick={fetchMailbox}
                className="px-3.5 py-1.5 rounded-xl bg-red-900/60 hover:bg-red-900 text-red-200 text-xs font-semibold transition-colors"
              >
                Try Again
              </button>
            </div>
          ) : filteredMessages.length === 0 ? (
            <div className="bg-navy-900 border border-navy-800/80 rounded-2xl p-8 text-center text-slate-400 space-y-2">
              <Inbox className="h-8 w-8 mx-auto text-slate-600" />
              <p className="text-xs font-semibold text-slate-300">No messages in this folder</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filteredMessages.map((msg) => {
                const isSelected = selectedThreadId === msg.thread_id;
                const isInbound = msg.from_addr.includes("example.test");

                return (
                  <div
                    key={msg.message_id}
                    onClick={() => loadThread(msg.thread_id)}
                    className={`p-4 rounded-xl border cursor-pointer transition-all ${
                      isSelected
                        ? "border-teal-500 bg-teal-950/30 shadow-sm"
                        : "border-navy-800 bg-navy-900 hover:border-navy-700"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex items-center space-x-2">
                        <span
                          className={`h-2 w-2 rounded-full ${
                            isInbound ? "bg-amber-400" : "bg-teal-400"
                          }`}
                        />
                        <span className="text-xs font-semibold text-white truncate">
                          {msg.from_addr}
                        </span>
                      </div>
                      <span className="text-[10px] text-slate-400 whitespace-nowrap">
                        {formatISTWithRelative(msg.created_at, false)}
                      </span>
                    </div>

                    <p className="text-xs font-semibold text-slate-200 mt-1 line-clamp-1">
                      {msg.subject}
                    </p>
                    <p className="text-[11px] text-slate-400 mt-0.5 line-clamp-2">
                      {msg.body_text}
                    </p>

                    <div className="mt-2 flex items-center justify-between text-[10px] text-slate-500">
                      <span className="uppercase tracking-wider font-mono bg-navy-950 px-1.5 py-0.5 rounded border border-navy-800 text-teal-300">
                        {msg.channel}
                      </span>
                      {msg.verified_sender && (
                        <span className="flex items-center text-teal-400 space-x-0.5">
                          <ShieldCheck className="h-3 w-3" />
                          <span>Verified</span>
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Thread Detail */}
        <div className="lg:col-span-7">
          {selectedThreadId ? (
            <div className="bg-navy-900 rounded-2xl border border-navy-800 shadow-sm overflow-hidden flex flex-col min-h-[480px]">
              {/* Thread Header */}
              <div className="p-4 border-b border-navy-800 bg-navy-950/60 flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-bold text-white">
                    {threadMessages[0]?.subject || "Message Thread"}
                  </h3>
                  <p className="text-[11px] text-slate-400 font-mono mt-0.5">
                    Thread ID: {selectedThreadId}
                  </p>
                </div>
                <span className="text-[10px] font-mono uppercase bg-amber-900/60 text-amber-300 px-2 py-0.5 rounded border border-amber-500/30">
                  SIMULATED
                </span>
              </div>

              {/* Messages dialogue */}
              <div className="flex-1 p-4 space-y-4 overflow-y-auto max-h-[380px]">
                {loadingThread ? (
                  <div className="flex items-center justify-center py-12 text-slate-400 text-xs">
                    <RefreshCw className="h-4 w-4 animate-spin mr-2" />
                    <span>Loading conversation...</span>
                  </div>
                ) : (
                  threadMessages.map((m) => {
                    const isStaff = !m.from_addr.includes("example.test");
                    return (
                      <div
                        key={m.message_id}
                        className={`flex flex-col ${isStaff ? "items-end" : "items-start"}`}
                      >
                        <div className="flex items-center space-x-1.5 text-[11px] text-slate-400 mb-1 px-1">
                          <span className="font-semibold text-slate-300">{m.from_addr}</span>
                          <span>&bull;</span>
                          <span>{formatISTWithRelative(m.created_at, true)}</span>
                        </div>

                        <div
                          className={`max-w-[85%] rounded-2xl p-3.5 text-xs shadow-sm ${
                            isStaff
                              ? "bg-teal-600 text-white rounded-br-sm"
                              : "bg-navy-950 text-slate-200 rounded-bl-sm border border-navy-800"
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
              <form onSubmit={handleSendReply} className="p-3 border-t border-navy-800 bg-navy-950/40">
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    value={replyText}
                    onChange={(e) => setReplyText(e.target.value)}
                    placeholder="Type store simulated reply..."
                    className="flex-1 px-3 py-2 text-xs rounded-xl border border-navy-800 bg-navy-950 text-white focus:outline-none focus:ring-2 focus:ring-teal-500"
                  />
                  <button
                    type="submit"
                    disabled={sending || !replyText.trim()}
                    className="inline-flex items-center space-x-1 px-3 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white text-xs font-semibold shadow-sm transition-colors"
                  >
                    <Send className="h-3.5 w-3.5" />
                    <span>{sending ? "Sending..." : "Send"}</span>
                  </button>
                </div>
              </form>
            </div>
          ) : (
            <div className="bg-navy-900 rounded-2xl border border-dashed border-navy-800 p-12 text-center text-slate-500 flex flex-col items-center justify-center min-h-[360px]">
              <MessageSquare className="h-10 w-10 text-slate-700 mb-2" />
              <p className="text-sm font-semibold text-slate-400">Select a message</p>
              <p className="text-xs text-slate-500 mt-1">
                Choose a customer communication thread from the left to view messages.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
