// nexus_core/src/matching/mod.rs
//
// The Institutional Matching Engine.
//
// This module implements the core Aggressor-Maker matching loop with:
// 1. Price-Time Priority (best price first, then FIFO at each price level).
// 2. Self-Trade Prevention (STP): cancel resting orders instead of self-matching.
// 3. Pre-Trade Risk Guardian: Fat-Finger price checks, max quantity limits.
// 4. Deterministic execution: same input sequence → identical output every time.

pub mod orderbook;

pub use orderbook::{
    Fill, L2Level, MatchResult, Order, OrderBook, RejectReason, RiskConfig,
};

use crate::types::Side;

/// The Matching Engine.
///
/// Wraps an `OrderBook` and implements the Aggressor-Maker matching algorithm
/// with integrated pre-trade risk checks.
///
/// # Performance Characteristics
/// - Price discovery: O(log N) via BTreeMap sorted traversal.
/// - Queue drain per level: O(1) via VecDeque::pop_front.
/// - STP check: O(1) per resting order (u32 comparison).
/// - Fat-Finger check: O(1) (single integer comparison).
/// - Trade generation: pre-allocated Vec with `with_capacity`.
pub struct MatchingEngine {
    pub book: OrderBook,
    risk_config: RiskConfig,
    #[allow(dead_code)]
    next_trade_id: u64,
    /// Nanosecond timestamp counter for deterministic replay.
    /// In production, this would read from a hardware clock.
    /// In simulation, it increments monotonically per event.
    ts_counter: u64,
}

impl MatchingEngine {
    pub fn new() -> Self {
        Self::with_config(RiskConfig::default())
    }

    pub fn with_config(risk_config: RiskConfig) -> Self {
        Self {
            book: OrderBook::new(),
            risk_config,
            next_trade_id: 0,
            ts_counter: 0,
        }
    }

    /// Advance the deterministic timestamp. Returns the new value.
    fn tick(&mut self) -> u64 {
        self.ts_counter += 1;
        self.ts_counter
    }

    /// Allocate the next unique trade ID.
    fn next_trade_id(&mut self) -> u64 {
        self.next_trade_id += 1;
        self.next_trade_id
    }

    // -----------------------------------------------------------------------
    // PRE-TRADE RISK GUARDIAN
    // -----------------------------------------------------------------------

    /// Validate an incoming order against the risk configuration.
    ///
    /// Checks performed (in order):
    /// 1. Price > 0 (for limit orders).
    /// 2. Quantity > 0.
    /// 3. Quantity ≤ max_order_qty (hard cap).
    /// 4. Fat-Finger: price within ±configured% of last trade price.
    pub fn validate_risk(
        &self,
        price: i64,
        qty: u32,
    ) -> Result<(), RejectReason> {
        if price <= 0 {
            return Err(RejectReason::InvalidPrice);
        }
        if qty == 0 {
            return Err(RejectReason::InvalidQuantity);
        }
        if qty > self.risk_config.max_order_qty {
            return Err(RejectReason::MaxQuantity {
                requested: qty,
                max: self.risk_config.max_order_qty,
            });
        }

        // Fat-Finger check: only if we have a reference price.
        if let Some(ref_price) = self.book.last_trade_price {
            let deviation = ((price - ref_price).abs() * 100_000_000) / ref_price;
            if deviation > self.risk_config.max_price_deviation_pct {
                return Err(RejectReason::FatFinger {
                    order_price: price,
                    reference_price: ref_price,
                });
            }
        }

        Ok(())
    }

    // -----------------------------------------------------------------------
    // THE CORE MATCHING LOOP (Aggressor-Maker Algorithm)
    // -----------------------------------------------------------------------

    /// Submit a new limit order to the engine.
    ///
    /// This is the primary entry point. It performs:
    /// 1. Pre-trade risk validation (Guardian).
    /// 2. Aggressor phase: cross the opposing book.
    /// 3. Self-Trade Prevention: cancel resting orders of the same trader.
    /// 4. Maker phase: post remaining quantity to the book.
    ///
    /// Returns `Ok(MatchResult)` on success, `Err(RejectReason)` if rejected.
    pub fn submit_order(
        &mut self,
        trader_id: u32,
        side: Side,
        price: i64,
        qty: u32,
    ) -> Result<MatchResult, RejectReason> {
        // Phase 0: Guardian risk checks.
        self.validate_risk(price, qty)?;

        let ts = self.tick();
        let order_id = self.book.next_order_id();

        // Pre-allocate output vectors. Max fills = qty (one fill per unit in worst case,
        // but realistically << qty). Start with 8 to avoid early reallocs.
        let mut fills: Vec<Fill> = Vec::with_capacity(8);
        let mut stp_cancels: Vec<u64> = Vec::new();
        let mut remaining_qty = qty;

        // Phase A: The Aggressor — cross the opposing book.
        match side {
            Side::Buy => {
                // Buy crosses against Asks (lowest price first).
                self.match_against_asks(
                    trader_id, order_id, price, &mut remaining_qty,
                    &mut fills, &mut stp_cancels, ts,
                );
            }
            Side::Sell => {
                // Sell crosses against Bids (highest price first).
                self.match_against_bids(
                    trader_id, order_id, price, &mut remaining_qty,
                    &mut fills, &mut stp_cancels, ts,
                );
            }
        }

        // Phase C: The Maker — post remaining quantity to the book.
        if remaining_qty > 0 {
            let resting = Order {
                trader_id,
                order_id,
                price,
                qty: remaining_qty,
                ts,
            };
            match side {
                Side::Buy => self.book.bids.insert(resting),
                Side::Sell => self.book.asks.insert(resting),
            }
        }

        Ok(MatchResult {
            order_id,
            fills,
            stp_cancels,
            resting_qty: remaining_qty,
        })
    }

    /// Match a Buy aggressor against the Ask book (lowest price first).
    ///
    /// The hot loop is kept intentionally "flat" (no function calls inside the
    /// inner while) to minimize branch mispredictions and maximize instruction
    /// pipeline throughput.
    fn match_against_asks(
        &mut self,
        taker_trader_id: u32,
        taker_order_id: u64,
        limit_price: i64,
        remaining_qty: &mut u32,
        fills: &mut Vec<Fill>,
        stp_cancels: &mut Vec<u64>,
        ts: u64,
    ) {
        // Drain ask levels starting from the lowest price.
        while *remaining_qty > 0 {
            // Get the best (lowest) ask price.
            let best_ask = match self.book.asks.levels.keys().next().copied() {
                Some(p) => p,
                None => break, // No more asks.
            };

            // If the best ask is above our limit price, we can't fill.
            if best_ask > limit_price {
                break;
            }

            // Get the order queue at this price level.
            let level = match self.book.asks.levels.get_mut(&best_ask) {
                Some(l) => l,
                None => break,
            };

            // Drain orders at this level in FIFO order.
            while *remaining_qty > 0 && !level.is_empty() {
                let maker = level.front().unwrap();

                // Self-Trade Prevention: if same trader, cancel the resting order.
                if maker.trader_id == taker_trader_id {
                    let cancelled = level.pop_front().unwrap();
                    self.book.asks.total_qty -= cancelled.qty as u64;
                    stp_cancels.push(cancelled.order_id);
                    continue;
                }

                let fill_qty = (*remaining_qty).min(maker.qty);
                let fill_price = maker.price; // Execution at the resting (maker) price.

                fills.push(Fill {
                    maker_order_id: maker.order_id,
                    taker_order_id,
                    maker_trader_id: maker.trader_id,
                    taker_trader_id,
                    price: fill_price,
                    qty: fill_qty,
                    timestamp_ns: ts,
                });

                *remaining_qty -= fill_qty;
                self.book.asks.total_qty -= fill_qty as u64;

                // Update last trade price for Fat-Finger reference.
                self.book.last_trade_price = Some(fill_price);

                if fill_qty >= maker.qty {
                    // Maker fully filled — remove from queue.
                    level.pop_front();
                } else {
                    // Maker partially filled — update remaining quantity.
                    level.front_mut().unwrap().qty -= fill_qty;
                }
            }

            // If the level is now empty, we let it be cleaned up.
            if level.is_empty() {
                self.book.asks.levels.remove(&best_ask);
            }
        }
    }

    /// Match a Sell aggressor against the Bid book (highest price first).
    fn match_against_bids(
        &mut self,
        taker_trader_id: u32,
        taker_order_id: u64,
        limit_price: i64,
        remaining_qty: &mut u32,
        fills: &mut Vec<Fill>,
        stp_cancels: &mut Vec<u64>,
        ts: u64,
    ) {
        while *remaining_qty > 0 {
            let best_bid = match self.book.bids.levels.keys().next_back().copied() {
                Some(p) => p,
                None => break,
            };

            if best_bid < limit_price {
                break;
            }

            let level = match self.book.bids.levels.get_mut(&best_bid) {
                Some(l) => l,
                None => break,
            };

            while *remaining_qty > 0 && !level.is_empty() {
                let maker = level.front().unwrap();

                if maker.trader_id == taker_trader_id {
                    let cancelled = level.pop_front().unwrap();
                    self.book.bids.total_qty -= cancelled.qty as u64;
                    stp_cancels.push(cancelled.order_id);
                    continue;
                }

                let fill_qty = (*remaining_qty).min(maker.qty);
                let fill_price = maker.price;

                fills.push(Fill {
                    maker_order_id: maker.order_id,
                    taker_order_id,
                    maker_trader_id: maker.trader_id,
                    taker_trader_id,
                    price: fill_price,
                    qty: fill_qty,
                    timestamp_ns: ts,
                });

                *remaining_qty -= fill_qty;
                self.book.bids.total_qty -= fill_qty as u64;
                self.book.last_trade_price = Some(fill_price);

                if fill_qty >= maker.qty {
                    level.pop_front();
                } else {
                    level.front_mut().unwrap().qty -= fill_qty;
                }
            }

            if level.is_empty() {
                self.book.bids.levels.remove(&best_bid);
            }
        }
    }

    // -----------------------------------------------------------------------
    // CONVENIENCE ACCESSORS
    // -----------------------------------------------------------------------

    /// Best bid price. O(log N).
    pub fn best_bid(&self) -> Option<i64> {
        self.book.best_bid()
    }

    /// Best ask price. O(log N).
    pub fn best_ask(&self) -> Option<i64> {
        self.book.best_ask()
    }

    /// L2 snapshot for the Market Data relay.
    pub fn l2_snapshot(&self, depth: usize) -> (Vec<L2Level>, Vec<L2Level>) {
        self.book.l2_snapshot(depth)
    }

    /// Clear the entire order book (session reset).
    pub fn clear(&mut self) {
        self.book.clear();
    }

    /// Cancel all orders for a trader (Cancel-on-Disconnect).
    pub fn cancel_all_for_trader(&mut self, trader_id: u32) -> Vec<u64> {
        self.book.cancel_all_for_trader(trader_id)
    }
}

impl Default for MatchingEngine {
    fn default() -> Self {
        Self::new()
    }
}

// ===========================================================================
// TESTS
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Side;

    const SCALE: i64 = 100_000_000;

    fn price(v: i64) -> i64 {
        v * SCALE
    }

    #[test]
    fn test_simple_buy_sell_match() {
        let mut engine = MatchingEngine::new();

        // Post a Sell at $100.
        let r1 = engine.submit_order(1, Side::Sell, price(100), 50).unwrap();
        assert_eq!(r1.fills.len(), 0);
        assert_eq!(r1.resting_qty, 50);

        // Post a Buy at $100 — should match.
        let r2 = engine.submit_order(2, Side::Buy, price(100), 30).unwrap();
        assert_eq!(r2.fills.len(), 1);
        assert_eq!(r2.fills[0].qty, 30);
        assert_eq!(r2.fills[0].price, price(100));
        assert_eq!(r2.fills[0].maker_trader_id, 1);
        assert_eq!(r2.fills[0].taker_trader_id, 2);
        assert_eq!(r2.resting_qty, 0); // Fully filled.

        // Ask should have 20 remaining.
        assert_eq!(engine.best_ask(), Some(price(100)));
    }

    #[test]
    fn test_partial_fill() {
        let mut engine = MatchingEngine::new();

        engine.submit_order(1, Side::Sell, price(100), 10).unwrap();
        let r = engine.submit_order(2, Side::Buy, price(100), 25).unwrap();

        assert_eq!(r.fills.len(), 1);
        assert_eq!(r.fills[0].qty, 10);
        assert_eq!(r.resting_qty, 15); // 25 - 10 = 15 rests on bid.

        assert_eq!(engine.best_bid(), Some(price(100)));
        assert_eq!(engine.best_ask(), None); // Ask was fully consumed.
    }

    #[test]
    fn test_price_time_priority() {
        let mut engine = MatchingEngine::new();

        // Post two Sells: first at $101, then at $100.
        engine.submit_order(1, Side::Sell, price(101), 10).unwrap();
        engine.submit_order(2, Side::Sell, price(100), 10).unwrap();

        // Buy at $101 — should match the LOWER ask ($100) first (price priority).
        let r = engine.submit_order(3, Side::Buy, price(101), 15).unwrap();
        assert_eq!(r.fills.len(), 2);
        assert_eq!(r.fills[0].price, price(100)); // Best price first.
        assert_eq!(r.fills[0].qty, 10);
        assert_eq!(r.fills[1].price, price(101)); // Then next price.
        assert_eq!(r.fills[1].qty, 5);
    }

    #[test]
    fn test_time_priority_fifo() {
        let mut engine = MatchingEngine::new();

        // Two Sells at the SAME price. Trader 1 is first.
        engine.submit_order(1, Side::Sell, price(100), 10).unwrap();
        engine.submit_order(2, Side::Sell, price(100), 10).unwrap();

        // Buy 10 — should match trader 1 first (FIFO).
        let r = engine.submit_order(3, Side::Buy, price(100), 10).unwrap();
        assert_eq!(r.fills.len(), 1);
        assert_eq!(r.fills[0].maker_trader_id, 1); // FIFO: trader 1 was first.
    }

    #[test]
    fn test_self_trade_prevention() {
        let mut engine = MatchingEngine::new();

        // Trader 1 posts a Sell.
        engine.submit_order(1, Side::Sell, price(100), 50).unwrap();

        // Trader 1 sends a Buy at the same price — STP should cancel the resting Sell.
        let r = engine.submit_order(1, Side::Buy, price(100), 30).unwrap();
        assert_eq!(r.fills.len(), 0); // NO match.
        assert_eq!(r.stp_cancels.len(), 1); // Resting sell was cancelled.
        assert_eq!(r.resting_qty, 30); // Buy rests on the book.

        // The ask should be empty now (the sell was cancelled by STP).
        assert_eq!(engine.best_ask(), None);
        // The buy should be resting.
        assert_eq!(engine.best_bid(), Some(price(100)));
    }

    #[test]
    fn test_fat_finger_rejection() {
        let mut engine = MatchingEngine::new();

        // Establish a reference price via a trade.
        engine.submit_order(1, Side::Sell, price(100), 10).unwrap();
        engine.submit_order(2, Side::Buy, price(100), 10).unwrap();

        // Now try a Buy at $200 (100% above last trade) — should be rejected.
        let result = engine.submit_order(3, Side::Buy, price(200), 10);
        assert!(result.is_err());
        match result.unwrap_err() {
            RejectReason::FatFinger { order_price, reference_price } => {
                assert_eq!(order_price, price(200));
                assert_eq!(reference_price, price(100));
            }
            other => panic!("Expected FatFinger, got {:?}", other),
        }
    }

    #[test]
    fn test_max_quantity_rejection() {
        let engine = MatchingEngine::new();
        let result = engine.validate_risk(price(100), 2_000_000);
        assert!(result.is_err());
        match result.unwrap_err() {
            RejectReason::MaxQuantity { requested, max } => {
                assert_eq!(requested, 2_000_000);
                assert_eq!(max, 1_000_000);
            }
            _ => panic!("Expected MaxQuantity rejection"),
        }
    }

    #[test]
    fn test_no_match_across_spread() {
        let mut engine = MatchingEngine::new();

        engine.submit_order(1, Side::Sell, price(105), 10).unwrap();
        // Buy at $100 — ask is at $105, no cross.
        let r = engine.submit_order(2, Side::Buy, price(100), 10).unwrap();
        assert_eq!(r.fills.len(), 0);
        assert_eq!(r.resting_qty, 10); // Rests on the bid book.
    }

    #[test]
    fn test_multiple_levels_consumed() {
        let mut engine = MatchingEngine::new();

        // Stack the ask book: 10@100, 10@101, 10@102.
        engine.submit_order(1, Side::Sell, price(100), 10).unwrap();
        engine.submit_order(2, Side::Sell, price(101), 10).unwrap();
        engine.submit_order(3, Side::Sell, price(102), 10).unwrap();

        // Buy 25 at limit $102 — should consume all of 100, all of 101, 5 of 102.
        let r = engine.submit_order(4, Side::Buy, price(102), 25).unwrap();
        assert_eq!(r.fills.len(), 3);
        assert_eq!(r.fills[0].price, price(100));
        assert_eq!(r.fills[0].qty, 10);
        assert_eq!(r.fills[1].price, price(101));
        assert_eq!(r.fills[1].qty, 10);
        assert_eq!(r.fills[2].price, price(102));
        assert_eq!(r.fills[2].qty, 5);
        assert_eq!(r.resting_qty, 0);

        // 5 should remain at $102.
        assert_eq!(engine.best_ask(), Some(price(102)));
    }

    #[test]
    fn test_l2_snapshot_after_trades() {
        let mut engine = MatchingEngine::new();

        engine.submit_order(1, Side::Buy, price(99), 10).unwrap();
        engine.submit_order(2, Side::Buy, price(100), 20).unwrap();
        engine.submit_order(3, Side::Sell, price(101), 15).unwrap();
        engine.submit_order(4, Side::Sell, price(102), 25).unwrap();

        let (bids, asks) = engine.l2_snapshot(5);
        assert_eq!(bids.len(), 2);
        assert_eq!(asks.len(), 2);
        // Best bid (highest) first.
        assert_eq!(bids[0].price, price(100));
        assert_eq!(bids[0].qty, 20);
        // Best ask (lowest) first.
        assert_eq!(asks[0].price, price(101));
        assert_eq!(asks[0].qty, 15);
    }

    #[test]
    fn test_cancel_on_disconnect() {
        let mut engine = MatchingEngine::new();

        engine.submit_order(1, Side::Buy, price(100), 10).unwrap();
        engine.submit_order(1, Side::Sell, price(105), 20).unwrap();
        engine.submit_order(2, Side::Buy, price(99), 30).unwrap();

        let cancelled = engine.cancel_all_for_trader(1);
        assert_eq!(cancelled.len(), 2);
        assert_eq!(engine.best_bid(), Some(price(99))); // Only trader 2's bid remains.
        assert_eq!(engine.best_ask(), None); // Trader 1's ask was cancelled.
    }
}
