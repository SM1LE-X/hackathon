import { memo } from "react";
import type { TraderState } from "../types";
import { fmtCash, fmtSigned } from "../utils/format";
import EquitySparkline from "./EquitySparkline";
import Panel from "./Panel";

interface BotPerformanceTableProps {
  traders: TraderState[];
}

function BotPerformanceTableComponent({ traders }: BotPerformanceTableProps) {
  return (
    <Panel title="Bot Performance">
      <div className="overflow-x-auto rounded-lg border border-arena-border/70">
        <table className="w-full min-w-[860px] border-collapse text-sm">
          <thead className="bg-black/25 text-left text-xs uppercase tracking-wider text-arena-muted">
            <tr>
              <th className="px-3 py-2">Trader</th>
              <th className="px-3 py-2 text-right">Position</th>
              <th className="px-3 py-2 text-right">Cash</th>
              <th className="px-3 py-2 text-right">Realized PnL</th>
              <th className="px-3 py-2 text-right">Total Equity</th>
              <th className="px-3 py-2 text-right">Net PnL</th>
              <th className="px-3 py-2 text-center">Equity</th>
            </tr>
          </thead>
          <tbody>
            {traders.map((t) => {
              const pnlClass = t.netPnl >= 0 ? "text-arena-bid" : "text-arena-ask";
              const realizedClass = t.realizedPnl >= 0 ? "text-arena-bid" : "text-arena-ask";
              return (
                <tr key={t.traderId} className="border-t border-arena-border/40 font-mono text-[13px]">
                  <td className="px-3 py-2 font-sans font-semibold text-arena-text">{t.traderId}</td>
                  <td className="px-3 py-2 text-right text-arena-text">{fmtSigned(t.position)}</td>
                  <td className="px-3 py-2 text-right text-arena-text">{fmtCash(t.cash)}</td>
                  <td className={`px-3 py-2 text-right ${realizedClass}`}>{fmtSigned(t.realizedPnl)}</td>
                  <td className="px-3 py-2 text-right text-arena-text">{fmtCash(t.totalEquity)}</td>
                  <td className={`px-3 py-2 text-right font-semibold ${pnlClass}`}>{fmtSigned(t.netPnl)}</td>
                  <td className="px-3 py-2 text-center">
                    <div className="inline-flex justify-center">
                      <EquitySparkline values={t.equityHistory} />
                    </div>
                  </td>
                </tr>
              );
            })}
            {traders.length === 0 ? (
              <tr>
                <td className="px-3 py-5 text-center text-arena-muted" colSpan={7}>
                  Waiting for position updates...
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

const BotPerformanceTable = memo(BotPerformanceTableComponent);
export default BotPerformanceTable;
