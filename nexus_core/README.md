# Nexus Core

Nexus Core is the high-performance, production-grade trading engine for OpenSim, written in Rust. It replaces the current Python prototype with an institutional-grade architecture designed for microsecond latency, strict determinism, and robust crash recovery.

## Architecture Overview

Nexus follows a **"Log-Then-Act" Disruptor Pipeline** model:
- **Zero-Copy IPC**: Components communicate over single-producer single-consumer (SPSC) shared memory ring buffers. No internal TCP overhead.
- **Binary Wire Protocol**: All messages use Simple Binary Encoding (SBE) with packed C-structs (`#[repr(C, packed)]`) for zero-allocation parsing.
- **Deterministic State Machine**: Every input is sequenced and fully deterministic.
- **Memory-Mapped WAL**: Provides persistence using an mmap-backed Write-Ahead Log, bypassing syscall overhead on the hot path.
- **Fixed-Point Math**: IEEE 754 floats are banned. All prices and quantities use exact `i64`/`u32` integer arithmetic scaled to 8 decimal places.

---

## Progress Tracking

### 🟢 Implemented Modules (Done)
- [x] **`types::fixed_point`**: Core math library. `Price` and `Quantity` wrappers with zero heap allocations and operator overloading for financial precision.
- [x] **`wire::messages`**: Binary serialization structs including `MessageHeader`, `NewOrder`, `OrderCancel`, and `TradeUpdate`.
- [x] **`persistence::sentinel`**: The memory-mapped Write-Ahead Log capable of writing entries in ~50ns and handling complete deterministic crash recovery.
- [x] **`matching::orderbook`**: The $O(1)$ limit order book backed by a `BTreeMap<i64, VecDeque<Order>>`.
- [x] **`risk::guardian`**: The Pre-Trade Risk Gate managing user balances, fat-finger prevention, margin locks, and the Kill Switch.

### 🟡 In Progress / Next Up
- [ ] **`matching::engine`**: The central loop connecting the Pre-Trade Guardian with the Limit Order Book.
- [ ] **Ring Buffer IPC (`wire::ring_buffer`)**: Lock-free SPSC circular queues for passing messages between processing threads.

### 🔴 Future Expectations (To Do)
- [ ] **`gateway::tcp`**: High-performance raw TCP socket ingress (replacing Python `server.py`).
- [ ] **`egress::multicast`**: UDP multicast pipeline for broadcasting L2 Market Data (BBO) to external clients.
- [ ] **`main.rs` (Wiring & CPU Pinning)**: The process bootstrapping to allocate ring buffers and pin threads to isolated CPU cores.

---
*This document tracks the incremental rewrite of the OpenSim engine as outlined in [`NEXUS_ARCH.md`](NEXUS_ARCH.md).*
