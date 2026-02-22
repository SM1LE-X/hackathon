import { memo, useEffect, useRef } from "react";
import type { LiquidationEventState } from "../types";
import { fmtClock } from "../utils/format";
import Panel from "./Panel";

interface LiquidationsProps {
  liquidations: LiquidationEventState[];
}

function LiquidationsComponent({ liquidations }: LiquidationsProps) {
  const liquidationRef = useRef<HTMLDivElement | null>(null);
  const isNearBottomRef = useRef(true);

  useEffect(() => {
    const el = liquidationRef.current;
    if (!el) {
      return;
    }

    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    if (isNearBottom || isNearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [liquidations]);

  const handleScroll = () => {
    const el = liquidationRef.current;
    if (!el) {
      return;
    }
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  };

  return (
    <Panel title="Liquidations">
      <div className="h-[420px] flex flex-col">
        <div
          ref={liquidationRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto pr-1"
        >
          <div className="space-y-2">
            {liquidations.map((l) => (
              <div
                key={l.id}
                className="grid grid-cols-[90px_1fr_auto] items-center gap-2 rounded-md border border-arena-border/70 bg-black/20 px-3 py-2 text-xs"
              >
                <div className="font-mono text-arena-muted">{fmtClock(l.timestamp)}</div>
                <div className="truncate text-arena-text">
                  <span className="font-semibold">{l.traderId}</span> {l.reason.replaceAll("_", " ")}
                </div>
                <div className={l.side === "sell" ? "font-mono text-arena-ask" : "font-mono text-arena-bid"}>
                  {l.side} {l.qty}
                </div>
              </div>
            ))}
            {liquidations.length === 0 ? (
              <div className="rounded-md border border-dashed border-arena-border px-3 py-4 text-center text-sm text-arena-muted">
                No liquidations.
              </div>
            ) : null}
            <div className="h-0.5" />
          </div>
        </div>
      </div>
    </Panel>
  );
}

const Liquidations = memo(LiquidationsComponent);
export default Liquidations;
