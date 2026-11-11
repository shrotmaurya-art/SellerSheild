import React from "react";
import { Clock, UserCheck } from "lucide-react";

export interface CaseCreatedCardProps {
  caseId: string;
  orderId?: string;
  priority?: "low" | "medium" | "high" | "urgent" | string;
  status?: string;
  subject: string;
  expectedResponse?: string;
  assignedTeam?: string;
}

export const CaseCreatedCard: React.FC<CaseCreatedCardProps> = ({
  caseId,
  orderId,
  priority = "medium",
  status = "Open",
  subject,
  expectedResponse = "Within 24 hours",
  assignedTeam = "Seller Support Specialist",
}) => {
  const getPriorityBadge = (p: string) => {
    switch (p.toLowerCase()) {
      case "urgent":
      case "high":
        return "bg-red-50 text-red-700 border-red-200";
      case "medium":
        return "bg-amber-50 text-amber-700 border-amber-200";
      default:
        return "bg-slate-100 text-slate-700 border-slate-200";
    }
  };

  return (
    <div
      data-testid="case-created-card"
      className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden text-xs text-slate-800 max-w-sm w-full my-2"
    >
      {/* Header */}
      <div className="bg-navy-900 text-white p-3.5 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="h-7 w-7 rounded-lg bg-teal-500/20 text-teal-300 flex items-center justify-center border border-teal-500/30">
            <UserCheck className="h-4 w-4" />
          </div>
          <div>
            <div className="font-bold text-sm leading-tight">Escalation Case Created</div>
            <div className="font-mono text-[10px] text-teal-300 mt-0.5">{caseId}</div>
          </div>
        </div>

        <span
          className={`capitalize px-2 py-0.5 rounded-full text-[10px] font-semibold border ${getPriorityBadge(
            priority
          )}`}
        >
          {priority} Priority
        </span>
      </div>

      {/* Content */}
      <div className="p-3.5 space-y-3">
        <div className="space-y-1">
          <span className="text-[10px] uppercase font-bold text-slate-400">Subject</span>
          <p className="font-semibold text-slate-900 text-xs leading-snug">{subject}</p>
          {orderId && (
            <p className="text-[11px] text-slate-500">
              Linked Order: <span className="font-mono font-medium text-navy-900">{orderId}</span>
            </p>
          )}
        </div>

        <div className="bg-slate-50 rounded-lg p-2.5 border border-slate-100 space-y-1.5 text-[11px]">
          <div className="flex justify-between items-center">
            <span className="text-slate-500">Status</span>
            <span className="font-semibold text-teal-700 capitalize">{status}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-slate-500">Assigned Team</span>
            <span className="font-medium text-slate-800">{assignedTeam}</span>
          </div>
        </div>

        <div className="flex items-center space-x-2 text-slate-600 bg-amber-50/60 border border-amber-200/60 p-2.5 rounded-lg text-[11px]">
          <Clock className="h-4 w-4 text-amber-600 flex-shrink-0" />
          <span>
            Expected response: <strong className="text-slate-800">{expectedResponse}</strong>. A human agent will review your issue.
          </span>
        </div>
      </div>
    </div>
  );
};
