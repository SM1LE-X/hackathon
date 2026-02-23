// nexus_core/src/python.rs
//
// Production-Grade PyO3 Python Bindings.
//
// ZERO-COPY DESIGN:
// - submit_order() accepts raw bytes (&[u8]) and returns PyBytes.
// - Python never constructs Price/Side/Order objects on the hot path.
// - Network bytes → Rust → Network bytes. No Python-level allocation.
//
// MEMORY ALLOCATION STRATEGY FOR TRADE RETURNS:
// - Fills are serialized into a pre-allocated Vec<u8> buffer INSIDE Rust.
// - The buffer is returned to Python as a single PyBytes object.
// - Python splits the buffer into individual trade records by fixed stride.
// - This means: 1 Python allocation per submit_order call, regardless of
//   how many fills occurred (vs N allocations for N fill dicts in Python).

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use pyo3::exceptions::{PyValueError, PyRuntimeError};

use crate::persistence::NexusExchange;
use crate::types::Side;
use crate::SCALE;

use std::time::Instant;

// ---------------------------------------------------------------------------
// Performance Tracker
// ---------------------------------------------------------------------------

/// Tracks hot-path latency for the dashboard.
struct PerfTracker {
    total_orders: u64,
    total_fills: u64,
    total_volume: u64,
    cumulative_latency_ns: u64,
    last_match_latency_ns: u64,
}

impl PerfTracker {
    fn new() -> Self {
        Self {
            total_orders: 0,
            total_fills: 0,
            total_volume: 0,
            cumulative_latency_ns: 0,
            last_match_latency_ns: 0,
        }
    }

    fn record_order(&mut self, fills: usize, volume: u64, latency_ns: u64) {
        self.total_orders += 1;
        self.total_fills += fills as u64;
        self.total_volume += volume;
        self.cumulative_latency_ns += latency_ns;
        self.last_match_latency_ns = latency_ns;
    }

    fn avg_latency_ns(&self) -> u64 {
        if self.total_orders == 0 { 0 } else {
            self.cumulative_latency_ns / self.total_orders
        }
    }
}

// ---------------------------------------------------------------------------
// Fill serialization format (for zero-copy returns to Python)
// ---------------------------------------------------------------------------
//
// Each fill is serialized as a fixed 40-byte record:
//   [8: maker_order_id][8: taker_order_id]
//   [4: maker_trader_id][4: taker_trader_id]
//   [8: price (fixed-point)][4: qty][4: padding]
//
pub const FILL_RECORD_SIZE: usize = 40;

fn serialize_fills(fills: &[crate::matching::Fill]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(fills.len() * FILL_RECORD_SIZE);
    for fill in fills {
        buf.extend_from_slice(&fill.maker_order_id.to_le_bytes());
        buf.extend_from_slice(&fill.taker_order_id.to_le_bytes());
        buf.extend_from_slice(&fill.maker_trader_id.to_le_bytes());
        buf.extend_from_slice(&fill.taker_trader_id.to_le_bytes());
        buf.extend_from_slice(&fill.price.to_le_bytes());
        buf.extend_from_slice(&fill.qty.to_le_bytes());
        buf.extend_from_slice(&[0u8; 4]); // Padding for alignment.
    }
    buf
}

// ---------------------------------------------------------------------------
// L2 Level serialization (16 bytes per level)
// ---------------------------------------------------------------------------
//
// [8: price][4: qty (truncated to u32)][4: order_count]
//
pub const L2_LEVEL_SIZE: usize = 16;

fn serialize_l2_levels(levels: &[crate::matching::L2Level]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(levels.len() * L2_LEVEL_SIZE);
    for level in levels {
        buf.extend_from_slice(&level.price.to_le_bytes());
        buf.extend_from_slice(&(level.qty as u32).to_le_bytes());
        buf.extend_from_slice(&level.order_count.to_le_bytes());
    }
    buf
}

// ---------------------------------------------------------------------------
// PyNexusExchange — The Python-facing exchange wrapper
// ---------------------------------------------------------------------------

/// The Nexus Exchange — Python interface to the Rust HFT engine.
///
/// Usage from Python:
/// ```python
/// from nexus_core import PyNexusExchange
///
/// exchange = PyNexusExchange()
/// exchange.add_funds(trader_id=1, amount=10000.0)
/// exchange.add_funds(trader_id=2, amount=10000.0)
///
/// # Submit order as raw bytes: [4:trader_id][1:side][8:price_raw][4:qty]
/// result = exchange.submit_order(order_bytes)
/// fills_bytes = result  # Raw bytes, 40 bytes per fill
/// ```
#[pyclass(name = "PyNexusExchange")]
pub struct PyNexusExchange {
    inner: NexusExchange,
    perf: PerfTracker,
}

#[pymethods]
impl PyNexusExchange {
    /// Create a new exchange without WAL persistence.
    #[new]
    #[pyo3(signature = (wal_path=None))]
    fn new(wal_path: Option<String>) -> PyResult<Self> {
        let inner = match wal_path {
            Some(path) => NexusExchange::with_persistence(&path)
                .map_err(|e| PyRuntimeError::new_err(format!("WAL init failed: {}", e)))?,
            None => NexusExchange::new(),
        };
        Ok(Self {
            inner,
            perf: PerfTracker::new(),
        })
    }

    // -------------------------------------------------------------------
    // ACCOUNT MANAGEMENT
    // -------------------------------------------------------------------

    /// Add funds to a trader account.
    ///
    /// Args:
    ///     trader_id (int): The trader's unique ID.
    ///     amount (float): Dollar amount to add (e.g., 10000.0).
    fn add_funds(&mut self, trader_id: u32, amount: f64) {
        self.inner.add_funds_float(trader_id, amount);
    }

    /// Add funds using raw fixed-point amount.
    fn add_funds_raw(&mut self, trader_id: u32, amount_raw: i64) {
        self.inner.add_funds(trader_id, amount_raw);
    }

    // -------------------------------------------------------------------
    // ORDER SUBMISSION — THE HOT PATH
    // -------------------------------------------------------------------

    /// Submit an order using raw bytes. Zero-copy hot path.
    ///
    /// Args:
    ///     order_bytes (bytes): 17-byte packed order:
    ///         [4: trader_id (u32 LE)]
    ///         [1: side (1=Buy, 2=Sell)]
    ///         [8: price (i64 LE, fixed-point)]  
    ///         [4: qty (u32 LE)]
    ///
    /// Returns:
    ///     bytes: Packed fill records, 40 bytes each.
    ///            Empty bytes if no fills (resting order).
    ///
    /// Raises:
    ///     ValueError: If order bytes are malformed.
    ///     RuntimeError: If rejected by risk gate (insufficient margin, fat-finger, etc).
    fn submit_order<'py>(&mut self, py: Python<'py>, order_bytes: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        if order_bytes.len() < 17 {
            return Err(PyValueError::new_err(
                format!("Order bytes must be >= 17 bytes, got {}", order_bytes.len())
            ));
        }

        let trader_id = u32::from_le_bytes(order_bytes[0..4].try_into().unwrap());
        let side = match order_bytes[4] {
            1 => Side::Buy,
            2 => Side::Sell,
            v => return Err(PyValueError::new_err(format!("Invalid side byte: {}. Must be 1 (Buy) or 2 (Sell)", v))),
        };
        let price = i64::from_le_bytes(order_bytes[5..13].try_into().unwrap());
        let qty = u32::from_le_bytes(order_bytes[13..17].try_into().unwrap());

        // Time the hot path.
        let start = Instant::now();

        let result = self.inner.submit_order(trader_id, side, price, qty)
            .map_err(|e| PyRuntimeError::new_err(format!("{:?}", e)))?;

        let elapsed_ns = start.elapsed().as_nanos() as u64;

        // Track performance.
        let volume: u64 = result.match_result.fills.iter().map(|f| f.qty as u64).sum();
        self.perf.record_order(result.match_result.fills.len(), volume, elapsed_ns);

        // Serialize fills to bytes (single allocation, returned as PyBytes).
        let fill_bytes = serialize_fills(&result.match_result.fills);
        Ok(PyBytes::new(py, &fill_bytes))
    }

    /// Submit an order using human-readable parameters.
    ///
    /// Convenience method for interactive use. NOT the hot path.
    ///
    /// Args:
    ///     trader_id (int): Trader ID.
    ///     side (str): "buy" or "sell" (case-insensitive).
    ///     price (float): Limit price (e.g., 100.05).
    ///     qty (int): Order quantity.
    ///
    /// Returns:
    ///     dict: { "order_id": int, "fills": list[dict], "resting_qty": int }
    fn submit_order_human<'py>(&mut self, py: Python<'py>, trader_id: u32, side: &str, price: f64, qty: u32) -> PyResult<Bound<'py, PyDict>> {
        let side = Side::from_str(side)
            .map_err(|e| PyValueError::new_err(e))?;
        let price_raw = (price * SCALE as f64).round() as i64;

        let start = Instant::now();

        let result = self.inner.submit_order(trader_id, side, price_raw, qty)
            .map_err(|e| PyRuntimeError::new_err(format!("{:?}", e)))?;

        let elapsed_ns = start.elapsed().as_nanos() as u64;
        let volume: u64 = result.match_result.fills.iter().map(|f| f.qty as u64).sum();
        self.perf.record_order(result.match_result.fills.len(), volume, elapsed_ns);

        // Build a Python dict for interactive use.
        let dict = PyDict::new(py);
        dict.set_item("order_id", result.match_result.order_id)?;
        dict.set_item("resting_qty", result.match_result.resting_qty)?;
        dict.set_item("latency_ns", elapsed_ns)?;

        let fills_list = PyList::empty(py);
        for fill in &result.match_result.fills {
            let fill_dict = PyDict::new(py);
            fill_dict.set_item("maker_order_id", fill.maker_order_id)?;
            fill_dict.set_item("taker_order_id", fill.taker_order_id)?;
            fill_dict.set_item("maker_trader_id", fill.maker_trader_id)?;
            fill_dict.set_item("taker_trader_id", fill.taker_trader_id)?;
            fill_dict.set_item("price", fill.price as f64 / SCALE as f64)?;
            fill_dict.set_item("price_raw", fill.price)?;
            fill_dict.set_item("qty", fill.qty)?;
            fills_list.append(fill_dict)?;
        }
        dict.set_item("fills", fills_list)?;

        Ok(dict)
    }

    // -------------------------------------------------------------------
    // MARKET DATA
    // -------------------------------------------------------------------

    /// Get an L2 (Market-By-Price) order book snapshot.
    ///
    /// Args:
    ///     depth (int): Number of price levels per side.
    ///
    /// Returns:
    ///     dict: { "bids": bytes, "asks": bytes, "bid_count": int, "ask_count": int }
    ///     Each side is packed as 16-byte records: [8:price][4:qty][4:orders].
    fn get_l2_snapshot<'py>(&self, py: Python<'py>, depth: usize) -> PyResult<Bound<'py, PyDict>> {
        let (bids, asks) = self.inner.l2_snapshot(depth);

        let dict = PyDict::new(py);
        dict.set_item("bids", PyBytes::new(py, &serialize_l2_levels(&bids)))?;
        dict.set_item("asks", PyBytes::new(py, &serialize_l2_levels(&asks)))?;
        dict.set_item("bid_count", bids.len())?;
        dict.set_item("ask_count", asks.len())?;

        if let Some(bb) = self.inner.engine.best_bid() {
            dict.set_item("best_bid", bb as f64 / SCALE as f64)?;
        }
        if let Some(ba) = self.inner.engine.best_ask() {
            dict.set_item("best_ask", ba as f64 / SCALE as f64)?;
        }

        Ok(dict)
    }

    /// Get a human-readable L2 snapshot (for dashboards).
    fn get_l2_human<'py>(&self, py: Python<'py>, depth: usize) -> PyResult<Bound<'py, PyDict>> {
        let (bids, asks) = self.inner.l2_snapshot(depth);

        let dict = PyDict::new(py);

        let bid_list = PyList::empty(py);
        for level in &bids {
            let d = PyDict::new(py);
            d.set_item("price", level.price as f64 / SCALE as f64)?;
            d.set_item("qty", level.qty)?;
            d.set_item("orders", level.order_count)?;
            bid_list.append(d)?;
        }

        let ask_list = PyList::empty(py);
        for level in &asks {
            let d = PyDict::new(py);
            d.set_item("price", level.price as f64 / SCALE as f64)?;
            d.set_item("qty", level.qty)?;
            d.set_item("orders", level.order_count)?;
            ask_list.append(d)?;
        }

        dict.set_item("bids", bid_list)?;
        dict.set_item("asks", ask_list)?;

        Ok(dict)
    }

    // -------------------------------------------------------------------
    // RECOVERY
    // -------------------------------------------------------------------

    /// Recover exchange state from the WAL file.
    ///
    /// Call this after creating the exchange with a WAL path.
    /// Pre-load trader accounts with add_funds() BEFORE calling recover().
    ///
    /// Returns:
    ///     int: Number of WAL entries replayed.
    fn recover(&mut self) -> usize {
        self.inner.recover_from_wal()
    }

    // -------------------------------------------------------------------
    // RISK MANAGEMENT
    // -------------------------------------------------------------------

    /// Ban a trader (Kill Switch). All future orders rejected in O(1).
    fn ban_trader(&mut self, trader_id: u32) {
        self.inner.ban_trader(trader_id);
    }

    /// Cancel all orders for a disconnected trader.
    fn cancel_on_disconnect(&mut self, trader_id: u32) -> Vec<u64> {
        self.inner.cancel_on_disconnect(trader_id)
    }

    /// Set the volatility band percentage (e.g., 0.10 for 10%).
    fn set_volatility_band(&mut self, pct: f64) {
        self.inner.guardian.set_volatility_band_pct(pct);
    }

    // -------------------------------------------------------------------
    // PERFORMANCE METRICS (The Pulse)
    // -------------------------------------------------------------------

    /// Get performance metrics for the dashboard.
    ///
    /// Returns:
    ///     dict: {
    ///         "total_orders": int,
    ///         "total_fills": int, 
    ///         "total_volume": int,
    ///         "avg_match_latency_ns": int,
    ///         "last_match_latency_ns": int,
    ///         "wal_entries": int,
    ///         "wal_bytes_used": int,
    ///         "best_bid": float | None,
    ///         "best_ask": float | None,
    ///         "spread": float | None,
    ///     }
    fn get_performance_metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);

        dict.set_item("total_orders", self.perf.total_orders)?;
        dict.set_item("total_fills", self.perf.total_fills)?;
        dict.set_item("total_volume", self.perf.total_volume)?;
        dict.set_item("avg_match_latency_ns", self.perf.avg_latency_ns())?;
        dict.set_item("last_match_latency_ns", self.perf.last_match_latency_ns)?;

        // WAL metrics.
        if let Some(ref sentinel) = self.inner.sentinel {
            dict.set_item("wal_entries", sentinel.entry_count())?;
            dict.set_item("wal_bytes_used", sentinel.write_pos())?;
        } else {
            dict.set_item("wal_entries", 0u64)?;
            dict.set_item("wal_bytes_used", 0usize)?;
        }

        // Book metrics.
        let best_bid = self.inner.engine.best_bid();
        let best_ask = self.inner.engine.best_ask();

        match best_bid {
            Some(b) => dict.set_item("best_bid", b as f64 / SCALE as f64)?,
            None => dict.set_item("best_bid", py.None())?,
        }
        match best_ask {
            Some(a) => dict.set_item("best_ask", a as f64 / SCALE as f64)?,
            None => dict.set_item("best_ask", py.None())?,
        }
        match (best_bid, best_ask) {
            (Some(b), Some(a)) => dict.set_item("spread", (a - b) as f64 / SCALE as f64)?,
            _ => dict.set_item("spread", py.None())?,
        }

        Ok(dict)
    }

    // -------------------------------------------------------------------
    // ACCOUNT QUERY
    // -------------------------------------------------------------------

    /// Get a trader's account state.
    ///
    /// Returns:
    ///     dict | None: { "available_balance": float, "locked_margin": float,
    ///                     "total_equity": float, "position": int }
    fn get_account<'py>(&self, py: Python<'py>, trader_id: u32) -> PyResult<Option<Bound<'py, PyDict>>> {
        match self.inner.guardian.get_account(trader_id) {
            Some(account) => {
                let dict = PyDict::new(py);
                dict.set_item("available_balance", account.available_balance as f64 / SCALE as f64)?;
                dict.set_item("locked_margin", account.locked_margin as f64 / SCALE as f64)?;
                dict.set_item("total_equity", account.total_equity() as f64 / SCALE as f64)?;
                dict.set_item("position", account.position(self.inner.symbol_id))?;
                dict.set_item("realized_pnl", account.realized_pnl as f64 / SCALE as f64)?;
                Ok(Some(dict))
            }
            None => Ok(None),
        }
    }

    // -------------------------------------------------------------------
    // UTILITY CONSTANTS
    // -------------------------------------------------------------------

    /// Get the fixed-point scale factor.
    #[staticmethod]
    fn scale() -> i64 {
        SCALE
    }

    /// Convert a float price to fixed-point raw value.
    #[staticmethod]
    fn price_to_raw(price: f64) -> i64 {
        (price * SCALE as f64).round() as i64
    }

    /// Convert a fixed-point raw value to float price.
    #[staticmethod]
    fn raw_to_price(raw: i64) -> f64 {
        raw as f64 / SCALE as f64
    }
}

// ---------------------------------------------------------------------------
// Module Registration
// ---------------------------------------------------------------------------

/// Register all Python-exposed types and the PyNexusExchange class.
pub fn register_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNexusExchange>()?;
    m.add_class::<crate::Price>()?;
    m.add_class::<crate::Quantity>()?;
    m.add_class::<crate::Side>()?;
    m.add("SCALE", SCALE)?;
    m.add("FILL_RECORD_SIZE", FILL_RECORD_SIZE)?;
    m.add("L2_LEVEL_SIZE", L2_LEVEL_SIZE)?;
    Ok(())
}
