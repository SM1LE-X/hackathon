// nexus_core/src/lib.rs
//
// Nexus Core — The root library crate.
//
// Pure Rust:  cargo test
// Python:    maturin develop --features python

pub mod types;
pub mod wire;
pub mod matching;
pub mod risk;
pub mod persistence;

#[cfg(feature = "python")]
pub mod python;

pub use types::{Price, Quantity, Side, SCALE};
pub use wire::messages::{
    MessageHeader, NewOrder, OrderCancel, TradeUpdate,
    msg_type, order_type, tif,
};
pub use matching::{MatchingEngine, OrderBook, Fill, MatchResult, L2Level, RejectReason, RiskConfig};
pub use risk::{Guardian, Account, GuardianConfig, GuardianReject, VolatilityBandConfig};
pub use persistence::{Sentinel, NexusExchange, ExchangeResult, ExchangeError, JournalHeader};

/// The PyO3 module entry point.
/// Compiled only with the `python` feature via maturin.
#[cfg(feature = "python")]
use pyo3::prelude::*;

#[cfg(feature = "python")]
#[pymodule]
fn nexus_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    python::register_module(m)?;
    Ok(())
}
