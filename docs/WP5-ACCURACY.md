# WP5 — meter accuracy, measured on the target

**Question:** does the billed byte count match what the server actually carried?
**Answer: yes — the counter bills L3 wire bytes (payload + TCP/IP headers), which is the correct
unit for a byte-metered tunnel. There is no percentage drift and no overcharge.**

Target: `testserver2`, kernel `6.12.113+deb13-amd64`, nftables `v1.1.3`.
Run under the corrected chain shape (`policy accept`, `quota drop` then `counter accept`).

## The finding that mattered

A single 262,144-byte payload made the counter read **262,568** — 424 bytes higher. Read naively that
is a 0.16 % "overcharge", and a harness that stopped there would have reported either a bug or a
tolerance and moved on. Scaling the payload instead shows the delta is **not a percentage at all**:

| payload | counter | delta |
|---|---|---|
| 1 | 269 | **268** |
| 100 | 368 | **268** |
| 1,000 | 1,268 | **268** |
| 10,000 | 10,268 | **268** |
| 100,000 | 100,372 | **372** |

The delta is a **flat 268 bytes**: 20 IP + 20 TCP header per packet, plus timestamp options, plus the
SYN/FIN/ACK control packets. It stays constant as the payload grows by five orders of magnitude, and
only moves when a second packet is needed (100,000 bytes needs two segments, hence 372).

**So the counter bills wire bytes including headers.** For a byte-metered tunnel that is the correct
unit — the header is real bandwidth the server carried. It is also what makes the meter *conservative
for the operator and still fair to the customer*: the overhead is fixed per connection, not
proportional, so it cannot scale with usage.

A harness that had asserted "counter == payload exactly" would have **failed a correct meter**. That
assertion is now documented as wrong rather than silently relaxed.

## Pricing arithmetic

With `bytes_per_sat = 1048576` and 262,568 bytes carried: `sats_due = 0`, remainder 262,568 (25 % of
one sat).

**The remainder is carried forward, not lost.** Billing divides *cumulative* bytes, and the counter is
monotonic across re-arms, so no fraction is dropped. This matters: a per-tick `floor()` on each delta
would lose up to 1 sat per tick per client — at 1 Hz that is **86,400 sats/day** per client thrown
away. The daemon therefore bills the cumulative counter, never the tick delta.

## The overcharge check

`quota.used` was compared against `counter.bytes`. In the policing experiment (384-byte budget, ten
128-byte datagrams) the counter read `packets 3 bytes 384` — delivered — while the quota read
`used 384` on a budget that had also absorbed a datagram that was **dropped**. Billing from
`quota.used` charges for traffic the client never received.

**Rule: the quota POLICES, the counter BILLS.** `tg_meter.py --read` exposes `policed_used` for
transparency only, and `library/tg_meterd.py` never reads it.

## Shape gate

The harness refuses to measure unless `tg_meter.py --verify` passes, because a wrong shape makes every
number meaningless:
- accept-policy chain → free tunnel (nothing billed, everything "works")
- accept rule ordered before the quota rule → free tunnel
- egress-keyed metered rule → bills the server's internet side

Verified both directions on the target: `--verify` returns `{"ok": true}` on the live corrected table
and **rejects** a deliberately-built `policy drop` table with a named reason.

## Reproduce

```
scp wp5-accuracy.sh vps2fips:/tmp/wp5.sh
ssh vps2fips 'sudo -n bash /tmp/wp5.sh'
```

Identifiers and thresholds are variables (`GA_TABLE`, `GA_CLIENT`, `GA_BUDGET`, `GA_BYTES`,
`GA_TOLERANCE_PCT`, `GA_STATE_DIR`). The harness provisions its own client, zeroes the counter,
measures, and **removes only its own objects** — the ceiling and any other client are untouched
(verified: production tables and `ip tgmeter` intact afterwards).
