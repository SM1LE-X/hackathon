export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export type Side = "buy" | "sell" | "unknown";

export type BookLevel = [number, number];

export interface TraderState {
  traderId: string;
  position: number;
  cash: number;
  avgEntryPrice: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalEquity: number;
  netPnl: number;
  equityHistory: number[];
}

export interface TradeEventState {
  id: string;
  timestamp: number;
  price: number;
  qty: number;
  side: Side;
  buyTraderId?: string;
  sellTraderId?: string;
}

export interface LiquidationEventState {
  id: string;
  timestamp: number;
  traderId: string;
  reason: string;
  qty: number;
  side: "buy" | "sell";
}

export interface OrderBookState {
  bids: BookLevel[];
  asks: BookLevel[];
}

export interface DashboardState {
  status: ConnectionStatus;
  endpoint: string;
  orderBook: OrderBookState;
  trades: TradeEventState[];
  liquidations: LiquidationEventState[];
  traders: Record<string, TraderState>;
  reconnectAttempt: number;
  lastError: string | null;
  lastEventAt: number | null;
}
