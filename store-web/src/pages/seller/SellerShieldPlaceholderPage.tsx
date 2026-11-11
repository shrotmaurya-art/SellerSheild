import { Bot, Sparkles, ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";

export function SellerShieldPlaceholderPage() {
  return (
    <div className="max-w-2xl mx-auto py-16 px-4 text-center">
      <div className="bg-navy-900 border border-teal-500/30 rounded-2xl p-8 sm:p-10 shadow-xl space-y-4">
        <div className="h-16 w-16 rounded-2xl bg-teal-500/10 text-teal-400 border border-teal-500/30 flex items-center justify-center mx-auto shadow-inner">
          <Bot className="h-8 w-8" />
        </div>

        <div className="space-y-1">
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-teal-900/60 text-teal-300 border border-teal-500/30">
            <Sparkles className="h-3 w-3" />
            <span>Document 2 Component</span>
          </span>
          <h2 className="text-xl font-bold text-white pt-2">SellerShield Agent Console</h2>
        </div>

        <p className="text-sm font-semibold text-teal-300">
          Available after the agent is built (Document 2)
        </p>

        <p className="text-xs text-slate-400 max-w-md mx-auto leading-relaxed">
          The autonomous SellerShield customer support agent, policy studio, decision trace drawer,
          and approval queues will connect to this console in Document 2.
        </p>

        <div className="pt-4">
          <Link
            to="/seller"
            className="inline-flex items-center space-x-1.5 px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-700 text-white text-xs font-semibold shadow-sm transition-colors"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span>Return to Dashboard</span>
          </Link>
        </div>
      </div>
    </div>
  );
}
