import { create } from "zustand";
import type {
  BookLevel,
  DashboardState,
  LiquidationEventState,
  Side,
  TradeEventState,
  TraderState
} from "../types";

const WS_URL = "ws://127.0.0.1:9010";
const STARTING_CAPITAL = 10_000;
const MAX_TRADES = 20;
const MAX_LIQUIDATIONS = 20;
const MAX_HISTORY = 60;
const RECONNECT_BASE_MS = 900;
const RECONNECT_MAX_MS = 6_000;
const MAX_SEEN_IDS = 2000;

const round4 = (value: number): number => {
  const out = Number(value.toFixed(4));
  return Object.is(out, -0) ? 0 : out;
};

const nowMs = (): number => Date.now();

const sortBids = (levels: BookLevel[]): BookLevel[] =>
  [...levels]
    .filter((l): l is BookLevel => Number.isFinite(l[0]) && Number.isFinite(l[1]) && l[1] > 0)
    .sort((a, b) => b[0] - a[0]);

const sortAsks = (levels: BookLevel[]): BookLevel[] =>
  [...levels]
    .filter((l): l is BookLevel => Number.isFinite(l[0]) && Number.isFinite(l[1]) && l[1] > 0)
    .sort((a, b) => a[0] - b[0]);

const parseLevels = (raw: unknown): BookLevel[] => {
  if (!Array.isArray(raw)) {
    return [];
  }
  const out: BookLevel[] = [];
  for (const level of raw) {
    if (!Array.isArray(level) || level.length !== 2) {
      continue;
    }
    const px = Number(level[0]);
    const qty = Number(level[1]);
    if (!Number.isFinite(px) || !Number.isFinite(qty) || qty <= 0) {
      continue;
    }
    out.push([round4(px), round4(qty)]);
  }
  return out;
};

const inferTradeSide = (payload: Record<string, unknown>, mid: number | null): Side => {
  const side = typeof payload.side === "string" ? payload.side.toLowerCase() : "";
  if (side === "buy" || side === "sell") {
    return side;
  }
  const aggressor = typeof payload.aggressor_side === "string" ? payload.aggressor_side.toLowerCase() : "";
  if (aggressor === "buy" || aggressor === "sell") {
    return aggressor;
  }
  const price = Number(payload.price);
  if (Number.isFinite(price) && mid !== null) {
    return price >= mid ? "buy" : "sell";
  }
  return "unknown";
};

const pushHistory = (history: number[], value: number): number[] => {
  if (history.length > 0 && Math.abs(history[history.length - 1] - value) < 0.0001) {
    return history;
  }
  if (history.length >= MAX_HISTORY) {
    return [...history.slice(1), value];
  }
  return [...history, value];
};

const computeMid = (bids: BookLevel[], asks: BookLevel[]): number | null => {
  if (bids.length === 0 || asks.length === 0) {
    return null;
  }
  return round4((bids[0][0] + asks[0][0]) / 2);
};

const levelsEqual = (a: BookLevel[], b: BookLevel[]): boolean => {
  if (a.length !== b.length) {
    return false;
  }
  for (let i = 0; i < a.length; i += 1) {
    if (a[i][0] !== b[i][0] || a[i][1] !== b[i][1]) {
      return false;
    }
  }
  return true;
};

const recomputeTraders = (
  traders: Record<string, TraderState>,
  mid: number | null
): Record<string, TraderState> => {
  const next: Record<string, TraderState> = {};
  for (const [traderId, trader] of Object.entries(traders)) {
    const unrealized = mid === null ? 0 : round4((mid - trader.avgEntryPrice) * trader.position);
    const total = round4(trader.cash + unrealized);
    const net = round4(total - STARTING_CAPITAL);
    next[traderId] = {
      ...trader,
      unrealizedPnl: unrealized,
      totalEquity: total,
      netPnl: net,
      equityHistory: pushHistory(trader.equityHistory, total)
    };
  }
  return next;
};

type MarketStore = DashboardState & {
  connect: (url?: string) => void;
  disconnect: () => void;
};

let socket: WebSocket | null = null;
let reconnectTimer: number | null = null;
let manualDisconnect = false;
const seenTradeIds = new Set<string>();
const seenLiquidationIds = new Set<string>();
const seenQueue: string[] = [];

const rememberSeen = (bucket: Set<string>, key: string): boolean => {
  if (bucket.has(key)) {
    return false;
  }
  bucket.add(key);
  seenQueue.push(`${bucket === seenTradeIds ? "t" : "l"}:${key}`);
  if (seenQueue.length > MAX_SEEN_IDS) {
    const oldest = seenQueue.shift();
    if (oldest) {
      const [kind, id] = oldest.split(":", 2);
      if (kind === "t") {
        seenTradeIds.delete(id);
      } else {
        seenLiquidationIds.delete(id);
      }
    }
  }
  return true;
};

const clearReconnectTimer = (): void => {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
};

const scheduleReconnect = (connectFn: () => void, attempt: number): void => {
  clearReconnectTimer();
  const delay = Math.min(RECONNECT_BASE_MS * Math.max(1, attempt), RECONNECT_MAX_MS);
  reconnectTimer = window.setTimeout(() => {
    connectFn();
  }, delay);
};

export const useMarketStore = create<MarketStore>((set, get) => {
  const applyPayload = (payload: Record<string, unknown>): void => {
    const type = typeof payload.type === "string" ? payload.type : "";
    if (!type) {
      return;
    }

    if (type === "book_update") {
      const bids = sortBids(parseLevels(payload.bids));
      const asks = sortAsks(parseLevels(payload.asks));
      set((state) => ({
        orderBook:
          levelsEqual(state.orderBook.bids, bids) && levelsEqual(state.orderBook.asks, asks)
            ? state.orderBook
            : { bids, asks },
        traders:
          levelsEqual(state.orderBook.bids, bids) && levelsEqual(state.orderBook.asks, asks)
            ? state.traders
            : recomputeTraders(state.traders, computeMid(bids, asks)),
        lastEventAt: nowMs()
      }));
      return;
    }

    if (type === "trade") {
      const price = Number(payload.price);
      const qty = Number(payload.qty);
      if (!Number.isFinite(price) || !Number.isFinite(qty)) {
        return;
      }
      const state = get();
      const mid = computeMid(state.orderBook.bids, state.orderBook.asks);
      const tradeId = String(
        payload.trade_id ??
          `${Number(payload.timestamp) || nowMs()}-${price}-${qty}-${String(payload.buy_trader_id ?? "b")}-${String(payload.sell_trader_id ?? "s")}`
      );
      if (!rememberSeen(seenTradeIds, tradeId)) {
        return;
      }
      const trade: TradeEventState = {
        id: tradeId,
        timestamp: Number(payload.timestamp) || nowMs(),
        price: round4(price),
        qty: round4(qty),
        side: inferTradeSide(payload, mid),
        buyTraderId: typeof payload.buy_trader_id === "string" ? payload.buy_trader_id : undefined,
        sellTraderId: typeof payload.sell_trader_id === "string" ? payload.sell_trader_id : undefined
      };
      set((current) => ({
        trades: [...current.trades, trade].slice(-MAX_TRADES),
        lastEventAt: trade.timestamp
      }));
      return;
    }

    if (type === "position_update") {
      const traderId = typeof payload.trader_id === "string" ? payload.trader_id : "";
      if (!traderId) {
        return;
      }
      const position = Number(payload.position);
      const cash = Number(payload.cash);
      const realized = Number(payload.realized_pnl);
      const avgEntry = Number(payload.avg_entry_price);
      const serverUnrealized = Number(payload.unrealized_pnl);
      const serverTotalEquity = Number(payload.total_equity);
      if (!Number.isFinite(position) || !Number.isFinite(cash) || !Number.isFinite(realized)) {
        return;
      }
      set((state) => {
        const mid = computeMid(state.orderBook.bids, state.orderBook.asks);
        const avgEntryPrice = Number.isFinite(avgEntry)
          ? round4(avgEntry)
          : round4(state.traders[traderId]?.avgEntryPrice ?? 0);
        const unrealized = Number.isFinite(serverUnrealized)
          ? round4(serverUnrealized)
          : mid === null
            ? 0
            : round4((mid - avgEntryPrice) * position);
        const total = Number.isFinite(serverTotalEquity) ? round4(serverTotalEquity) : round4(cash + unrealized);
        const nextTrader: TraderState = {
          traderId,
          position: round4(position),
          cash: round4(cash),
          avgEntryPrice,
          realizedPnl: round4(realized),
          unrealizedPnl: unrealized,
          totalEquity: total,
          netPnl: round4(total - STARTING_CAPITAL),
          equityHistory: pushHistory(state.traders[traderId]?.equityHistory ?? [], total)
        };
        return {
          traders: {
            ...state.traders,
            [traderId]: nextTrader
          },
          lastEventAt: Number(payload.timestamp) || nowMs()
        };
      });
      return;
    }

    if (type === "liquidation") {
      const traderId = typeof payload.trader_id === "string" ? payload.trader_id : "unknown";
      const liqKey = `${Number(payload.timestamp) || nowMs()}-${traderId}-${String(payload.reason ?? "")}-${String(payload.qty ?? "")}-${String(payload.side ?? "")}`;
      if (!rememberSeen(seenLiquidationIds, liqKey)) {
        return;
      }
      const event: LiquidationEventState = {
        id: liqKey,
        timestamp: Number(payload.timestamp) || nowMs(),
        traderId,
        reason: typeof payload.reason === "string" ? payload.reason : "unspecified",
        qty: Number(payload.qty) || 0,
        side: payload.side === "buy" ? "buy" : "sell"
      };
      set((state) => ({
        liquidations: [...state.liquidations, event].slice(-MAX_LIQUIDATIONS),
        lastEventAt: event.timestamp
      }));
    }
  };

  const connect = (url = get().endpoint): void => {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    clearReconnectTimer();
    manualDisconnect = false;

    set((state) => ({
      status: "connecting",
      endpoint: url,
      reconnectAttempt: state.reconnectAttempt + 1
    }));

    socket = new WebSocket(url);

    socket.onopen = () => {
      set({
        status: "connected",
        lastError: null,
        reconnectAttempt: 0
      });
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      let payload: unknown;
      try {
        payload = JSON.parse(event.data);
      } catch {
        set({ lastError: "Invalid JSON payload from feed." });
        return;
      }
      if (!payload || typeof payload !== "object") {
        return;
      }
      applyPayload(payload as Record<string, unknown>);
    };

    socket.onerror = () => {
      set({ lastError: "WebSocket error from market-data feed." });
    };

    socket.onclose = () => {
      socket = null;
      set({ status: "disconnected" });
      if (!manualDisconnect) {
        const attempt = Math.max(1, get().reconnectAttempt + 1);
        scheduleReconnect(() => connect(url), attempt);
      }
    };
  };

  const disconnect = (): void => {
    manualDisconnect = true;
    clearReconnectTimer();
    if (socket) {
      socket.close();
      socket = null;
    }
    set({ status: "disconnected" });
  };

  return {
    status: "disconnected",
    endpoint: WS_URL,
    orderBook: { bids: [], asks: [] },
    trades: [],
    liquidations: [],
    traders: {},
    reconnectAttempt: 0,
    lastError: null,
    lastEventAt: null,
    connect,
    disconnect
  };
});

export const selectBestBid = (state: DashboardState): number | null =>
  state.orderBook.bids.length > 0 ? state.orderBook.bids[0][0] : null;

export const selectBestAsk = (state: DashboardState): number | null =>
  state.orderBook.asks.length > 0 ? state.orderBook.asks[0][0] : null;

export const selectMid = (state: DashboardState): number | null => {
  const bid = selectBestBid(state);
  const ask = selectBestAsk(state);
  if (bid === null || ask === null) {
    return null;
  }
  return round4((bid + ask) / 2);
};

export const selectSpread = (state: DashboardState): number | null => {
  const bid = selectBestBid(state);
  const ask = selectBestAsk(state);
  if (bid === null || ask === null) {
    return null;
  }
  return round4(Math.max(0, ask - bid));
};
