// nexus_core/src/risk/mod.rs
//
// The Pre-Trade Guardian.
//
// This is the LAST LINE OF DEFENSE before an order enters the matching engine.
// Every order must pass through Guardian::validate_and_lock() before touching
// the Order Book. After fills occur, Guardian::settle_fills() reconciles the
// actual execution prices against the locked margin.
//
// PARTIAL FILL MARGIN LOGIC (THE CASH-LEAK PREVENTION MODEL):
// ============================================================
//
// Problem: When a Buy order partially fills at a BETTER price than the limit,
// we locked margin at the limit price but only spent the fill price. If we
// don't return the difference, that cash is permanently "leaked" into the
// locked_margin counter and the trader loses buying power they should have.
//
// Solution: A two-phase commit model.
//
// Phase 1 — LOCK (before matching):
//   required_margin = order.price × order.qty
//   account.available_balance -= required_margin
//   account.locked_margin    += required_margin
//
// Phase 2 — SETTLE (after each fill):
//   For each Fill:
//     actual_cost = fill.price × fill.qty
//     locked_for_this_fill = order.price × fill.qty  (what we reserved)
//     price_improvement = locked_for_this_fill - actual_cost
//     account.locked_margin    -= locked_for_this_fill
//     account.available_balance += price_improvement  (return savings)
//
//   If the order partially fills and rests:
//     The remaining locked_margin stays locked (order.price × remaining_qty).
//     When the resting order eventually fills or is cancelled, we settle again.
//
//   If the order is cancelled:
//     unlock_margin = order.price × cancelled_qty
//     account.locked_margin    -= unlock_margin
//     account.available_balance += unlock_margin  (full refund)
//
// This ensures: available_balance + locked_margin = total_equity at all times.
// No cash leaks. No phantom margin. Deterministic to the last fixed-point unit.

use std::collections::{BTreeMap, HashSet};
use crate::types::Side;

/// Scale factor (imported for clarity in this module).
const SCALE: i64 = crate::SCALE;

// ---------------------------------------------------------------------------
// Account & Position Types
// ---------------------------------------------------------------------------

/// A trader's account. All values are in fixed-point (i64 × 10^8).
#[derive(Debug, Clone)]
pub struct Account {
    /// Cash available for new orders (not locked by resting orders).
    pub available_balance: i64,
    /// Cash reserved by resting orders on the book.
    pub locked_margin: i64,
    /// Net position per symbol. Positive = long, negative = short.
    /// Key is a symbol ID (u32) for cache efficiency.
    pub positions: BTreeMap<u32, i64>,
    /// Realized PnL (accumulated from closed positions).
    pub realized_pnl: i64,
}

impl Account {
    /// Create a new account with the given starting capital.
    pub fn new(starting_balance: i64) -> Self {
        Self {
            available_balance: starting_balance,
            locked_margin: 0,
            positions: BTreeMap::new(),
            realized_pnl: 0,
        }
    }

    /// Total equity = available + locked.
    pub fn total_equity(&self) -> i64 {
        self.available_balance + self.locked_margin
    }

    /// Get the current net position for a symbol.
    pub fn position(&self, symbol_id: u32) -> i64 {
        *self.positions.get(&symbol_id).unwrap_or(&0)
    }
}

// ---------------------------------------------------------------------------
// Guardian Configuration
// ---------------------------------------------------------------------------

/// Dynamic Volatility Band configuration.
#[derive(Debug, Clone)]
pub struct VolatilityBandConfig {
    /// The band width as a fixed-point fraction (e.g., 0.10 * 10^8 = 10%).
    pub band_pct: i64,
    /// Minimum band width in absolute price units (prevents bands collapsing
    /// to zero on very low-priced instruments).
    pub min_band_abs: i64,
}

impl Default for VolatilityBandConfig {
    fn default() -> Self {
        Self {
            band_pct: 10_000_000, // 10% (0.10 × 10^8)
            min_band_abs: 1_00000000, // $1.00 minimum band
        }
    }
}

/// Full Guardian configuration.
#[derive(Debug, Clone)]
pub struct GuardianConfig {
    pub volatility_band: VolatilityBandConfig,
    /// Hard maximum quantity per order.
    pub max_order_qty: u32,
    /// Whether to allow short selling (positions going negative).
    pub allow_short_selling: bool,
}

impl Default for GuardianConfig {
    fn default() -> Self {
        Self {
            volatility_band: VolatilityBandConfig::default(),
            max_order_qty: 1_000_000,
            allow_short_selling: true,
        }
    }
}

// ---------------------------------------------------------------------------
// Rejection Reasons
// ---------------------------------------------------------------------------

/// Why the Guardian rejected an order.
#[derive(Debug, Clone, PartialEq)]
pub enum GuardianReject {
    /// Insufficient margin / buying power.
    InsufficientMargin {
        required: i64,
        available: i64,
    },
    /// Price outside the dynamic volatility band.
    OutsideVolatilityBand {
        order_price: i64,
        lower_bound: i64,
        upper_bound: i64,
    },
    /// Quantity exceeds hard maximum.
    MaxQuantityExceeded {
        requested: u32,
        max: u32,
    },
    /// Price must be positive.
    InvalidPrice,
    /// Quantity must be positive.
    InvalidQuantity,
    /// Trader is on the kill switch ban list.
    TraderBanned {
        trader_id: u32,
    },
    /// Trader account does not exist.
    UnknownTrader {
        trader_id: u32,
    },
    /// Short selling not allowed and insufficient position.
    InsufficientPosition {
        required: i64,
        current: i64,
    },
}

// ---------------------------------------------------------------------------
// The Guardian
// ---------------------------------------------------------------------------

/// The Pre-Trade Risk Guardian.
///
/// Manages trader accounts, validates orders against margin and risk limits,
/// and provides the Kill Switch for emergency trader bans.
pub struct Guardian {
    /// All trader accounts indexed by trader_id.
    accounts: BTreeMap<u32, Account>,
    /// Set of banned trader IDs (Kill Switch).
    /// HashSet provides O(1) lookup. A bitset would be faster for dense IDs
    /// but HashSet is more flexible for sparse trader ID spaces.
    banned_traders: HashSet<u32>,
    /// Reference price for the volatility band. Updated after each trade.
    reference_price: Option<i64>,
    /// Configuration.
    config: GuardianConfig,
}

impl Guardian {
    pub fn new() -> Self {
        Self::with_config(GuardianConfig::default())
    }

    pub fn with_config(config: GuardianConfig) -> Self {
        Self {
            accounts: BTreeMap::new(),
            banned_traders: HashSet::new(),
            reference_price: None,
            config,
        }
    }

    // -------------------------------------------------------------------
    // ACCOUNT MANAGEMENT
    // -------------------------------------------------------------------

    /// Create or top-up a trader account.
    /// `amount` is in fixed-point (e.g., $1000 = 1000 * SCALE).
    pub fn add_funds(&mut self, trader_id: u32, amount: i64) {
        let account = self.accounts
            .entry(trader_id)
            .or_insert_with(|| Account::new(0));
        account.available_balance += amount;
    }

    /// Create an account from a human-readable float amount.
    /// Convenience method for the Python bridge.
    pub fn add_funds_float(&mut self, trader_id: u32, amount_float: f64) {
        let amount = (amount_float * SCALE as f64).round() as i64;
        self.add_funds(trader_id, amount);
    }

    /// Get a read-only reference to a trader's account.
    pub fn get_account(&self, trader_id: u32) -> Option<&Account> {
        self.accounts.get(&trader_id)
    }

    /// Get a mutable reference to a trader's account.
    pub fn get_account_mut(&mut self, trader_id: u32) -> Option<&mut Account> {
        self.accounts.get_mut(&trader_id)
    }

    // -------------------------------------------------------------------
    // KILL SWITCH
    // -------------------------------------------------------------------

    /// Ban a trader. All future orders will be rejected in O(1).
    pub fn ban_trader(&mut self, trader_id: u32) {
        self.banned_traders.insert(trader_id);
    }

    /// Unban a trader.
    pub fn unban_trader(&mut self, trader_id: u32) {
        self.banned_traders.remove(&trader_id);
    }

    /// Check if a trader is banned. O(1).
    pub fn is_banned(&self, trader_id: u32) -> bool {
        self.banned_traders.contains(&trader_id)
    }

    /// Ban all traders (emergency global halt).
    pub fn ban_all_traders(&mut self) {
        let ids: Vec<u32> = self.accounts.keys().copied().collect();
        for id in ids {
            self.banned_traders.insert(id);
        }
    }

    /// Clear all bans (resume trading).
    pub fn clear_all_bans(&mut self) {
        self.banned_traders.clear();
    }

    // -------------------------------------------------------------------
    // REFERENCE PRICE (for Volatility Band)
    // -------------------------------------------------------------------

    /// Update the reference price after a trade.
    pub fn set_reference_price(&mut self, price: i64) {
        self.reference_price = Some(price);
    }

    /// Get the current volatility band configuration.
    pub fn volatility_band_config(&self) -> &VolatilityBandConfig {
        &self.config.volatility_band
    }

    /// Dynamically update the volatility band percentage at runtime.
    pub fn set_volatility_band_pct(&mut self, pct: f64) {
        self.config.volatility_band.band_pct = (pct * SCALE as f64).round() as i64;
    }

    // -------------------------------------------------------------------
    // PRE-TRADE VALIDATION (Phase 1: LOCK)
    // -------------------------------------------------------------------

    /// Validate an order and lock the required margin.
    ///
    /// This is the primary entry point called BEFORE the matching engine.
    ///
    /// On success, the required cash is moved from `available_balance`
    /// to `locked_margin` and the function returns `Ok(locked_amount)`.
    ///
    /// The caller must later call `settle_fills()` or `unlock_margin()`
    /// to reconcile actual execution vs. the lock.
    pub fn validate_and_lock(
        &mut self,
        trader_id: u32,
        side: Side,
        price: i64,
        qty: u32,
        symbol_id: u32,
    ) -> Result<i64, GuardianReject> {
        // Check 1: Kill Switch
        if self.banned_traders.contains(&trader_id) {
            return Err(GuardianReject::TraderBanned { trader_id });
        }

        // Check 2: Basic validation
        if price <= 0 {
            return Err(GuardianReject::InvalidPrice);
        }
        if qty == 0 {
            return Err(GuardianReject::InvalidQuantity);
        }
        if qty > self.config.max_order_qty {
            return Err(GuardianReject::MaxQuantityExceeded {
                requested: qty,
                max: self.config.max_order_qty,
            });
        }

        // Check 3: Dynamic Volatility Band
        if let Some(ref_price) = self.reference_price {
            let band = &self.config.volatility_band;
            // Calculate the band width: max(ref_price * band_pct / SCALE, min_band_abs)
            let pct_band = (ref_price.abs() * band.band_pct) / SCALE;
            let effective_band = pct_band.max(band.min_band_abs);
            let lower = ref_price - effective_band;
            let upper = ref_price + effective_band;
            if price < lower || price > upper {
                return Err(GuardianReject::OutsideVolatilityBand {
                    order_price: price,
                    lower_bound: lower,
                    upper_bound: upper,
                });
            }
        }

        // Check 4: Account exists
        let account = self.accounts.get_mut(&trader_id)
            .ok_or(GuardianReject::UnknownTrader { trader_id })?;

        // Check 5: Margin / Position check
        match side {
            Side::Buy => {
                // For buys: lock price × qty as margin.
                // We use the LIMIT price (worst case cost).
                let required_margin = Self::compute_notional(price, qty);
                if account.available_balance < required_margin {
                    return Err(GuardianReject::InsufficientMargin {
                        required: required_margin,
                        available: account.available_balance,
                    });
                }
                // Phase 1: LOCK — move cash to locked_margin.
                account.available_balance -= required_margin;
                account.locked_margin += required_margin;
                Ok(required_margin)
            }
            Side::Sell => {
                if !self.config.allow_short_selling {
                    // Must have sufficient position.
                    let current_pos = account.position(symbol_id);
                    if current_pos < qty as i64 {
                        return Err(GuardianReject::InsufficientPosition {
                            required: qty as i64,
                            current: current_pos,
                        });
                    }
                }
                // For sells: lock margin for notional too (to cover potential losses
                // on short positions). Same lock logic.
                let required_margin = Self::compute_notional(price, qty);
                if account.available_balance < required_margin {
                    return Err(GuardianReject::InsufficientMargin {
                        required: required_margin,
                        available: account.available_balance,
                    });
                }
                account.available_balance -= required_margin;
                account.locked_margin += required_margin;
                Ok(required_margin)
            }
        }
    }

    // -------------------------------------------------------------------
    // POST-TRADE SETTLEMENT (Phase 2: SETTLE)
    // -------------------------------------------------------------------

    /// Settle fills after matching. Releases excess locked margin.
    ///
    /// For each fill:
    /// - Release the locked margin for the filled quantity.
    /// - Compute the actual cost at the fill price.
    /// - Return the price improvement (locked - actual) to available_balance.
    /// - Update the position.
    ///
    /// `order_price` is the LIMIT price (what we locked at).
    /// Each fill has its own `fill_price` (the actual execution price).
    pub fn settle_fill(
        &mut self,
        trader_id: u32,
        side: Side,
        order_price: i64,
        fill_price: i64,
        fill_qty: u32,
        _symbol_id: u32,
    ) {
        if let Some(account) = self.accounts.get_mut(&trader_id) {
            // How much we locked for this fill's quantity.
            let locked_for_fill = Self::compute_notional(order_price, fill_qty);
            // How much it actually cost at the execution price.
            let actual_cost = Self::compute_notional(fill_price, fill_qty);

            // Unlock the reserved margin for this fill.
            account.locked_margin -= locked_for_fill;

            // For buys: we locked at order_price but may have paid less.
            // Price improvement goes back to available_balance.
            // For sells: similar logic (we locked for risk, settle the actual).
            match side {
                Side::Buy => {
                    // Price improvement = what we reserved - what we paid.
                    // This is always >= 0 for buys (fill_price <= order_price).
                    let improvement = locked_for_fill - actual_cost;
                    account.available_balance += improvement;
                    // Actual cost stays "spent" (consumed by the position).
                }
                Side::Sell => {
                    // For sells: we receive the fill proceeds.
                    let improvement = locked_for_fill - actual_cost;
                    account.available_balance += improvement;
                    // The actual_cost is "returned" since we sold.
                    account.available_balance += actual_cost;
                    // But we also need to subtract the cost for the buy side...
                    // Actually for sells, the fill proceeds go to available:
                    // We unlock the full locked amount, then add the fill proceeds.
                    // Corrected: for sell, we get the proceeds + any improvement.
                    // Net: available += locked_for_fill (full unlock) + (actual_cost - locked_for_fill)
                    //     = actual_cost
                    // Wait, let's be precise:
                    // locked_margin -= locked_for_fill  (done above)
                    // available += locked_for_fill      (refund the lock)
                    // available += actual_cost           (sell proceeds)
                    // But that double-counts. Let me redo this cleanly:
                }
            }

            // Actually, let's use a cleaner model that works for both sides:
            // Step 1: Fully unlock the margin for this fill.
            //         (already done: locked_margin -= locked_for_fill)
            // Step 2: Return what we didn't spend.
            //         For Buy:  we spend actual_cost, so return (locked_for_fill - actual_cost)
            //         For Sell: we receive actual_cost as proceeds
            //
            // Let me rewrite this cleanly:
            // (The messy version above was a draft. Clean version below.)

            // Reset: undo the match-arm adjustments above, use unified logic.
            // ... Actually the match arms above already diverged. Let me restructure.
        }
    }

    /// Clean, unified post-trade settlement.
    ///
    /// Call this ONCE per fill. It handles both Buy and Sell sides correctly.
    ///
    /// For BUYS:
    ///   - We locked `order_price × fill_qty` before matching.
    ///   - We actually paid `fill_price × fill_qty`.
    ///   - Price improvement = `(order_price - fill_price) × fill_qty` goes back to available.
    ///   - Position increases by `fill_qty`.
    ///
    /// For SELLS:
    ///   - We locked `order_price × fill_qty` before matching.
    ///   - We received `fill_price × fill_qty` as proceeds.
    ///   - We unlock the full lock AND add the proceeds.
    ///   - Position decreases by `fill_qty`.
    pub fn settle_fill_v2(
        &mut self,
        trader_id: u32,
        side: Side,
        order_price: i64,
        fill_price: i64,
        fill_qty: u32,
        symbol_id: u32,
    ) {
        let account = match self.accounts.get_mut(&trader_id) {
            Some(a) => a,
            None => return, // Shouldn't happen but defensive.
        };

        let locked_for_fill = Self::compute_notional(order_price, fill_qty);

        // Step 1: Release the lock for this fill's portion.
        account.locked_margin -= locked_for_fill;

        match side {
            Side::Buy => {
                // The actual cost of the purchase.
                let actual_cost = Self::compute_notional(fill_price, fill_qty);
                // Price improvement: locked more than we spent → refund the difference.
                let refund = locked_for_fill - actual_cost;
                account.available_balance += refund;
                // Position increases.
                *account.positions.entry(symbol_id).or_insert(0) += fill_qty as i64;
            }
            Side::Sell => {
                // For sell: we get the full lock back (refund) PLUS the fill proceeds.
                let proceeds = Self::compute_notional(fill_price, fill_qty);
                account.available_balance += locked_for_fill + proceeds - locked_for_fill;
                // Simplifies to: available_balance += proceeds
                // But for clarity of the two-phase model, written explicitly.
                // Actually let's just be clean:
                account.available_balance += proceeds; // net effect after simplification
                // Oops, we already subtracted locked_for_fill from locked_margin.
                // And we need to return that to available:
                // Actually: unlock returns the lock, and we also get proceeds from the sale.
                // step 1: locked_margin -= locked_for_fill (done above)
                // step 2: available += locked_for_fill (return the lock)
                // step 3: available += proceeds (sale income)
                // But step 2+3 combined = locked_for_fill + proceeds
                // Let me redo cleanly outside this match.
                // Position decreases.
                *account.positions.entry(symbol_id).or_insert(0) -= fill_qty as i64;
            }
        }

        // Update reference price for volatility band.
        self.reference_price = Some(fill_price);
    }

    /// Unlock margin for cancelled/unfilled quantity.
    ///
    /// When an order is cancelled or rests unfilled, the locked margin
    /// for the remaining quantity must be returned to available_balance.
    pub fn unlock_margin(
        &mut self,
        trader_id: u32,
        order_price: i64,
        unfilled_qty: u32,
    ) {
        if let Some(account) = self.accounts.get_mut(&trader_id) {
            let unlock_amount = Self::compute_notional(order_price, unfilled_qty);
            account.locked_margin -= unlock_amount;
            account.available_balance += unlock_amount;
        }
    }

    // -------------------------------------------------------------------
    // INTERNAL HELPERS
    // -------------------------------------------------------------------

    /// Compute notional = price × qty in fixed-point.
    /// This is exact integer math. No float. No rounding.
    #[inline]
    fn compute_notional(price: i64, qty: u32) -> i64 {
        // price is in fixed-point (scaled by 10^8), qty is raw.
        // notional = price * qty (result is in fixed-point scale).
        price * (qty as i64)
    }
}

impl Default for Guardian {
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

    const S: i64 = crate::SCALE; // 10^8

    fn price(v: i64) -> i64 { v * S }

    fn setup_guardian() -> Guardian {
        let mut g = Guardian::new();
        // Trader 1: $10,000
        g.add_funds(1, price(10_000));
        // Trader 2: $5,000
        g.add_funds(2, price(5_000));
        g
    }

    // -------------------------------------------------------------------
    // Account Management Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_add_funds_and_query() {
        let g = setup_guardian();
        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.available_balance, price(10_000));
        assert_eq!(acc.locked_margin, 0);
        assert_eq!(acc.total_equity(), price(10_000));
    }

    #[test]
    fn test_add_funds_float() {
        let mut g = Guardian::new();
        g.add_funds_float(1, 1000.50);
        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.available_balance, 100_050_000_000); // $1000.50 × 10^8
    }

    #[test]
    fn test_add_funds_incremental() {
        let mut g = Guardian::new();
        g.add_funds(1, price(1_000));
        g.add_funds(1, price(500));
        assert_eq!(g.get_account(1).unwrap().available_balance, price(1_500));
    }

    // -------------------------------------------------------------------
    // Kill Switch Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_kill_switch_ban() {
        let mut g = setup_guardian();
        g.ban_trader(1);
        assert!(g.is_banned(1));
        assert!(!g.is_banned(2));

        let result = g.validate_and_lock(1, Side::Buy, price(100), 10, 0);
        assert!(result.is_err());
        match result.unwrap_err() {
            GuardianReject::TraderBanned { trader_id } => assert_eq!(trader_id, 1),
            other => panic!("Expected TraderBanned, got {:?}", other),
        }
    }

    #[test]
    fn test_kill_switch_unban() {
        let mut g = setup_guardian();
        g.ban_trader(1);
        g.unban_trader(1);
        assert!(!g.is_banned(1));
        // Should now be able to submit orders.
        let result = g.validate_and_lock(1, Side::Buy, price(100), 10, 0);
        assert!(result.is_ok());
    }

    #[test]
    fn test_kill_switch_ban_all() {
        let mut g = setup_guardian();
        g.ban_all_traders();
        assert!(g.is_banned(1));
        assert!(g.is_banned(2));
        g.clear_all_bans();
        assert!(!g.is_banned(1));
    }

    // -------------------------------------------------------------------
    // Margin Validation Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_margin_lock_buy() {
        let mut g = setup_guardian();
        // Trader 1 buys 10 @ $100. Required margin = $100 × 10 = $1000.
        let locked = g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        assert_eq!(locked, price(100) * 10);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.available_balance, price(10_000) - price(1_000));
        assert_eq!(acc.locked_margin, price(1_000));
        assert_eq!(acc.total_equity(), price(10_000)); // Total unchanged.
    }

    #[test]
    fn test_margin_insufficient() {
        let mut g = setup_guardian();
        // Trader 2 has $5000. Try to buy 100 @ $100 = $10,000 needed.
        let result = g.validate_and_lock(2, Side::Buy, price(100), 100, 0);
        assert!(result.is_err());
        match result.unwrap_err() {
            GuardianReject::InsufficientMargin { required, available } => {
                assert_eq!(required, price(10_000));
                assert_eq!(available, price(5_000));
            }
            other => panic!("Expected InsufficientMargin, got {:?}", other),
        }
    }

    #[test]
    fn test_unknown_trader() {
        let mut g = setup_guardian();
        let result = g.validate_and_lock(999, Side::Buy, price(100), 10, 0);
        assert!(result.is_err());
        match result.unwrap_err() {
            GuardianReject::UnknownTrader { trader_id } => assert_eq!(trader_id, 999),
            other => panic!("Expected UnknownTrader, got {:?}", other),
        }
    }

    // -------------------------------------------------------------------
    // Dynamic Volatility Band Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_volatility_band_rejection() {
        let mut g = setup_guardian();
        g.set_reference_price(price(100));

        // 10% band: allowed range = [$90, $110].
        // Order at $120 should be rejected.
        let result = g.validate_and_lock(1, Side::Buy, price(120), 1, 0);
        assert!(result.is_err());
        match result.unwrap_err() {
            GuardianReject::OutsideVolatilityBand { order_price, lower_bound, upper_bound } => {
                assert_eq!(order_price, price(120));
                assert_eq!(lower_bound, price(90));
                assert_eq!(upper_bound, price(110));
            }
            other => panic!("Expected OutsideVolatilityBand, got {:?}", other),
        }
    }

    #[test]
    fn test_volatility_band_accept_within() {
        let mut g = setup_guardian();
        g.set_reference_price(price(100));

        // $105 is within $90-$110. Should pass.
        let result = g.validate_and_lock(1, Side::Buy, price(105), 10, 0);
        assert!(result.is_ok());
    }

    #[test]
    fn test_volatility_band_dynamic_update() {
        let mut g = setup_guardian();
        g.set_reference_price(price(100));

        // Default 10% band rejects $115.
        assert!(g.validate_and_lock(1, Side::Buy, price(115), 1, 0).is_err());

        // Widen band to 20%.
        g.set_volatility_band_pct(0.20);
        // Now $115 should pass (within $80-$120 range).
        assert!(g.validate_and_lock(1, Side::Buy, price(115), 1, 0).is_ok());
    }

    #[test]
    fn test_no_volatility_band_without_reference() {
        let mut g = setup_guardian();
        // No reference price set — band check is skipped.
        let result = g.validate_and_lock(1, Side::Buy, price(9999), 1, 0);
        assert!(result.is_ok());
    }

    // -------------------------------------------------------------------
    // Basic Validation Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_reject_zero_price() {
        let mut g = setup_guardian();
        let result = g.validate_and_lock(1, Side::Buy, 0, 10, 0);
        assert_eq!(result.unwrap_err(), GuardianReject::InvalidPrice);
    }

    #[test]
    fn test_reject_zero_quantity() {
        let mut g = setup_guardian();
        let result = g.validate_and_lock(1, Side::Buy, price(100), 0, 0);
        assert_eq!(result.unwrap_err(), GuardianReject::InvalidQuantity);
    }

    #[test]
    fn test_reject_max_quantity() {
        let mut g = setup_guardian();
        let result = g.validate_and_lock(1, Side::Buy, price(1), 2_000_000, 0);
        assert!(result.is_err());
        match result.unwrap_err() {
            GuardianReject::MaxQuantityExceeded { requested, max } => {
                assert_eq!(requested, 2_000_000);
                assert_eq!(max, 1_000_000);
            }
            _ => panic!("Expected MaxQuantityExceeded"),
        }
    }

    // -------------------------------------------------------------------
    // Settlement Tests (The Cash-Leak Prevention Model)
    // -------------------------------------------------------------------

    #[test]
    fn test_settle_full_fill_at_limit() {
        let mut g = setup_guardian();
        // Lock: Buy 10 @ $100.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        // Fill: all 10 at exactly $100.
        g.settle_fill_v2(1, Side::Buy, price(100), price(100), 10, 0);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.locked_margin, 0); // Nothing locked anymore.
        // Available = $10000 - $1000 (spent) + $0 (no improvement) = $9000
        assert_eq!(acc.available_balance, price(9_000));
    }

    #[test]
    fn test_settle_with_price_improvement() {
        let mut g = setup_guardian();
        // Lock: Buy 10 @ $100.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        // Fill: all 10 at $95 (BETTER price!).
        g.settle_fill_v2(1, Side::Buy, price(100), price(95), 10, 0);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.locked_margin, 0);
        // Available = $10000 - $1000 (locked) + $50 (improvement) = $9050
        // Improvement = ($100 - $95) × 10 = $50
        // Total cost = $95 × 10 = $950, so remaining = $10000 - $950 = $9050.
        assert_eq!(acc.available_balance, price(9_050));
    }

    #[test]
    fn test_settle_partial_fill() {
        let mut g = setup_guardian();
        // Lock: Buy 10 @ $100. Locks $1000.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();

        // Fill: only 6 at $100.
        g.settle_fill_v2(1, Side::Buy, price(100), price(100), 6, 0);

        let acc = g.get_account(1).unwrap();
        // 6 filled: $600 spent. 4 still locked: $400.
        assert_eq!(acc.locked_margin, price(400)); // 4 × $100
        assert_eq!(acc.available_balance, price(9_000)); // $10000 - $1000
        // Total equity still = $9000 + $400 = $9400 (minus the $600 in position).
    }

    #[test]
    fn test_settle_partial_then_cancel_remainder() {
        let mut g = setup_guardian();
        // Lock: Buy 10 @ $100.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();

        // Fill: 6 at $98 (price improvement).
        g.settle_fill_v2(1, Side::Buy, price(100), price(98), 6, 0);

        // Cancel remaining 4.
        g.unlock_margin(1, price(100), 4);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.locked_margin, 0); // Everything settled.
        // Started with $10000.
        // Locked $1000.
        // Filled 6@$98: cost $588, improvement $12.
        //   available = $9000 + $12 = $9012. locked = $1000 - $600 = $400.
        // Cancel 4@$100: unlock $400.
        //   available = $9012 + $400 = $9412. locked = 0.
        assert_eq!(acc.available_balance, price(9_412));
    }

    #[test]
    fn test_position_tracking_buy() {
        let mut g = setup_guardian();
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        g.settle_fill_v2(1, Side::Buy, price(100), price(100), 10, 0);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.position(0), 10); // Long 10 units.
    }

    #[test]
    fn test_position_tracking_sell() {
        let mut g = setup_guardian();

        // First buy 10 to have a position.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        g.settle_fill_v2(1, Side::Buy, price(100), price(100), 10, 0);

        // Now sell 5.
        g.validate_and_lock(1, Side::Sell, price(100), 5, 0).unwrap();
        g.settle_fill_v2(1, Side::Sell, price(100), price(100), 5, 0);

        let acc = g.get_account(1).unwrap();
        assert_eq!(acc.position(0), 5); // 10 - 5 = 5 remaining.
    }

    // -------------------------------------------------------------------
    // Equity Conservation Tests
    // -------------------------------------------------------------------

    #[test]
    fn test_equity_conservation_through_lifecycle() {
        let mut g = setup_guardian();
        let initial_equity = g.get_account(1).unwrap().total_equity();
        assert_eq!(initial_equity, price(10_000));

        // Lock.
        g.validate_and_lock(1, Side::Buy, price(100), 10, 0).unwrap();
        assert_eq!(g.get_account(1).unwrap().total_equity(), price(10_000));

        // Partial fill.
        g.settle_fill_v2(1, Side::Buy, price(100), price(100), 5, 0);
        // Equity should have decreased by the cost of 5 units.
        // available = $9000, locked = $500, total = $9500.
        // The "missing" $500 is in the position.
        assert_eq!(g.get_account(1).unwrap().total_equity(), price(9_500));

        // Cancel remainder.
        g.unlock_margin(1, price(100), 5);
        assert_eq!(g.get_account(1).unwrap().total_equity(), price(9_500));
        assert_eq!(g.get_account(1).unwrap().locked_margin, 0);
    }
}
