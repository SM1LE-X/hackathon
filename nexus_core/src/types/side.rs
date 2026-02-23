// nexus_core/src/types/side.rs
//
// The ONE TRUE Side Enum.
//
// WHY THIS EXISTS:
// The Python codebase had TWO conflicting Side enums:
//   - models.py:          Side("BUY", "SELL")  — uppercase strings
//   - message_schemas.py: Side("buy", "sell")  — lowercase strings
// This Rust enum kills that bug permanently. 1 byte, 1 instruction compare.

#[cfg(feature = "python")]
use pyo3::prelude::*;
use std::fmt;

/// Order side: Buy or Sell. Represented as a single byte (`u8`).
#[cfg_attr(feature = "python", pyclass(eq, eq_int))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Side {
    Buy = 1,
    Sell = 2,
}

#[cfg_attr(feature = "python", pymethods)]
impl Side {
    /// Parse from a string (case-insensitive).
    #[cfg_attr(feature = "python", staticmethod)]
    pub fn from_str(s: &str) -> Result<Self, String> {
        match s.to_ascii_lowercase().as_str() {
            "buy" => Ok(Side::Buy),
            "sell" => Ok(Side::Sell),
            _ => Err("Side must be 'buy' or 'sell'".to_string()),
        }
    }

    /// The opposite side.
    pub fn opposite(&self) -> Side {
        match self {
            Side::Buy => Side::Sell,
            Side::Sell => Side::Buy,
        }
    }

    /// Sign multiplier: Buy = +1, Sell = -1.
    pub fn sign(&self) -> i32 {
        match self {
            Side::Buy => 1,
            Side::Sell => -1,
        }
    }

    /// Raw u8 value for binary serialization.
    pub fn as_u8(&self) -> u8 {
        *self as u8
    }

    /// Reconstruct from raw u8.
    #[cfg_attr(feature = "python", staticmethod)]
    pub fn from_u8(value: u8) -> Result<Self, String> {
        match value {
            1 => Ok(Side::Buy),
            2 => Ok(Side::Sell),
            _ => Err(format!("Invalid Side byte: {}. Must be 1 (Buy) or 2 (Sell)", value)),
        }
    }
}

impl fmt::Display for Side {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Side::Buy => write!(f, "Buy"),
            Side::Sell => write!(f, "Sell"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_side_size_is_one_byte() {
        assert_eq!(std::mem::size_of::<Side>(), 1);
    }

    #[test]
    fn test_side_from_str_case_insensitive() {
        assert_eq!(Side::from_str("buy").unwrap(), Side::Buy);
        assert_eq!(Side::from_str("BUY").unwrap(), Side::Buy);
        assert_eq!(Side::from_str("Buy").unwrap(), Side::Buy);
        assert_eq!(Side::from_str("sell").unwrap(), Side::Sell);
        assert_eq!(Side::from_str("SELL").unwrap(), Side::Sell);
    }

    #[test]
    fn test_side_roundtrip_u8() {
        assert_eq!(Side::from_u8(Side::Buy.as_u8()).unwrap(), Side::Buy);
        assert_eq!(Side::from_u8(Side::Sell.as_u8()).unwrap(), Side::Sell);
        assert!(Side::from_u8(0).is_err());
        assert!(Side::from_u8(3).is_err());
    }

    #[test]
    fn test_side_opposite() {
        assert_eq!(Side::Buy.opposite(), Side::Sell);
        assert_eq!(Side::Sell.opposite(), Side::Buy);
    }

    #[test]
    fn test_side_sign() {
        assert_eq!(Side::Buy.sign(), 1);
        assert_eq!(Side::Sell.sign(), -1);
    }
}
