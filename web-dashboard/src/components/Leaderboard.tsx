import { memo } from "react";
import type { TraderState } from "../types";
import { fmtCash, fmtSigned } from "../utils/format";
import Panel from "./Panel";

interface LeaderboardProps {
  traders: TraderState[];
}

function LeaderboardComponent({ traders }: LeaderboardProps) {
  return (
    <Panel title="Leaderboard (Total Equity)">
      <div className="space-y-2">
        {traders.map((trader, idx) => {
          const pnlClass = trader.netPnl >= 0 ? "text-arena-bid" : "text-arena-ask";
          return (
            <div
              key={trader.traderId}
              className="grid grid-cols-[40px_1fr_auto_auto] items-center gap-3 rounded-lg border border-arena-border/70 bg-black/15 px-3 py-2"
            >
              <div className="text-center font-mono text-sm text-arena-muted">#{idx + 1}</div>
              <div className="truncate font-semibold text-arena-text">{trader.traderId}</div>
              <div className="font-mono text-sm text-arena-text">{fmtCash(trader.totalEquity)}</div>
              <div className={`font-mono text-sm font-semibold ${pnlClass}`}>{fmtSigned(trader.netPnl)}</div>
            </div>
          );
        })}
        {traders.length === 0 ? (
          <div className="rounded-lg border border-dashed border-arena-border px-3 py-4 text-center text-sm text-arena-muted">
            Leaderboard unavailable.
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

const Leaderboard = memo(LeaderboardComponent);
export default Leaderboard;
