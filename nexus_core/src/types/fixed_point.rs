// nexus_core/src/types/fixed_point.rs
//
// Fixed-Point Arithmetic for Financial Math.
//
// WHY THIS IS FASTER:
// IEEE 754 floats cannot represent 0.1 exactly (it becomes 0.1000000000000000055...).
// Over millions of trades, this drift causes real financial losses.
// Fixed-point uses a 64-bit integer scaled by 10^8, giving us 8 decimal places
// of precision with ZERO rounding error on addition and subtraction.
//
// WHY NO HEAP ALLOCATION:
// `Price` and `Quantity` are both `#[derive(Copy, Clone)]`. They live entirely
// on the stack or inside pre-allocated slab arrays. The matching engine will
// never call `malloc` for a Price value.

#[cfg(feature = "python")]
use pyo3::prelude::*;
use std::fmt;

/// Scale factor: 10^8. All prices are stored as `raw_value = human_price * SCALE`.
///
/// Example: $100.05 → `10_005_000_000i64`
pub const SCALE: i64 = 100_000_000;

/// Fixed-point price representation.
///
/// Internally stored as `i64` scaled by `SCALE` (10^8).
/// Supports exact addition, subtraction, and notional computation.
///
/// # Memory Layout
/// Exactly 8 bytes. Fits in a single CPU register. No heap.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Price {
    raw: i64,
}

#[cfg_attr(feature = "python", pymethods)]
impl Price {
    /// Create a Price from a raw integer value (already scaled by 10^8).
    #[cfg_attr(feature = "python", new)]
    pub fn new(raw: i64) -> Self {
        Self { raw }
    }

    /// Create a Price from a human-readable float string (e.g., "100.05").
    /// This is the ONLY place float-to-fixed conversion happens.
    /// After this, all math is pure integer.
    #[cfg_attr(feature = "python", staticmethod)]
    pub fn from_str_decimal(s: &str) -> Result<Self, String> {
        let trimmed = s.trim();
        let parts: Vec<&str> = trimmed.split('.').collect();
        if parts.is_empty() || parts.len() > 2 {
            return Err("Invalid price string format".to_string());
        }

        let integer_part: i64 = parts[0]
            .parse()
            .map_err(|_| "Invalid integer part".to_string())?;

        let fractional_raw: i64 = if parts.len() == 2 && !parts[1].is_empty() {
            let frac_str = parts[1];
            let frac_digits = frac_str.len();
            if frac_digits > 8 {
                return Err("Max 8 decimal places supported".to_string());
            }
            let frac_val: i64 = frac_str
                .parse()
                .map_err(|_| "Invalid fractional part".to_string())?;
            let multiplier = 10i64.pow((8 - frac_digits) as u32);
            frac_val * multiplier
        } else {
            0
        };

        let sign = if integer_part < 0 { -1i64 } else { 1i64 };
        let raw = integer_part * SCALE + sign * fractional_raw;
        Ok(Self { raw })
    }

    /// Create a Price from a floating point value.
    /// WARNING: Use `from_str_decimal` when possible.
    #[cfg_attr(feature = "python", staticmethod)]
    pub fn from_float(value: f64) -> Self {
        Self {
            raw: (value * SCALE as f64).round() as i64,
        }
    }

    /// The raw i64 value (scaled by 10^8).
    pub fn raw(&self) -> i64 {
        self.raw
    }

    /// Convert to human-readable float for display / Python interop.
    pub fn to_float(&self) -> f64 {
        self.raw as f64 / SCALE as f64
    }

    /// Compute notional value: price × quantity. Exact integer math.
    pub fn notional(&self, qty: u32) -> i64 {
        self.raw * (qty as i64)
    }

    /// Weighted average of two prices (integer division truncates).
    #[cfg_attr(feature = "python", staticmethod)]
    pub fn weighted_avg(old_avg: &Price, old_qty: u32, new_price: &Price, new_qty: u32) -> Price {
        let total_qty = old_qty as i64 + new_qty as i64;
        if total_qty == 0 {
            return Price { raw: 0 };
        }
        let raw = (old_avg.raw * old_qty as i64 + new_price.raw * new_qty as i64) / total_qty;
        Price { raw }
    }

    /// Midpoint of two prices (integer division truncates).
    pub fn midpoint(&self, other: &Price) -> Price {
        Price {
            raw: (self.raw + other.raw) / 2,
        }
    }
}

impl std::ops::Add for Price {
    type Output = Price;
    fn add(self, rhs: Price) -> Price {
        Price { raw: self.raw + rhs.raw }
    }
}

impl std::ops::Sub for Price {
    type Output = Price;
    fn sub(self, rhs: Price) -> Price {
        Price { raw: self.raw - rhs.raw }
    }
}

impl fmt::Display for Price {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let integer_part = self.raw / SCALE;
        let fractional_part = (self.raw % SCALE).unsigned_abs();
        write!(f, "{}.{:08}", integer_part, fractional_part)
    }
}

/// Fixed-point quantity. Exactly 4 bytes. Fits in a single register.
#[cfg_attr(feature = "python", pyclass)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Quantity {
    raw: u32,
}

#[cfg_attr(feature = "python", pymethods)]
impl Quantity {
    #[cfg_attr(feature = "python", new)]
    pub fn new(raw: u32) -> Self {
        Self { raw }
    }

    pub fn raw(&self) -> u32 {
        self.raw
    }

    pub fn is_zero(&self) -> bool {
        self.raw == 0
    }
}

impl fmt::Display for Quantity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.raw)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_price_from_str_decimal() {
        let p = Price::from_str_decimal("100.05").unwrap();
        assert_eq!(p.raw(), 10_005_000_000);
    }

    #[test]
    fn test_price_from_str_integer() {
        let p = Price::from_str_decimal("100").unwrap();
        assert_eq!(p.raw(), 10_000_000_000);
    }

    #[test]
    fn test_price_display() {
        let p = Price::new(10_005_000_000);
        assert_eq!(format!("{}", p), "100.05000000");
    }

    #[test]
    fn test_price_addition_exact() {
        // 0.1 + 0.2 must equal 0.3 EXACTLY. Floats cannot do this.
        let a = Price::from_str_decimal("0.1").unwrap();
        let b = Price::from_str_decimal("0.2").unwrap();
        let c = Price::from_str_decimal("0.3").unwrap();
        let sum = a + b;
        assert_eq!(sum, c, "Fixed-point: 0.1 + 0.2 == 0.3 exactly");
    }

    #[test]
    fn test_notional_exact() {
        let price = Price::from_str_decimal("100.00").unwrap();
        let notional = price.notional(50);
        let expected = Price::from_str_decimal("5000.00").unwrap().raw();
        assert_eq!(notional, expected);
    }

    #[test]
    fn test_weighted_avg() {
        let old_avg = Price::from_str_decimal("100.00").unwrap();
        let new_price = Price::from_str_decimal("102.00").unwrap();
        let result = Price::weighted_avg(&old_avg, 10, &new_price, 10);
        let expected = Price::from_str_decimal("101.00").unwrap();
        assert_eq!(result, expected);
    }

    #[test]
    fn test_midpoint() {
        let bid = Price::from_str_decimal("99.50").unwrap();
        let ask = Price::from_str_decimal("100.50").unwrap();
        let mid = bid.midpoint(&ask);
        let expected = Price::from_str_decimal("100.00").unwrap();
        assert_eq!(mid, expected);
    }

    #[test]
    fn test_price_from_float_round_trip() {
        let p = Price::from_float(99.95);
        assert_eq!(p.raw(), 9_995_000_000);
        assert!((p.to_float() - 99.95).abs() < 1e-10);
    }
}
