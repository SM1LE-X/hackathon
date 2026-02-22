import { fmtPrice } from "../utils/format";

interface HeaderStatsProps {
  status: "connecting" | "connected" | "disconnected";
  endpoint: string;
  bestBid: number | null;
  bestAsk: number | null;
  mid: number | null;
  spread: number | null;
  lastEventAt: number | null;
}

function statusClass(status: HeaderStatsProps["status"]): string {
  if (status === "connected") {
    return "bg-arena-bid/20 text-arena-bid";
  }
  if (status === "connecting") {
    return "bg-arena-warn/20 text-arena-warn";
  }
  return "bg-arena-ask/20 text-arena-ask";
}

export default function HeaderStats({
  status,
  endpoint,
  bestBid,
  bestAsk,
  mid,
  spread,
  lastEventAt
}: HeaderStatsProps) {
  return (
    <header className="rounded-xl border border-arena-border bg-arena-panel2/95 p-4 shadow-glow">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-arena-muted">OpenMarketSim Dashboard</p>
          <p className="mt-1 text-xs text-arena-muted">Feed: {endpoint}</p>
        </div>
        <div className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide ${statusClass(status)}`}>
          {status}
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-5">
        <Metric label="Best Bid" value={fmtPrice(bestBid)} tone="bid" />
        <Metric label="Best Ask" value={fmtPrice(bestAsk)} tone="ask" />
        <Metric label="Mid" value={fmtPrice(mid)} tone="accent" />
        <Metric label="Spread" value={fmtPrice(spread)} tone="muted" />
        <Metric
          label="Last Event"
          value={lastEventAt ? new Date(lastEventAt).toLocaleTimeString() : "-"}
          tone="muted"
        />
      </div>
    </header>
  );
}

function Metric({
  label,
  value,
  tone
}: {
  label: string;
  value: string;
  tone: "bid" | "ask" | "accent" | "muted";
}) {
  const valueClass =
    tone === "bid"
      ? "text-arena-bid"
      : tone === "ask"
        ? "text-arena-ask"
        : tone === "accent"
          ? "text-arena-accent"
          : "text-arena-text";
  return (
    <div className="rounded-lg border border-arena-border/70 bg-black/15 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.16em] text-arena-muted">{label}</div>
      <div className={`mt-1 text-base font-semibold ${valueClass}`}>{value}</div>
    </div>
  );
}
