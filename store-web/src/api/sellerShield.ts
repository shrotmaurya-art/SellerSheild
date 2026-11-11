import { authFetch } from "./client";

export class SellerApiError extends Error {
  public readonly status: number;
  public readonly code: string;
  public readonly details?: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    code = "api_error",
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "SellerApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function parseResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const errJson = await res.json().catch(() => null);
    const errObj = errJson?.error;
    const msg =
      typeof errObj?.message === "string"
        ? errObj.message
        : `Request failed with status ${res.status}`;
    const code = typeof errObj?.code === "string" ? errObj.code : "http_error";
    const details =
      errObj?.details && typeof errObj.details === "object"
        ? (errObj.details as Record<string, unknown>)
        : undefined;
    throw new SellerApiError(msg, res.status, code, details);
  }
  return (await res.json()) as T;
}

export interface MonitorRunItem {
  run_id: string;
  checked_orders_count: number;
  anomalies_detected: number;
  actions_taken: string[];
  created_at: string;
}

export interface SellerOverview {
  pending_approvals_count: number;
  pending_approval_ids: string[];
  overdue_approvals_count: number;
  overdue_approval_ids: string[];
  open_cases_count: number;
  open_cases_by_priority: Record<string, number>;
  open_case_ids_by_priority: Record<string, string[]>;
  failed_notifications_count: number;
  failed_notification_ids: string[];
  last_monitor_run: MonitorRunItem | null;
  today_conversations_count: number;
  today_conversation_ids: string[];
  today_proposals_count: number;
  today_proposal_ids: string[];
  today_executions_count: number;
  today_execution_ids: string[];
  auto_resolved_rate: number;
  active_policy_version: string;
}

export interface ComputedAmount {
  items_paise: number;
  shipping_refund_paise: number;
  fee_paise: number;
  total_paise: number;
  capped: boolean;
}

export interface SellerApproval {
  approval_id: string;
  proposal_id: string;
  store_id: string;
  customer_id: string;
  order_id: string;
  return_id: string | null;
  refund_id: string | null;
  requested_amount_paise: number;
  amount_paise: number;
  required_role: string;
  policy_version: string;
  reason: string;
  status:
    | "pending"
    | "approving"
    | "approved"
    | "rejected"
    | "expired"
    | "needs_revalidation"
    | "withdrawn";
  expected_version: number;
  risk_flags: string[];
  store_authorization_ref: string | null;
  created_at: string;
  due_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  last_error: string | null;
  method: string;
  resolution: string;
  reason_code: string | null;
  order: Record<string, unknown> | null;
  computed_amount: ComputedAmount | null;
  rule_results: Record<string, unknown>[];
  risk: Record<string, unknown> | null;
  unverified_customer_statement: {
    label: string;
    text: string;
    truncated: boolean;
  } | null;
  evidence_ids: string[];
  recommendation: "approve" | "review";
  recommendation_reason: string | null;
  similar_past_decisions: {
    approved_count: number;
    rejected_count: number;
    expired_count: number;
    withdrawn_count: number;
    pending_count: number;
    same_order_count: number;
  } | null;
  revalidation_diff: Record<string, unknown>[] | null;
}

export interface SellerApprovalListResponse {
  approvals: SellerApproval[];
  total: number;
  limit: number;
  offset: number;
}

export interface CaseMessage {
  message_id: string;
  sender_role: string;
  sender_id: string;
  text: string;
  created_at: string;
}

export interface SellerCase {
  case_id: string;
  customer_id: string;
  order_id: string | null;
  status: string;
  reason: string;
  priority: string;
  assigned_to: string | null;
  evidence_ids: string[] | null;
  brief: Record<string, unknown> | null;
  messages: CaseMessage[];
  created_at: string;
  updated_at: string | null;
}

export interface CaseBrief {
  case_id: string;
  customer_id: string;
  order_id: string | null;
  reason: string;
  facts: Record<string, unknown>;
  llm_summary: string | null;
  created_at: string;
}

export interface NotificationItem {
  notification_id: string;
  channel: string;
  recipient: string;
  subject: string | null;
  status: string;
  attempts: number;
  created_at: string;
}

export interface AuditEventItem {
  seq: number;
  event_id: string;
  event_type: string;
  actor: string;
  request_id: string | null;
  hash: string;
  prev_hash: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface AuditVerifyResult {
  verified: boolean;
  total_events: number;
  head_hash: string | null;
  broken_seq: number | null;
  error: string | null;
}

export interface DecisionTrace {
  request_id: string;
  timestamp: string;
  llm_calls: Record<string, unknown>[];
  tool_calls: Record<string, unknown>[];
  policy_decisions: Record<string, unknown>[];
  audit_events: {
    seq: number;
    event_id: string;
    event_type: string;
    stage: string;
    actor: string;
    action: string;
    outcome: string;
    resource_type: string;
    resource_id: string;
    details: Record<string, unknown>;
    created_at: string;
  }[];
}

export async function fetchSellerOverview(): Promise<SellerOverview> {
  const res = await authFetch("/agent/v1/seller/overview");
  return parseResponse<SellerOverview>(res);
}

export async function fetchSellerApprovals(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<SellerApprovalListResponse> {
  const qs = new URLSearchParams();
  if (params?.status && params.status !== "all") qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await authFetch(`/agent/v1/seller/approvals${suffix}`);
  return parseResponse<SellerApprovalListResponse>(res);
}

export async function decideSellerApproval(
  approvalId: string,
  payload: {
    decision: "approve" | "reject";
    reason?: string | null;
    expected_version: number;
  },
): Promise<SellerApproval> {
  const idemKey = `seller-dec-${approvalId}-v${payload.expected_version}-${Date.now()}`;
  const res = await authFetch(`/agent/v1/seller/approvals/${approvalId}/decision`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idemKey,
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<SellerApproval>(res);
}

export async function fetchSellerCases(params?: {
  status?: string;
  priority?: string;
}): Promise<{ cases: SellerCase[] }> {
  const qs = new URLSearchParams();
  if (params?.status && params.status !== "all") qs.set("status", params.status);
  if (params?.priority && params.priority !== "all") qs.set("priority", params.priority);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await authFetch(`/agent/v1/seller/cases${suffix}`);
  return parseResponse<{ cases: SellerCase[] }>(res);
}

export async function fetchSellerCaseBrief(caseId: string): Promise<CaseBrief> {
  const res = await authFetch(`/agent/v1/seller/cases/${caseId}/brief`);
  return parseResponse<CaseBrief>(res);
}

export async function sendSellerCaseMessage(caseId: string, text: string): Promise<unknown> {
  const res = await authFetch(`/agent/v1/seller/cases/${caseId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return parseResponse<unknown>(res);
}

export async function updateSellerCaseStatus(
  caseId: string,
  status: string,
  resolutionNote?: string,
): Promise<SellerCase> {
  const res = await authFetch(`/agent/v1/seller/cases/${caseId}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status,
      ...(resolutionNote?.trim() ? { resolution_note: resolutionNote.trim() } : {}),
    }),
  });
  return parseResponse<SellerCase>(res);
}

export async function assignSellerCase(caseId: string, assignedTo: string): Promise<unknown> {
  const res = await authFetch(`/agent/v1/seller/cases/${caseId}/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ assigned_to: assignedTo }),
  });
  return parseResponse<unknown>(res);
}

export async function handbackSellerCase(caseId: string, instructions?: string): Promise<unknown> {
  const res = await authFetch(`/agent/v1/seller/cases/${caseId}/handback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instructions: instructions?.trim() || null }),
  });
  return parseResponse<unknown>(res);
}

export async function triggerMonitorRun(): Promise<MonitorRunItem> {
  const res = await authFetch("/agent/v1/seller/monitor/run", {
    method: "POST",
    headers: {
      "Idempotency-Key": `seller-mon-${Date.now()}`,
    },
  });
  return parseResponse<MonitorRunItem>(res);
}

export async function fetchMonitorRuns(): Promise<{ runs: MonitorRunItem[] }> {
  const res = await authFetch("/agent/v1/seller/monitor/runs");
  return parseResponse<{ runs: MonitorRunItem[] }>(res);
}

export async function fetchSellerNotifications(
  status?: string,
): Promise<{ notifications: NotificationItem[] }> {
  const qs = status && status !== "all" ? `?status=${encodeURIComponent(status)}` : "";
  const res = await authFetch(`/agent/v1/seller/notifications${qs}`);
  return parseResponse<{ notifications: NotificationItem[] }>(res);
}

export async function retrySellerNotification(id: string): Promise<NotificationItem> {
  const res = await authFetch(`/agent/v1/seller/notifications/${id}/retry`, {
    method: "POST",
  });
  return parseResponse<NotificationItem>(res);
}

export async function fetchSellerAudit(params?: {
  limit?: number;
  cursor?: string | null;
  request_id?: string;
  resource_id?: string;
  event_type?: string;
}): Promise<{ events: AuditEventItem[]; next_cursor: string | null }> {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.cursor) qs.set("cursor", params.cursor);
  if (params?.request_id?.trim()) qs.set("request_id", params.request_id.trim());
  if (params?.resource_id?.trim()) qs.set("resource_id", params.resource_id.trim());
  if (params?.event_type?.trim() && params.event_type !== "all") {
    qs.set("event_type", params.event_type.trim());
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await authFetch(`/agent/v1/seller/audit${suffix}`);
  return parseResponse<{ events: AuditEventItem[]; next_cursor: string | null }>(res);
}

export async function verifySellerAudit(): Promise<AuditVerifyResult> {
  const res = await authFetch("/agent/v1/seller/audit/verify");
  return parseResponse<AuditVerifyResult>(res);
}

export async function fetchDecisionTrace(requestId: string): Promise<DecisionTrace> {
  const res = await authFetch(`/agent/v1/seller/traces/${encodeURIComponent(requestId)}`);
  return parseResponse<DecisionTrace>(res);
}
