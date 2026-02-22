import { useEffect, useMemo } from "react";
import BotPerformanceTable from "./components/BotPerformanceTable";
import HeaderStats from "./components/HeaderStats";
import Leaderboard from "./components/Leaderboard";
import Liquidations from "./components/Liquidations";
import OrderBook from "./components/OrderBook";
import RecentTrades from "./components/RecentTrades";
import {
  selectBestAsk,
  selectBestBid,
  selectMid,
  selectSpread,
  useMarketStore
} from "./store/useMarketStore";

export default function App() {
  const connect = useMarketStore((s) => s.connect);
  const disconnect = useMarketStore((s) => s.disconnect);
  const endpoint = useMarketStore((s) => s.endpoint);
  const status = useMarketStore((s) => s.status);
  const orderBook = useMarketStore((s) => s.orderBook);
  const trades = useMarketStore((s) => s.trades);
  const liquidations = useMarketStore((s) => s.liquidations);
  const tradersMap = useMarketStore((s) => s.traders);
  const lastError = useMarketStore((s) => s.lastError);
  const lastEventAt = useMarketStore((s) => s.lastEventAt);

  const bestBid = useMarketStore(selectBestBid);
  const bestAsk = useMarketStore(selectBestAsk);
  const mid = useMarketStore(selectMid);
  const spread = useMarketStore(selectSpread);

  useEffect(() => {
    connect(endpoint);
    return () => {
      disconnect();
    };
  }, [connect, disconnect, endpoint]);

  const traders = useMemo(
    () =>
      Object.values(tradersMap).sort((a, b) => {
        if (b.totalEquity !== a.totalEquity) {
          return b.totalEquity - a.totalEquity;
        }
        return a.traderId.localeCompare(b.traderId);
      }),
    [tradersMap]
  );

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-[1500px] flex-col gap-4 px-4 py-4 lg:px-6">
      <HeaderStats
        status={status}
        endpoint={endpoint}
        bestBid={bestBid}
        bestAsk={bestAsk}
        mid={mid}
        spread={spread}
        lastEventAt={lastEventAt}
      />

      {lastError ? (
        <div className="rounded-lg border border-arena-ask/40 bg-arena-ask/10 px-4 py-2 text-sm text-arena-ask">
          {lastError}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <OrderBook bids={orderBook.bids} asks={orderBook.asks} depth={10} />
        </div>
        <div className="space-y-4">
          <RecentTrades trades={trades} />
          <Liquidations liquidations={liquidations} />
        </div>
      </div>

      <BotPerformanceTable traders={traders} />
      <Leaderboard traders={traders} />
    </div>
  );
}
