import { memo, useMemo } from "react";
import type { BookLevel } from "../types";
import { fmtPrice, fmtQty } from "../utils/format";
import Panel from "./Panel";

interface OrderBookProps {
  bids: BookLevel[];
  asks: BookLevel[];
  depth?: number;
}

function OrderBookComponent({ bids, asks, depth = 10 }: OrderBookProps) {
  const bidRows = useMemo(() => {
    const rows = bids.slice(0, depth);
    while (rows.length < depth) {
      rows.push([0, 0]);
    }
    return rows;
  }, [bids, depth]);
  const askRows = useMemo(() => {
    const rows = asks.slice(0, depth);
    while (rows.length < depth) {
      rows.push([0, 0]);
    }
    return rows;
  }, [asks, depth]);
  const maxQty = useMemo(
    () =>
      Math.max(
        1,
        ...bidRows.map(([, qty]) => qty),
        ...askRows.map(([, qty]) => qty)
      ),
    [bidRows, askRows]
  );

  return (
    <Panel title="Order Book (Depth 10)" className="h-full">
      <div className="grid grid-cols-2 gap-3">
        <BookSide title="Bids" side="bid" levels={bidRows} maxQty={maxQty} />
        <BookSide title="Asks" side="ask" levels={askRows} maxQty={maxQty} />
      </div>
    </Panel>
  );
}
const OrderBook = memo(OrderBookComponent);
export default OrderBook;

function BookSide({
  title,
  side,
  levels,
  maxQty
}: {
  title: string;
  side: "bid" | "ask";
  levels: BookLevel[];
  maxQty: number;
}) {
  const isBid = side === "bid";
  const tone = isBid ? "text-arena-bid" : "text-arena-ask";
  return (
    <div>
      <div className={`mb-2 text-xs font-semibold uppercase tracking-wider ${tone}`}>{title}</div>
      <div className="grid grid-cols-[1fr_auto_auto] gap-1 text-xs text-arena-muted">
        <div>Size</div>
        <div className="text-right">Price</div>
        <div className="text-right">Qty</div>
      </div>
      <div className="mt-2 space-y-1">
        {levels.map(([price, qty], idx) => {
          const isPlaceholder = qty <= 0 || price <= 0;
          const width = `${Math.max(2, Math.round((qty / maxQty) * 100))}%`;
          return (
            <div key={`${side}-${price}-${idx}`} className="relative overflow-hidden rounded-md border border-arena-border/80 bg-black/20">
              <div
                className={`absolute inset-y-0 left-0 ${
                  isBid ? "bg-arena-bid/20" : "bg-arena-ask/20"
                }`}
                style={{ width: isPlaceholder ? "0%" : width }}
              />
              <div className="relative grid grid-cols-[1fr_auto_auto] items-center gap-2 px-2 py-1 font-mono text-[12px]">
                <div className={isPlaceholder ? "text-arena-muted" : isBid ? "text-arena-bid" : "text-arena-ask"}>
                  {isPlaceholder ? "-" : fmtQty(qty)}
                </div>
                <div className={`text-right ${isPlaceholder ? "text-arena-muted" : isBid ? "text-arena-bid" : "text-arena-ask"}`}>
                  {isPlaceholder ? "-" : fmtPrice(price)}
                </div>
                <div className={`text-right ${isPlaceholder ? "text-arena-muted" : "text-arena-text"}`}>
                  {isPlaceholder ? "-" : fmtQty(qty)}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
