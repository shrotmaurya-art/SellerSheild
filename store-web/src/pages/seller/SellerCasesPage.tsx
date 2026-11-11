import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  FileText,
  MessageSquare,
  RefreshCw,
  Send,
  UserCheck,
} from "lucide-react";
import {
  assignSellerCase,
  fetchSellerCaseBrief,
  fetchSellerCases,
  handbackSellerCase,
  sendSellerCaseMessage,
  updateSellerCaseStatus,
  type CaseBrief,
  type SellerCase,
} from "../../api/sellerShield";
import { formatISTWithRelative } from "../../utils/date";

const STATUS_OPTIONS = [
  "open",
  "in_review",
  "waiting_for_customer",
  "waiting_for_seller",
  "resolved",
  "closed",
];

export function SellerCasesPage() {
  const [cases, setCases] = useState<SellerCase[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [priorityFilter, setPriorityFilter] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [brief, setBrief] = useState<CaseBrief | null>(null);

  const [replyText, setReplyText] = useState("");
  const [assigneeInput, setAssigneeInput] = useState("");
  const [handbackNotes, setHandbackNotes] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadCases = useCallback(
    async (preserveSelected = true) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchSellerCases({
          status: statusFilter,
          priority: priorityFilter,
        });
        setCases(res.cases);
        setSelectedId((prev) => {
          if (res.cases.length === 0) return null;
          if (preserveSelected && prev && res.cases.some((c) => c.case_id === prev)) {
            return prev;
          }
          return res.cases[0].case_id;
        });
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : "Failed to load escalation cases");
      } finally {
        setLoading(false);
      }
    },
    [statusFilter, priorityFilter],
  );

  useEffect(() => {
    loadCases(false);
  }, [loadCases]);

  const selectedCase = cases.find((c) => c.case_id === selectedId) || null;

  useEffect(() => {
    if (!selectedId) {
      setBrief(null);
      return;
    }
    setAssigneeInput(selectedCase?.assigned_to || "");
    fetchSellerCaseBrief(selectedId)
      .then(setBrief)
      .catch(() => setBrief(null));
  }, [selectedId, selectedCase?.assigned_to]);

  const handleSendReply = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedCase || !replyText.trim()) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await sendSellerCaseMessage(selectedCase.case_id, replyText.trim());
      setReplyText("");
      setNotice("Reply sent to customer and handler switched to human.");
      await loadCases(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to send reply");
    } finally {
      setBusy(false);
    }
  };

  const handleStatusChange = async (newStatus: string) => {
    if (!selectedCase) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await updateSellerCaseStatus(selectedCase.case_id, newStatus);
      setNotice(`Case ${selectedCase.case_id} updated to ${newStatus}.`);
      await loadCases(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to update case status");
    } finally {
      setBusy(false);
    }
  };

  const handleAssign = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedCase || !assigneeInput.trim()) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await assignSellerCase(selectedCase.case_id, assigneeInput.trim());
      setNotice(`Assigned case to ${assigneeInput.trim()}.`);
      await loadCases(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Assignment failed");
    } finally {
      setBusy(false);
    }
  };

  const handleHandback = async () => {
    if (!selectedCase) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await handbackSellerCase(selectedCase.case_id, handbackNotes);
      setHandbackNotes("");
      setNotice(`Case ${selectedCase.case_id} handed back to autonomous SellerShield AI agent.`);
      await loadCases(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Handback failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-navy-800 gap-4">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">Escalation Cases</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Inspect deterministic case briefs, reply directly to customers, or hand conversation control back to SellerShield AI
          </p>
        </div>

        <button
          type="button"
          onClick={() => loadCases(true)}
          disabled={loading}
          className="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-xl bg-navy-900 hover:bg-navy-800 border border-navy-700 text-slate-300 text-xs font-medium self-start sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh Cases</span>
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <select
          aria-label="Filter by Case Status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-2 rounded-xl bg-navy-900 border border-navy-800 text-white focus:outline-none focus:border-teal-500"
        >
          <option value="all">All Statuses</option>
          {STATUS_OPTIONS.map((st) => (
            <option key={st} value={st}>
              {st}
            </option>
          ))}
        </select>

        <select
          aria-label="Filter by Case Priority"
          value={priorityFilter}
          onChange={(e) => setPriorityFilter(e.target.value)}
          className="px-3 py-2 rounded-xl bg-navy-900 border border-navy-800 text-white focus:outline-none focus:border-teal-500"
        >
          <option value="all">All Priorities</option>
          <option value="high">High Priority</option>
          <option value="normal">Normal Priority</option>
        </select>
      </div>

      {notice && (
        <div className="bg-emerald-950/50 border border-emerald-500/40 rounded-xl p-3.5 text-xs text-emerald-200 flex items-center space-x-2">
          <CheckCircle2 className="h-4 w-4 text-emerald-400 flex-shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-red-500/40 rounded-xl p-3.5 text-xs text-red-200 flex items-center space-x-2">
          <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading ? (
        <div className="py-12 text-center text-xs font-mono text-slate-400">
          Loading escalated cases...
        </div>
      ) : cases.length === 0 ? (
        <div className="bg-navy-900 border border-navy-800 rounded-2xl p-10 text-center space-y-2">
          <MessageSquare className="h-8 w-8 text-teal-400 mx-auto" />
          <div className="text-sm font-bold text-white">No Escalation Cases Found</div>
          <p className="text-xs text-slate-400">
            Escalated customer chats and severe courier delay cases appear here automatically.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
          {/* Case Queue List */}
          <div className="lg:col-span-4 space-y-2.5 max-h-[680px] overflow-y-auto custom-scrollbar pr-1">
            {cases.map((c) => {
              const active = c.case_id === selectedId;
              return (
                <button
                  key={c.case_id}
                  type="button"
                  onClick={() => setSelectedId(c.case_id)}
                  className={`w-full text-left p-4 rounded-2xl border transition-all space-y-2 ${
                    active
                      ? "bg-teal-950/35 border-teal-500/60 shadow-sm"
                      : "bg-navy-900 border-navy-800 hover:border-navy-700"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-bold text-xs text-teal-300">{c.case_id}</span>
                    <span
                      className={`px-2 py-0.5 rounded-full text-[10px] font-mono uppercase font-bold ${
                        c.priority === "high"
                          ? "bg-red-950 text-red-300 border border-red-500/40"
                          : "bg-navy-950 text-slate-300 border border-navy-800"
                      }`}
                    >
                      {c.priority}
                    </span>
                  </div>
                  <div className="text-xs font-semibold text-white">
                    {c.reason} {c.order_id ? `• ${c.order_id}` : ""}
                  </div>
                  <div className="flex items-center justify-between text-[11px] font-mono text-slate-400">
                    <span>{c.status}</span>
                    <span>{c.customer_id}</span>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Selected Case Workspace */}
          {selectedCase && (
            <div className="lg:col-span-8 min-w-0 bg-navy-900 border border-navy-800 rounded-2xl p-5 space-y-5 text-xs">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 border-b border-navy-800 pb-4">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-base font-bold text-white font-mono">
                      {selectedCase.case_id}
                    </h2>
                    <span className="px-2 py-0.5 rounded bg-navy-950 border border-navy-800 font-mono text-[11px] text-slate-300">
                      Customer {selectedCase.customer_id}
                    </span>
                    {selectedCase.order_id && (
                      <span className="px-2 py-0.5 rounded bg-navy-950 border border-navy-800 font-mono text-[11px] text-teal-300">
                        {selectedCase.order_id}
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] text-slate-400 mt-1">
                    Created {formatISTWithRelative(selectedCase.created_at, true)}
                  </div>
                </div>

                <select
                  aria-label="Update Case Status"
                  value={selectedCase.status}
                  disabled={busy}
                  onChange={(e) => handleStatusChange(e.target.value)}
                  className="px-3 py-1.5 rounded-xl bg-navy-950 border border-navy-700 text-xs text-white focus:outline-none focus:border-teal-500"
                >
                  {STATUS_OPTIONS.map((st) => (
                    <option key={st} value={st}>
                      Status: {st}
                    </option>
                  ))}
                </select>
              </div>

              {/* Deterministic Brief */}
              <div className="bg-navy-950/90 border border-navy-800 rounded-xl p-4 space-y-2">
                <div className="flex items-center space-x-2 text-[10px] font-mono uppercase text-teal-400 font-bold">
                  <FileText className="h-3.5 w-3.5" />
                  <span>Deterministic Case Brief</span>
                </div>
                <pre className="text-[11px] font-mono text-slate-300 overflow-x-auto max-h-40">
                  {JSON.stringify(brief?.facts || selectedCase.brief || {}, null, 2)}
                </pre>
              </div>

              {/* Case Thread */}
              <div className="space-y-2.5 max-h-60 overflow-y-auto custom-scrollbar pr-1">
                {selectedCase.messages.length === 0 ? (
                  <p className="text-slate-400">No case messages yet.</p>
                ) : (
                  selectedCase.messages.map((m) => (
                    <div
                      key={m.message_id}
                      className={`p-3 rounded-xl border ${
                        m.sender_role === "seller"
                          ? "bg-teal-950/30 border-teal-500/30 ml-6"
                          : m.sender_role === "customer"
                            ? "bg-navy-950 border-navy-800 mr-6"
                            : "bg-slate-900/70 border-slate-800 text-slate-300"
                      }`}
                    >
                      <div className="flex items-center justify-between text-[10px] font-mono text-slate-400 mb-1">
                        <span className="uppercase font-bold text-slate-300">
                          {m.sender_role} ({m.sender_id || "system"})
                        </span>
                        <span>{formatISTWithRelative(m.created_at, true)}</span>
                      </div>
                      <p className="text-slate-100 whitespace-pre-wrap">{m.text}</p>
                    </div>
                  ))
                )}
              </div>

              {/* Reply Form */}
              <form onSubmit={handleSendReply} className="space-y-2">
                <label htmlFor="case-reply-input" className="block font-semibold text-slate-300">
                  Reply to Customer (flips session handler to human)
                </label>
                <div className="flex flex-col sm:flex-row gap-2">
                  <input
                    id="case-reply-input"
                    type="text"
                    value={replyText}
                    onChange={(e) => setReplyText(e.target.value)}
                    placeholder="Write a customer-safe response..."
                    className="flex-1 px-3.5 py-2.5 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
                  />
                  <button
                    type="submit"
                    disabled={busy || !replyText.trim()}
                    className="inline-flex items-center justify-center space-x-1.5 px-4 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-700 disabled:bg-navy-800 text-white font-semibold"
                  >
                    <Send className="h-3.5 w-3.5" />
                    <span>Send Reply</span>
                  </button>
                </div>
              </form>

              {/* Assignee + Handback to Agent */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2 border-t border-navy-800">
                <form onSubmit={handleAssign} className="flex items-center gap-2">
                  <input
                    type="text"
                    value={assigneeInput}
                    onChange={(e) => setAssigneeInput(e.target.value)}
                    placeholder="Staff ID (e.g. S002)"
                    aria-label="Assign Case To"
                    className="flex-1 min-w-0 px-3 py-2 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
                  />
                  <button
                    type="submit"
                    disabled={busy || !assigneeInput.trim()}
                    className="inline-flex items-center space-x-1 px-3 py-2 rounded-xl bg-navy-800 hover:bg-navy-700 text-white font-semibold"
                  >
                    <UserCheck className="h-3.5 w-3.5" />
                    <span>Assign</span>
                  </button>
                </form>

                <div className="flex items-center gap-2 min-w-0">
                  <input
                    type="text"
                    value={handbackNotes}
                    onChange={(e) => setHandbackNotes(e.target.value)}
                    placeholder="Optional handback instructions..."
                    aria-label="Handback Instructions"
                    className="flex-1 min-w-0 px-3 py-2 rounded-xl bg-navy-950 border border-navy-800 text-white placeholder-slate-500 focus:outline-none focus:border-teal-500"
                  />
                  <button
                    type="button"
                    disabled={busy}
                    onClick={handleHandback}
                    className="inline-flex items-center space-x-1 px-3 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white font-semibold whitespace-nowrap"
                  >
                    <Bot className="h-3.5 w-3.5" />
                    <span>Hand Back to AI</span>
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
