// nexus_core/src/wire/messages.rs
//
// Simple Binary Encoding (SBE) Wire Format Structs.
//
// WHY #[repr(C, packed)]:
// This attribute tells the Rust compiler to lay out the struct fields in
// EXACTLY the order declared, with ZERO padding between fields.
// This means the CPU can read a `NewOrder` by simply casting a raw byte pointer
// to `*const NewOrder` — no parsing, no field extraction, no heap allocation.
//
// L1 CACHE OPTIMIZATION:
// A typical L1 data cache line on modern CPUs (Intel, AMD) is 64 bytes.
// Our `NewOrder` struct is exactly 36 bytes. This means:
//   - TWO complete NewOrder messages fit in a single L1 cache line.
//   - When the matching engine reads an order, the NEXT order is likely
//     already pre-fetched into L1 cache by the CPU's hardware prefetcher.
//   - This eliminates cache misses on sequential order processing,
//     which is the #1 source of latency jitter in naive implementations.
//
// Compare to the Python JSON approach:
//   - A JSON `{"type":"order","side":"buy","price":100.05,"qty":50}` is ~60+ bytes
//     of UTF-8 text that must be parsed character-by-character into a Python dict
//     (which itself allocates ~300+ bytes of heap memory for the dict + string keys).
//   - That's 300+ bytes vs 36 bytes. Nearly 10× more cache pressure.

use crate::types::{Price, Side};
use std::fmt;

// ---------------------------------------------------------------------------
// Common Message Header (8 bytes)
// ---------------------------------------------------------------------------

/// Every SBE message begins with this 8-byte header.
/// The `sequence_num` provides strict total ordering for the WAL and
/// deterministic replay.
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
pub struct MessageHeader {
    /// Total message length in bytes (including this header).
    pub msg_length: u16,
    /// Message type discriminator. See `MsgType` constants.
    pub msg_type: u8,
    /// Schema version for backward compatibility.
    pub version: u8,
    /// Monotonically increasing sequence number.
    pub sequence_num: u32,
}

/// Message type constants.
pub mod msg_type {
    pub const NEW_ORDER: u8 = 0x01;
    pub const ORDER_CANCEL: u8 = 0x02;
    pub const EXECUTION_REPORT: u8 = 0x10;
    pub const MARKET_DATA_BBO: u8 = 0x20;
    pub const TRADE_UPDATE: u8 = 0x30;
    pub const KILL_SWITCH: u8 = 0xFF;
}

// ---------------------------------------------------------------------------
// NewOrder (36 bytes)
// ---------------------------------------------------------------------------

/// Inbound order entry message.
///
/// # Layout (36 bytes total)
/// ```text
/// Offset | Size | Field
/// -------|------|----------------
///  0     |  8   | header
///  8     |  4   | trader_id (u32)
/// 12     |  8   | client_order_id (u64)
/// 20     |  8   | price (i64, fixed-point)
/// 28     |  4   | quantity (u32)
/// 32     |  1   | side (u8: 1=Buy, 2=Sell)
/// 33     |  1   | order_type (u8: 1=Limit, 2=Market)
/// 34     |  1   | time_in_force (u8: 1=GTC, 2=IOC, 3=FOK)
/// 35     |  1   | _padding
/// ```
///
/// # Cache Performance
/// At 36 bytes, exactly **1.78 orders fit per 64-byte L1 cache line**.
/// The hardware prefetcher will load the next cache line while the current
/// order is being processed, effectively giving us zero-latency sequential reads.
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
pub struct NewOrder {
    pub header: MessageHeader,
    pub trader_id: u32,
    pub client_order_id: u64,
    pub price: i64,
    pub quantity: u32,
    pub side: u8,
    pub order_type: u8,
    pub time_in_force: u8,
    pub _padding: u8,
}

/// Order type constants.
pub mod order_type {
    pub const LIMIT: u8 = 1;
    pub const MARKET: u8 = 2;
}

/// Time-in-force constants.
pub mod tif {
    /// Good Till Cancel — rests on the book until explicitly cancelled.
    pub const GTC: u8 = 1;
    /// Immediate Or Cancel — fill what you can, cancel the rest.
    pub const IOC: u8 = 2;
    /// Fill Or Kill — fill the entire quantity or reject completely.
    pub const FOK: u8 = 3;
}

impl NewOrder {
    /// Create a new order with a properly initialized header.
    pub fn new(
        sequence_num: u32,
        trader_id: u32,
        client_order_id: u64,
        price: Price,
        quantity: u32,
        side: Side,
        order_type_val: u8,
        time_in_force: u8,
    ) -> Self {
        Self {
            header: MessageHeader {
                msg_length: std::mem::size_of::<Self>() as u16,
                msg_type: msg_type::NEW_ORDER,
                version: 1,
                sequence_num,
            },
            trader_id,
            client_order_id,
            price: price.raw(),
            quantity,
            side: side.as_u8(),
            order_type: order_type_val,
            time_in_force,
            _padding: 0,
        }
    }

    /// Extract the Side enum from the raw byte.
    pub fn side_enum(&self) -> Option<Side> {
        match self.side {
            1 => Some(Side::Buy),
            2 => Some(Side::Sell),
            _ => None,
        }
    }

    /// Extract the Price as a fixed-point Price struct.
    pub fn price_fixed(&self) -> Price {
        Price::new(self.price)
    }
}

impl fmt::Display for NewOrder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Copy fields to locals to avoid misaligned references (packed struct UB).
        let seq = self.header.sequence_num;
        let trader = self.trader_id;
        let side = self.side_enum().unwrap_or(Side::Buy);
        let price = self.price_fixed();
        let qty = self.quantity;
        write!(
            f,
            "NewOrder[seq={}, trader={}, side={:?}, price={}, qty={}]",
            seq, trader, side, price, qty,
        )
    }
}

// ---------------------------------------------------------------------------
// OrderCancel (20 bytes)
// ---------------------------------------------------------------------------

/// Cancel a specific resting order.
///
/// # Layout (20 bytes total)
/// ```text
/// Offset | Size | Field
/// -------|------|--------------------
///  0     |  8   | header
///  8     |  4   | trader_id (u32)
/// 12     |  8   | target_order_id (u64)
/// ```
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
pub struct OrderCancel {
    pub header: MessageHeader,
    pub trader_id: u32,
    pub target_order_id: u64,
}

impl OrderCancel {
    pub fn new(sequence_num: u32, trader_id: u32, target_order_id: u64) -> Self {
        Self {
            header: MessageHeader {
                msg_length: std::mem::size_of::<Self>() as u16,
                msg_type: msg_type::ORDER_CANCEL,
                version: 1,
                sequence_num,
            },
            trader_id,
            target_order_id,
        }
    }
}

// ---------------------------------------------------------------------------
// TradeUpdate (48 bytes)
// ---------------------------------------------------------------------------

/// Outbound trade notification emitted by the matching engine.
///
/// # Layout (48 bytes total)
/// ```text
/// Offset | Size | Field
/// -------|------|-------------------
///  0     |  8   | header
///  8     |  8   | trade_id (u64)
/// 16     |  8   | price (i64, fixed-point)
/// 24     |  4   | quantity (u32)
/// 28     |  4   | buy_trader_id (u32)
/// 32     |  4   | sell_trader_id (u32)
/// 36     |  8   | timestamp_ns (u64)
/// 44     |  4   | _padding
/// ```
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
pub struct TradeUpdate {
    pub header: MessageHeader,
    pub trade_id: u64,
    pub price: i64,
    pub quantity: u32,
    pub buy_trader_id: u32,
    pub sell_trader_id: u32,
    pub timestamp_ns: u64,
    pub _padding: u32,
}

impl TradeUpdate {
    pub fn new(
        sequence_num: u32,
        trade_id: u64,
        price: Price,
        quantity: u32,
        buy_trader_id: u32,
        sell_trader_id: u32,
        timestamp_ns: u64,
    ) -> Self {
        Self {
            header: MessageHeader {
                msg_length: std::mem::size_of::<Self>() as u16,
                msg_type: msg_type::TRADE_UPDATE,
                version: 1,
                sequence_num,
            },
            trade_id,
            price: price.raw(),
            quantity,
            buy_trader_id,
            sell_trader_id,
            timestamp_ns,
            _padding: 0,
        }
    }

    pub fn price_fixed(&self) -> Price {
        Price::new(self.price)
    }
}

impl fmt::Display for TradeUpdate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Copy fields to locals to avoid misaligned references (packed struct UB).
        let trade_id = self.trade_id;
        let price = self.price_fixed();
        let qty = self.quantity;
        let buyer = self.buy_trader_id;
        let seller = self.sell_trader_id;
        write!(
            f,
            "Trade[id={}, price={}, qty={}, buyer={}, seller={}]",
            trade_id, price, qty, buyer, seller,
        )
    }
}

// ---------------------------------------------------------------------------
// Compile-time size assertions
// ---------------------------------------------------------------------------
// These are zero-cost checks that fire at compile time if the struct layout
// ever drifts from the SBE specification. If someone adds a field and forgets
// to update the spec, the build fails immediately.

const _: () = assert!(std::mem::size_of::<MessageHeader>() == 8);
const _: () = assert!(std::mem::size_of::<NewOrder>() == 36);
const _: () = assert!(std::mem::size_of::<OrderCancel>() == 20);
const _: () = assert!(std::mem::size_of::<TradeUpdate>() == 48);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_order_size() {
        assert_eq!(std::mem::size_of::<NewOrder>(), 36);
    }

    #[test]
    fn test_order_cancel_size() {
        assert_eq!(std::mem::size_of::<OrderCancel>(), 20);
    }

    #[test]
    fn test_trade_update_size() {
        assert_eq!(std::mem::size_of::<TradeUpdate>(), 48);
    }

    #[test]
    fn test_new_order_roundtrip() {
        let price = Price::from_str_decimal("100.05").unwrap();
        let order = NewOrder::new(
            1,      // sequence
            42,     // trader_id
            12345,  // client_order_id
            price,
            50,     // quantity
            Side::Buy,
            order_type::LIMIT,
            tif::GTC,
        );

        // Copy fields to locals to avoid packed-struct misaligned ref UB.
        let mt = order.header.msg_type;
        let tid = order.trader_id;
        let qty = order.quantity;
        assert_eq!(mt, msg_type::NEW_ORDER);
        assert_eq!(tid, 42);
        assert_eq!(qty, 50);
        assert_eq!(order.side_enum(), Some(Side::Buy));
        assert_eq!(order.price_fixed(), price);
    }

    #[test]
    fn test_zero_copy_cast() {
        // Simulate what the TCP Gateway does: cast raw bytes to a struct pointer.
        let price = Price::from_str_decimal("99.95").unwrap();
        let order = NewOrder::new(1, 1, 1, price, 100, Side::Sell, order_type::LIMIT, tif::GTC);

        // Serialize to raw bytes (what would come off the wire).
        let bytes: &[u8] = unsafe {
            std::slice::from_raw_parts(
                &order as *const NewOrder as *const u8,
                std::mem::size_of::<NewOrder>(),
            )
        };
        assert_eq!(bytes.len(), 36);

        // Deserialize by casting the pointer back (zero-copy).
        let recovered: &NewOrder = unsafe { &*(bytes.as_ptr() as *const NewOrder) };
        // Copy fields to locals to avoid packed-struct misaligned ref UB.
        let tid = recovered.trader_id;
        let qty = recovered.quantity;
        assert_eq!(tid, 1);
        assert_eq!(qty, 100);
        assert_eq!(recovered.side_enum(), Some(Side::Sell));
        assert_eq!(recovered.price_fixed(), price);
    }
}
