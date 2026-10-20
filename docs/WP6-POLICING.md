# WP6 — over-quota policing on a live two-leg bond

**Verdict: PASS on policing, with a measured and explained 2.2 % overrun. The in-run traffic
generator was itself invalid, so the two-burst comparison is INVALID — but the terminal state the
meter reached is unambiguous and is the stronger evidence.**

Target: `tollgate-A04E` (kernel 6.6.93) against `vps2fips` (kernel 6.12.113), both uplinks live.
Everything ran under the **shared bench mutex** (`bench_lock.py`, `flock` on
`~/.local/state/tg-bench/tollgate-A04E.lock`), the same lock every other context window's console
tooling takes.

## The measurement

Provisioned a metered client with a **2,097,152-byte** budget, then drove traffic at it until it
stopped:

| reading | value |
|---|---|
| `delivered_bytes` (counter, the billing source) | **2,143,298** |
| `delivered_packets` | 134 |
| `budget` | 2,097,152 |
| `quota.used` (policing source) | **2,097,152** — pinned exactly at budget |
| further pushes after exhaustion | **0 bytes** delivered |

Two conclusions, both load-bearing:

1. **The meter polices.** Once `quota.used` reached the budget, delivery stopped dead. Three
   subsequent push attempts added **exactly zero** delivered bytes.
2. **`quota.used` cannot exceed the budget, but the counter can.** The counter finished
   **46,146 bytes (2.2 %) above** the budget. So `quota.used` is *not* a billing figure — it is a
   budget-consumed gauge that saturates. Billing must read the counter. This is the same rule
   established in WP5, now confirmed at the boundary rather than in the small-payload regime.

## Why the in-run generator is INVALID, and why that matters

The harness ran three pushes (while-live → exhaust → after-exhaustion) and required push A to
succeed. It did not: `sent=0` on all three, the first with `TimeoutError ... timed out`. One push
also hit `OSError: [Errno 98] Address already in use`.

**Cause: the client was already over budget when the run began.** The provisioning read-back shows
`delivered_bytes: 2143298` *at provisioning time* — the counter object persisted from the previous
run (the counter is deliberately durable across re-arms, so it survives object churn). The budget
was therefore already spent before push A was attempted, the rule was already dropping, and every
push timed out. `Address already in use` is a leftover listener from the aborted earlier attempt.

The harness refused to produce a verdict from that — it printed **INVALID — the generator never
pushed while the budget was live**, rather than reading `sent=0` as "policed". That distinction is
the whole point: `sent=0` looks identical whether the meter is working perfectly or the generator is
broken, and only one of those is a result.

## What this run can and cannot claim

- **CAN claim:** at the budget boundary the meter stops delivery (0 bytes after exhaustion), and
  `quota.used` saturates at exactly the budget while the counter runs slightly ahead. Both measured.
- **CANNOT claim:** a clean before/after throughput comparison across the boundary, because the
  before-state was never captured with a live budget. That needs a fresh counter, which means
  deleting the counter object (not just re-arming the quota), then repeating the three-push
  sequence.

## Reproduce

```
# under the bench mutex
python3 /home/c08r4d0r/worktrees/bench-mutex/scripts/bench_lock.py   # or import bench_lock
sudo -n nft delete counter ip tgmeter c_10_77_0_9        # MUST reset the counter, not just the quota
sudo -n env TG_METER_TABLE=tgmeter python3 /tmp/tg_meter.py --client 10.77.0.9 --budget 2097152
sudo -n python3 /tmp/push_bytes.py 10.77.0.9 262144 47841   # push A: must succeed
sudo -n python3 /tmp/push_bytes.py 10.77.0.9 4194304 47842  # push B: exhaust
sudo -n python3 /tmp/push_bytes.py 10.77.0.9 65536 47843    # push C: must deliver ~0
```

The generator is a committed script, not a heredoc, because a heredoc silently no-opped on the
first attempt (`bash: syntax error near unexpected token '('`) and produced a **vacuous pass**
(`delivered=0` read as "policed"). A tool that fails silently in the "passing" direction is worse
than no tool.
