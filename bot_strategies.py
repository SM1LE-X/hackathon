# File: bot_strategies.py

from __future__ import annotations

import importlib
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from message_schemas import OrderRequest, OrderType, Side, round4


@dataclass(frozen=True, slots=True)
class StrategyContext:
    trader_id: str
    best_bid: float | None
    best_ask: float | None
    mid_price: float
    spread: float | None
    timestamp: int
    position: int = 0
    cash: float = 10_000.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 10_000.0
    maintenance_margin: float = 0.0
    last_rejection_reason: str | None = None
    last_rejection_ts: int = 0
    last_liquidation_ts: int = 0


class Strategy(Protocol):
    """Strategy interface used by bot_client."""

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        """
        Return the next order or None to skip this decision cycle.
        """


def parse_strategy_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise ValueError(f"invalid strategy param '{raw}', expected key=value")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid strategy param '{raw}', empty key")
        params[key] = value.strip()
    return params


class RandomStrategy:
    """Baseline random mixed-maker/taker strategy."""

    def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
        self._trader_id = trader_id
        self._rng = rng
        self._min_qty = max(1, int(params.get("min_qty", "1")))
        self._max_qty = max(self._min_qty, int(params.get("max_qty", "5")))
        self._market_prob = min(1.0, max(0.0, float(params.get("market_prob", "0.15"))))

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        side = Side.BUY if self._rng.random() < 0.5 else Side.SELL
        qty = self._rng.randint(self._min_qty, self._max_qty)

        use_market = (
            self._rng.random() < self._market_prob
            and context.best_bid is not None
            and context.best_ask is not None
        )
        if use_market:
            return OrderRequest(
                trader_id=self._trader_id,
                side=side,
                qty=qty,
                order_type=OrderType.MARKET,
                price=None,
                client_order_id=f"{self._trader_id}-rnd-mkt-{self._rng.randint(1000, 9999)}",
            )

        if side == Side.BUY:
            if context.best_bid is None:
                base = context.mid_price - 0.10
            elif context.best_ask is not None and self._rng.random() < 0.35:
                base = context.best_ask
            else:
                base = context.best_bid
            price = round4(max(0.01, base - self._rng.choice([0.0, 0.05, 0.10])))
        else:
            if context.best_ask is None:
                base = context.mid_price + 0.10
            elif context.best_bid is not None and self._rng.random() < 0.35:
                base = context.best_bid
            else:
                base = context.best_ask
            price = round4(max(0.01, base + self._rng.choice([0.0, 0.05, 0.10])))

        return OrderRequest(
            trader_id=self._trader_id,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            client_order_id=f"{self._trader_id}-rnd-lmt-{self._rng.randint(1000, 9999)}",
        )


class MakerStrategy:
    """
    Risk-aware layered maker designed to survive margin pressure.

    Features:
    - Dynamic quote sizing from available equity and leverage budget.
    - Inventory skew (long -> lean ask, short -> lean bid).
    - Pre-liquidation guard (size cut + temporary pause).
    - Incremental depth maintenance (no full cancel/replace each tick).
    - Minimum depth target per side when equity allows.
    """

    def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
        self._trader_id = trader_id
        self._rng = rng
        self._min_levels = max(1, int(params.get("min_levels", "5")))
        self._max_levels = max(self._min_levels, int(params.get("max_levels", "10")))
        self._levels = self._max_levels
        self._tick = max(0.01, float(params.get("tick", "0.05")))
        self._default_mid = max(0.01, float(params.get("default_mid", "100.0")))
        self._min_spread = max(self._tick, float(params.get("min_spread", f"{self._tick:.4f}")))
        self._mid_move_ticks = max(1, int(params.get("mid_move_ticks", "1")))
        self._leverage = max(0.1, float(params.get("leverage", "2.5")))
        self._maint_margin_rate = max(0.001, float(params.get("maintenance_margin_rate", "0.10")))
        self._base_qty_floor = max(1, int(params.get("min_qty", "1")))
        self._inventory_skew_limit = max(1, int(params.get("inventory_skew_limit", "20")))
        self._pause_seconds = max(0.2, float(params.get("pause_seconds", "2.0")))

        self._tick_index = 0
        self._anchor_mid: float | None = None
        self._quote_epoch = 0
        self._emit_seq = 0
        self._resting_bids: dict[float, int] = {}
        self._resting_asks: dict[float, int] = {}
        self._quote_plan: deque[tuple[Side, float, int]] = deque()
        self._paused_until_mono = 0.0
        self._recovery_until_mono = 0.0
        self._last_liquidation_ts = 0

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        self._tick_index += 1
        now_mono = time.monotonic()
        now_ms = int(now_mono * 1000)

        self._on_liquidation_if_seen(context, now_mono)

        if self._should_pause(context, now_ms):
            self._paused_until_mono = max(self._paused_until_mono, now_mono + self._pause_seconds)
            return None
        if now_mono < self._paused_until_mono:
            return None

        mid = self._resolve_mid(context)
        size_scale = self._risk_size_scale(context, mid, now_mono)
        self._reconcile_state(context, mid)
        self._top_up_missing_levels(context, mid, size_scale)
        if not self._quote_plan:
            return None

        side, price, qty = self._quote_plan.popleft()
        self._emit_seq += 1
        side_tag = "bid" if side == Side.BUY else "ask"
        client_order_id = f"{self._trader_id}-mak-e{self._quote_epoch}-{side_tag}-n{self._emit_seq}"

        # Mark as intended resting immediately to prevent duplicate spam.
        if side == Side.BUY:
            self._resting_bids[price] = qty
        else:
            self._resting_asks[price] = qty

        return OrderRequest(
            trader_id=self._trader_id,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            client_order_id=client_order_id,
        )

    def _resolve_mid(self, context: StrategyContext) -> float:
        if context.best_bid is not None and context.best_ask is not None:
            return round4((context.best_bid + context.best_ask) / 2.0)
        if context.mid_price > 0:
            return round4(context.mid_price)
        return round4(self._default_mid)

    def _on_liquidation_if_seen(self, context: StrategyContext, now_mono: float) -> None:
        if context.last_liquidation_ts <= 0:
            return
        if context.last_liquidation_ts <= self._last_liquidation_ts:
            return

        # Post-liquidation recovery mode: clear stale tracked levels and resume with smaller sizing.
        self._last_liquidation_ts = context.last_liquidation_ts
        self._quote_epoch += 1
        self._resting_bids.clear()
        self._resting_asks.clear()
        self._quote_plan.clear()
        self._anchor_mid = None
        self._recovery_until_mono = max(self._recovery_until_mono, now_mono + self._pause_seconds)

    def _should_pause(self, context: StrategyContext, now_ms: int) -> bool:
        equity = max(0.0, float(context.total_equity))
        maintenance = self._maintenance_margin(context, self._resolve_mid(context))
        if maintenance > 0 and equity < (1.05 * maintenance):
            return True

        reason = (context.last_rejection_reason or "").lower()
        if reason in {"account_frozen", "maintenance_margin_breach", "initial_margin_insufficient"}:
            if context.last_rejection_ts > 0 and (now_ms - context.last_rejection_ts) <= int(self._pause_seconds * 1000):
                return True
        return False

    def _risk_size_scale(self, context: StrategyContext, mid: float, now_mono: float) -> float:
        equity = max(0.0, float(context.total_equity))
        maintenance = self._maintenance_margin(context, mid)
        scale = 1.0

        if maintenance > 0 and equity < (1.2 * maintenance):
            scale *= 0.30
        if now_mono < self._recovery_until_mono:
            scale *= 0.50

        return max(0.05, min(1.0, scale))

    def _maintenance_margin(self, context: StrategyContext, mid: float) -> float:
        if context.maintenance_margin > 0:
            return float(context.maintenance_margin)
        return round4(abs(context.position * mid) * self._maint_margin_rate)

    def _reconcile_state(self, context: StrategyContext, mid: float) -> None:
        moved_significantly = self._anchor_mid is None or abs(mid - self._anchor_mid) > (self._mid_move_ticks * self._tick)
        if moved_significantly:
            # Significant mid move -> reset tracked ladder and rebuild incrementally.
            self._quote_epoch += 1
            self._anchor_mid = mid
            self._resting_bids.clear()
            self._resting_asks.clear()
            self._quote_plan.clear()

        # Remove invalid/crossed tracked levels.
        if context.best_ask is not None:
            self._resting_bids = {
                px: qty for px, qty in self._resting_bids.items() if px > 0 and px < context.best_ask and qty > 0
            }
        else:
            self._resting_bids = {px: qty for px, qty in self._resting_bids.items() if px > 0 and qty > 0}

        if context.best_bid is not None:
            self._resting_asks = {
                px: qty for px, qty in self._resting_asks.items() if px > context.best_bid and qty > 0
            }

        # Drop stale/duplicate pending quotes.
        cleaned: deque[tuple[Side, float, int]] = deque()
        seen: set[tuple[Side, float]] = set()
        for side, price, qty in self._quote_plan:
            if qty < 1:
                continue
            if side == Side.BUY:
                if price <= 0:
                    continue
                if context.best_ask is not None and price >= context.best_ask:
                    continue
                if price in self._resting_bids:
                    continue
            else:
                if context.best_bid is not None and price <= context.best_bid:
                    continue
                if price in self._resting_asks:
                    continue
            key = (side, price)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append((side, price, qty))
        self._quote_plan = cleaned

    def _top_up_missing_levels(self, context: StrategyContext, mid: float, size_scale: float) -> None:
        level_target = self._target_depth(context, mid)
        if level_target <= 0:
            return

        target_bids, target_asks = self._target_levels(context, mid, level_target)

        pending_bids = {px: qty for side, px, qty in self._quote_plan if side == Side.BUY}
        pending_asks = {px: qty for side, px, qty in self._quote_plan if side == Side.SELL}

        bid_depth = len(self._resting_bids) + len(pending_bids)
        ask_depth = len(self._resting_asks) + len(pending_asks)

        bid_mult, ask_mult = self._inventory_size_multipliers(context.position)

        # Keep adding missing prices in strict level order without skipping.
        for level_idx, price in enumerate(target_bids):
            if bid_depth >= self._max_levels:
                break
            if price in self._resting_bids or price in pending_bids:
                continue
            qty = self._size_for_level(
                side=Side.BUY,
                level_index=level_idx,
                price=price,
                context=context,
                level_target=level_target,
                side_mult=bid_mult * size_scale,
            )
            if qty < 1:
                break
            self._quote_plan.append((Side.BUY, price, qty))
            pending_bids[price] = qty
            bid_depth += 1
            if bid_depth >= min(self._min_levels, level_target):
                break

        for level_idx, price in enumerate(target_asks):
            if ask_depth >= self._max_levels:
                break
            if price in self._resting_asks or price in pending_asks:
                continue
            qty = self._size_for_level(
                side=Side.SELL,
                level_index=level_idx,
                price=price,
                context=context,
                level_target=level_target,
                side_mult=ask_mult * size_scale,
            )
            if qty < 1:
                break
            self._quote_plan.append((Side.SELL, price, qty))
            pending_asks[price] = qty
            ask_depth += 1
            if ask_depth >= min(self._min_levels, level_target):
                break

        # If we are healthy, allow topping up to max (not required each tick).
        if bid_depth >= min(self._min_levels, level_target):
            for level_idx, price in enumerate(target_bids):
                if bid_depth >= self._max_levels:
                    break
                if price in self._resting_bids or price in pending_bids:
                    continue
                qty = self._size_for_level(
                    side=Side.BUY,
                    level_index=level_idx,
                    price=price,
                    context=context,
                    level_target=level_target,
                    side_mult=bid_mult * size_scale,
                )
                if qty < 1:
                    break
                self._quote_plan.append((Side.BUY, price, qty))
                pending_bids[price] = qty
                bid_depth += 1

        if ask_depth >= min(self._min_levels, level_target):
            for level_idx, price in enumerate(target_asks):
                if ask_depth >= self._max_levels:
                    break
                if price in self._resting_asks or price in pending_asks:
                    continue
                qty = self._size_for_level(
                    side=Side.SELL,
                    level_index=level_idx,
                    price=price,
                    context=context,
                    level_target=level_target,
                    side_mult=ask_mult * size_scale,
                )
                if qty < 1:
                    break
                self._quote_plan.append((Side.SELL, price, qty))
                pending_asks[price] = qty
                ask_depth += 1

    def _target_depth(self, context: StrategyContext, mid: float) -> int:
        equity = max(0.0, float(context.total_equity))
        max_notional = equity * self._leverage
        if max_notional <= 0 or mid <= 0:
            return 0

        # Rough affordability check for both sides with at least one lot per level.
        affordable = int(max_notional / (2.0 * mid))
        affordable = max(0, min(self._max_levels, affordable))

        if affordable >= self._min_levels:
            return min(self._max_levels, affordable)
        return max(1, affordable)

    def _inventory_size_multipliers(self, position: int) -> tuple[float, float]:
        if position == 0:
            return 1.0, 1.0
        skew = min(1.0, abs(position) / float(self._inventory_skew_limit))
        if position > 0:
            # Long: de-risk bids, lean on asks.
            return max(0.15, 1.0 - (0.75 * skew)), min(1.8, 1.0 + (0.65 * skew))
        # Short: de-risk asks, lean on bids.
        return min(1.8, 1.0 + (0.65 * skew)), max(0.15, 1.0 - (0.75 * skew))

    def _inventory_price_skew_steps(self, position: int) -> tuple[int, int]:
        if position == 0:
            return 0, 0
        skew = min(1.0, abs(position) / float(self._inventory_skew_limit))
        steps = 1 + int(2 * skew)
        if position > 0:
            # Long: bids less aggressive (farther), asks more aggressive (closer).
            return steps, steps
        # Short: bids more aggressive (closer), asks less aggressive (farther).
        return -steps, -steps

    def _target_levels(self, context: StrategyContext, mid: float, level_target: int) -> tuple[list[float], list[float]]:
        bid_shift, ask_shift = self._inventory_price_skew_steps(context.position)

        bids: list[float] = []
        asks: list[float] = []
        for i in range(level_target):
            base_ticks = i + 1
            if context.position > 0:
                bid_ticks = base_ticks + max(0, bid_shift)
                ask_ticks = max(1, base_ticks - max(0, ask_shift))
            elif context.position < 0:
                bid_ticks = max(1, base_ticks - max(0, -bid_shift))
                ask_ticks = base_ticks + max(0, -ask_shift)
            else:
                bid_ticks = base_ticks
                ask_ticks = base_ticks

            bid_price = self._snap_down(mid - (self._tick * bid_ticks))
            ask_price = self._snap_up(mid + (self._tick * ask_ticks))

            if bid_price <= 0:
                break
            if context.best_ask is not None and bid_price >= context.best_ask:
                bid_price = self._snap_down(context.best_ask - self._tick)
            if context.best_bid is not None and ask_price <= context.best_bid:
                ask_price = self._snap_up(context.best_bid + self._tick)

            if ask_price <= bid_price or (ask_price - bid_price) < self._min_spread:
                ask_price = self._snap_up(bid_price + max(self._min_spread, self._tick))

            if bid_price > 0 and (context.best_ask is None or bid_price < context.best_ask):
                bids.append(round4(bid_price))
            if context.best_bid is None or ask_price > context.best_bid:
                asks.append(round4(ask_price))

        return list(dict.fromkeys(bids)), list(dict.fromkeys(asks))

    def _size_for_level(
        self,
        *,
        side: Side,
        level_index: int,
        price: float,
        context: StrategyContext,
        level_target: int,
        side_mult: float,
    ) -> int:
        equity = max(0.0, float(context.total_equity))
        max_notional = equity * self._leverage
        if max_notional <= 0 or price <= 0 or level_target <= 0:
            return 0

        side_budget = max_notional / 2.0
        per_level_notional = side_budget / float(level_target)

        # Slight decay for deeper levels to reduce tail exposure.
        depth_decay = max(0.55, 1.0 - (0.07 * level_index))
        adjusted_notional = per_level_notional * max(0.05, side_mult) * depth_decay

        qty = int(adjusted_notional / price)
        if qty < self._base_qty_floor:
            if (self._base_qty_floor * price) <= adjusted_notional:
                qty = self._base_qty_floor
            else:
                return 0

        # Hard cap by available side budget.
        max_qty_for_level = int(side_budget / price)
        qty = min(qty, max_qty_for_level)
        return max(0, qty)

    def _snap_down(self, value: float) -> float:
        ticks = int(value / self._tick)
        return round4(max(0.01, ticks * self._tick))

    def _snap_up(self, value: float) -> float:
        ticks = int((value + self._tick - 1e-12) / self._tick)
        return round4(max(0.01, ticks * self._tick))


class TakerStrategy:
    """Aggressive strategy that crosses spread frequently."""

    def __init__(self, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> None:
        self._trader_id = trader_id
        self._rng = rng
        self._qty = max(1, int(params.get("qty", "3")))
        self._market_prob = min(1.0, max(0.0, float(params.get("market_prob", "0.70"))))
        self._min_ticks_between_orders = max(1, int(params.get("min_ticks_between_orders", "2")))
        self._side_jitter = min(0.45, max(0.0, float(params.get("side_jitter", "0.10"))))
        self._tick_index = 0
        self._last_order_tick = -10_000

    def next_order(self, context: StrategyContext) -> OrderRequest | None:
        self._tick_index += 1
        if (self._tick_index - self._last_order_tick) < self._min_ticks_between_orders:
            # Throttle order frequency to reduce churn and avoid over-aggressive spam.
            return None

        if context.best_bid is None and context.best_ask is None:
            return None

        # Slightly jitter side probability around 50/50 to avoid deterministic oscillation.
        buy_prob = 0.5 + self._rng.uniform(-self._side_jitter, self._side_jitter)
        side = Side.BUY if self._rng.random() < buy_prob else Side.SELL

        can_send_market = (side == Side.BUY and context.best_ask is not None) or (
            side == Side.SELL and context.best_bid is not None
        )
        if self._rng.random() < self._market_prob:
            # Only send market orders when opposite-side liquidity exists.
            if not can_send_market:
                return None
            self._last_order_tick = self._tick_index
            return OrderRequest(
                trader_id=self._trader_id,
                side=side,
                qty=self._qty,
                order_type=OrderType.MARKET,
                price=None,
                client_order_id=f"{self._trader_id}-tak-mkt-{self._rng.randint(1000, 9999)}",
            )

        if side == Side.BUY:
            price = context.best_ask if context.best_ask is not None else context.mid_price
        else:
            price = context.best_bid if context.best_bid is not None else context.mid_price
        self._last_order_tick = self._tick_index
        return OrderRequest(
            trader_id=self._trader_id,
            side=side,
            qty=self._qty,
            order_type=OrderType.LIMIT,
            price=round4(max(0.01, price)),
            client_order_id=f"{self._trader_id}-tak-lmt-{self._rng.randint(1000, 9999)}",
        )


BUILTIN_STRATEGIES: dict[str, Callable[[str, random.Random, dict[str, str]], Strategy]] = {
    "random": lambda trader_id, rng, params: RandomStrategy(trader_id=trader_id, rng=rng, params=params),
    "maker": lambda trader_id, rng, params: MakerStrategy(trader_id=trader_id, rng=rng, params=params),
    "taker": lambda trader_id, rng, params: TakerStrategy(trader_id=trader_id, rng=rng, params=params),
}


def load_strategy(strategy_spec: str, *, trader_id: str, rng: random.Random, params: dict[str, str]) -> Strategy:
    """
    Load built-in strategy by name or custom strategy by module path.

    `strategy_spec` formats:
    - built-in: `random`, `maker`, `taker`
    - custom: `your_module:YourStrategyClass`

    Custom class must provide:
    `__init__(self, trader_id: str, rng: random.Random, params: dict[str, str])`
    and `next_order(self, context: StrategyContext) -> OrderRequest | None`.
    """

    builtin = BUILTIN_STRATEGIES.get(strategy_spec.lower())
    if builtin is not None:
        return builtin(trader_id, rng, params)

    if ":" not in strategy_spec:
        valid = ", ".join(sorted(BUILTIN_STRATEGIES))
        raise ValueError(f"unknown strategy '{strategy_spec}'. built-ins: {valid} or use module:Class")

    module_name, class_name = strategy_spec.split(":", 1)
    module_name = module_name.strip()
    class_name = class_name.strip()
    if not module_name or not class_name:
        raise ValueError("custom strategy format must be module:Class")

    module = importlib.import_module(module_name)
    strategy_cls = getattr(module, class_name, None)
    if strategy_cls is None:
        raise ValueError(f"custom strategy class '{class_name}' not found in '{module_name}'")

    instance = strategy_cls(trader_id=trader_id, rng=rng, params=params)
    if not hasattr(instance, "next_order"):
        raise ValueError("custom strategy must implement next_order(context)")
    return instance
