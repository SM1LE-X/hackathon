import { memo, useEffect, useRef } from "react";
import type { TradeEventState } from "../types";
import { fmtClock, fmtPrice, fmtQty } from "../utils/format";
import Panel from "./Panel";

interface RecentTradesProps {
  trades: TradeEventState[];
}

function RecentTradesComponent({ trades }: RecentTradesProps) {
  const tradesRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = tradesRef.current;
    if (!el) {
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, [trades]);

  return (
    <Panel title="Recent Trades (Last 20)">
      <div
        ref={tradesRef}
        className="h-[420px] overflow-y-auto rounded-lg border border-arena-border/70"
      >
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-black/25 text-left text-xs uppercase tracking-wider text-arena-muted">
            <tr>
              <th className="px-3 py-2">Time</th>
              <th className="px-3 py-2 text-right">Price</th>
              <th className="px-3 py-2 text-right">Qty</th>
              <th className="px-3 py-2 text-right">Side</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((trade) => {
              const sideClass =
                trade.side === "buy" ? "text-arena-bid" : trade.side === "sell" ? "text-arena-ask" : "text-arena-muted";
              return (
                <tr key={trade.id} className="border-t border-arena-border/40 font-mono text-[13px]">
                  <td className="px-3 py-2 text-arena-muted">{fmtClock(trade.timestamp)}</td>
                  <td className={`px-3 py-2 text-right font-semibold ${sideClass}`}>{fmtPrice(trade.price)}</td>
                  <td className="px-3 py-2 text-right text-arena-text">{fmtQty(trade.qty)}</td>
                  <td className={`px-3 py-2 text-right uppercase ${sideClass}`}>{trade.side}</td>
                </tr>
              );
            })}
            {trades.length === 0 ? (
              <tr>
                <td className="px-3 py-4 text-center text-arena-muted" colSpan={4}>
                  No trades yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

const RecentTrades = memo(RecentTradesComponent);
export default RecentTrades;
