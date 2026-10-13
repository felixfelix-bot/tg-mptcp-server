# Multi-WAN bonding + TollGate — integration evaluation and demo design

> **Redaction note.** This document deliberately refers to the de-aggregation
> host as `<mptcp-server-host>` instead of its real address. The repository is
> public and naming a live listener's host, port and cipher advertises an
> unnecessary brute-force target. The technique is unchanged; only the
> deployment specifics are withheld. Supply your own endpoint locally.

Status: evaluation written from live verification (2026-10-13). Server side is
**verified working**. Router side is **staged and blocked on hardware access**
(see §6).

Every claim below is either marked **[verified]** (I observed it) or
**[to measure]** (planned, not yet observed).

---

## 1. Verified state (what actually works right now)

| Component | State | Evidence |
|---|---|---|
| tg-mptcp-server on vps2 `<mptcp-server-host>` | **live** | an `ss-server --mptcp` listener on a non-default port; cipher chacha20-ietf-poly1305, `tcp_and_udp`, `fast_open` |
| vps2 kernel MPTCP | **on** | kernel `6.12.111+deb13-amd64`, `CONFIG_MPTCP=y`, `net.mptcp.enabled=1` |
| Client → server MPTCP | **verified** | from x280: `ss-local --mptcp -l 1080` → vps2<port>; `ss -Mti` shows the connection carrying `tcp-ulp-mptcp` with a negotiated token (`token:0000(id:0)/6062ec55(id:0)`), `subflows_max:2`; egress IP = `<mptcp-server-host>` |
| Two-path subflow mechanism | **verified** | from cobrador (dual-homed: house ethernet + MT6000 guest wifi), MPTCP endpoints present on both interfaces (`ip mptcp endpoint show` → 2 endpoints, `subflows 2`), connection tokens negotiated on <port> |
| MT6000 kernel MPTCP | **on** | `tollgate-VIL4`, OpenWrt 25.12.5 r33051, kernel 6.12.94, `net.mptcp.enabled=1` (read over serial root shell) |
| MT6000 MPTCP client tooling | **missing** | BusyBox `ip` (no `ip mptcp` subcommand), no `ss`, no `mptcpize`, `/etc/apk/repositories` empty → no internet to install |

> Historical note: a July kanban run (`multi-wan-bonding` board, card
> `t_5458aeac`) reported the same box family with `net.mptcp.enabled=1` and
> demonstrated a 2-subflow MP_JOIN **bonding demo passing**. That is consistent
> with what I measured: the kernel capability is present on these MT6000 images;
> only the userspace tooling and the uplinks are missing.

---

## 2. What multi-WAN bonding actually takes (concrete, per layer)

Bonding is a **proxy-terminated MPTCP** design — not a kernel routing trick. Both
ends must be MPTCP-capable, and traffic is carried inside a proxy tunnel.

```
LAN clients
   │
TollGate router (MT6000, OpenWrt)
   ├── nftables: redirect WAN-bound traffic → local SOCKS/redirect proxy (tproxy or REDIRECT)
   ├── ss-redir --mptcp  ──► MPTCP connection to VPS ──┐
   ├── ip mptcp endpoint add <addr> subflow dev <wan1> │  2+ subflows,
   ├── ip mptcp endpoint add <addr> subflow dev <wan2> │  one per uplink
   └── ip mptcp limits set subflow 2                   ┘
                                                       │
VPS (tg-mptcp-server): ss-server --mptcp <port> ──► NAT ──► internet
```

Required, in order:

1. **Two genuinely independent uplinks** on the router (WAN ethernet + wifi-STA
   wwan, or ethernet + LTE). Two paths through one upstream do **not** multiply
   bandwidth — measured in M0 (see the bonding plan): two-path MPTCP ≈ best
   single path when the paths share a bottleneck.
2. **Kernel MPTCP on both ends** — present on the MT6000 (`net.mptcp.enabled=1`)
   and on the VPS.
3. **Userspace MPTCP-capable proxy** — `ss-redir`/`ss-local` with `--mptcp`
   (OpenWrt ships `shadowsocks-libev-*` packages in its feed).
4. **`ip-full` + `ss` + `mptcpize`** on the router (BusyBox `ip` cannot do
   `ip mptcp`).
5. **Per-uplink subflow registration**: `ip mptcp endpoint add <local-addr>
   subflow dev <wan-iface>` for each uplink, plus `ip mptcp limits set subflow N`.
6. **Traffic steering** so LAN traffic enters the proxy: nftables
   `tproxy`/`REDIRECT` to the local proxy port. This is the piece that collides
   with TollGate's own nftables rules — see §5.
7. **MTU/PMTU handling** — MTU discovery is a known real problem in this space
   (the bondingshouldbefree project maintains a dedicated tool for it). Measure;
   do not assume.

**Naming discipline:** MPTCP's path manager prefers the lowest-RTT subflow and
has no goodput maximiser in mainline OpenWrt kernels. Expect **failover + modest
aggregation**, never "2× the speed". Word user-facing claims accordingly.

---

## 3. bondingshouldbefree (BSBF) — evaluation

**What they actually run** (from their own client scripts and profile docs):
kernel MPTCP (`ip mptcp limits set subflows 8`,
`sysctl net.mptcp.syn_retrans_before_tcp_fallback=128` to *disable* TCP
fallback, `net.mptcp.blackhole_timeout=0`, per-default-route
`ip mptcp endpoint add ... subflow dev <wan>` polled every second) with a
**tproxy → xray VLESS** userspace proxy on top. `udp-encap-tun` and
`plp-mtu-discovery` (RFC 4821) exist to fix tunnel MTU. So it is *not* a
"TCP-over-UDP" stack — it is proxy-terminated MPTCP, the same architecture as
§2.

**Licensing (blocks adoption, verified by repo inspection):**

| Their component | Licence | Verdict |
|---|---|---|
| `OpenWrt/packages` forks | GPL-2.0-only | **incompatible** with TollGate's GPL-3.0 |
| `rutos-sdk`, `udp-encap-tun`, `tcp-in-udp` | no licence file at all | cannot use |
| `net/bsbf-resources` glue | AGPL-3.0-or-later, **already merged upstream** into `openwrt/packages` | redundant |
| `plp-mtu-discovery` | MIT | usable, but we do not need it |

**Verdict: interoperate at arm's length; do not vendor or depend.** Everything
their client needs — kernel MPTCP, `mptcpize`, `mptcpd`, `ip-full`, `xray-core`
— is already upstream. Their credential API (`server-ipv4`/`uuid`/`ocg`) is a
centralised per-device subscription, the opposite of a permissionless payment
rail. Our clean path is mainline MPTCP + our own proxy + our own Go endpoint
manager.

**One thing they got right that we should copy:** marking a weak link `backup`
so it serves failover rather than dragging aggregate throughput down, and
running MTU discovery rather than assuming 1500.

---

## 4. Integration with `c03rad0r/tg-mptcp-server`

**Server: done.** The Ansible playbook (`roles/mptcp_server`) deploys
`ss-server --mptcp` under a systemd override, enables the MPTCP sysctls
(`roles/base`), and sets `ip mptcp limits` via a `mptcp-limits.service`. It is
deployed and **verified live on vps2** (§1). Ports: a non-default port (shadowsocks TCP+UDP),
65001 (glorytun, UDP aggregation).

**Client (router) steps — staged, needs the router online:**

```
# 1. tooling (needs internet)
apk update
apk add ip-full ss mptcpize shadowsocks-libev-ss-redir

# 2. per-uplink subflows
ip mptcp limits set subflow 2 add_addr_accepted 2
ip mptcp endpoint add <wan1-ip> subflow dev <wan1-iface>
ip mptcp endpoint add <wwan-ip> subflow dev <wwan-iface>

# 3. MPTCP-aware redirect proxy to the VPS
ss-redir -s <mptcp-server-host> -p <port> -l 1080 -k <pw> -m chacha20-ietf-poly1305 --mptcp

# 4. steer traffic into it (nftables tproxy/REDIRECT) — collision warning in §5
```

`ss-local --mptcp` to this same server is **already proven** to establish a
token-negotiated MPTCP connection (§1), so the only untested part is the
on-router redirect and the second uplink.

---

## 5. TollGate integration — what it takes

**Principle: the bonded path is just "the uplink".** TollGate merchant mode
(captive portal, sessions, per-step pricing, gate close on exhaustion) does not
change. A client that buys access from the router gets internet through the
bonded path transparently.

Three real integration points:

### 5.1 Config surface (already partly built)

The fork branch `feat/mptcp-bonding` already carries a `mptcp_bonding` config
section (config version bumped v0.0.8 → v0.0.9) with `enabled`, `server`,
`interfaces`, `max_subflows`, `fallback_to_normal`, `local_proxy_port`. That is
the right shape and the natural seam — it is **opt-in and disabled by default
with graceful fallback**, which matters: a bonding failure must not take the
router's internet away.

Fields to align with what is actually needed: drop anything the runtime does not
consume; add the per-uplink iface list explicitly and the server endpoint.

### 5.2 nftables / mark / route-table collision — **the hard part**

TollGate merchant mode already owns nftables hooks, packet marks, and a
captive-portal redirect. BSBF-style stacks claim packet marks 1–2, routing table
1, ports 12345/8080. **Two tproxy/redirect systems and two mark/table
namespaces on one box will fight.** Before writing any rules, write down which
marks, tables, metrics and ports each component owns — this is a required
deliverable, not documentation polish. Expect this to be the bulk of the work.

### 5.3 Crows Nest / upstream detector (the strategic win)

`tg-mptcp-server`'s own README frames the router side as **Crows Nest**
(`tollgate-module-crowsnest-go`) connecting to multiple WiFi gateways. Today a
router uses one gateway at a time. Bonding is what makes "use all of them at
once" possible — i.e. the natural integration is:

- `wireless_gateway_manager` / crowsnest: connect to **N** gateways instead of one
- `upstream_detector`: advertise all of them
- bonding layer: register a subflow per gateway interface
- `valve` (ndsctl): unchanged — still gates MACs

### 5.4 Economics — do not skip this

A future "pay the bonding server per megabyte" design has a measured problem:
TollGate's `step_size` is an indivisible allotment (21 MiB in the live advert,
`price_per_step` 1 sat across 6 mints) and an allotment is **non-refundable and
non-transferable**. A bonding client cannot align per-subflow byte consumption to
step boundaries, so every purchase strands allotment — and a two-upstream bond
pays that loss twice. MPTCP reinjection may also be metered twice (per-path
counting). Price per byte with a small step (1 MiB), `min_steps=1`, bounded
`max_steps`. Stage 1 (honest server-side metering) before Stage 2.

### 5.5 The advert interface (verified live, useful for the demo)

`GET :2121/` on a running TollGate returns its kind-10021 advert with
`metric=bytes`, `step_size=22020096`, `price_per_step` per mint. A bonding
service could publish the same advert shape with a capability tag
(`capability=bonding`) rather than inventing a new kind — `upstream_detector`
already parses 10021.

---

## 6. Demo design (what I will show, and what is blocked)

**Demo A — server-side aggregation endpoint (runnable today).**
VPS ss-server `--mptcp` + a dual-homed client; show `ss -Mti` subflows and
failover when one path is blackholed. **Verified in parts** (§1); the
throughput multiplication claim needs two independent uplinks and is *not*
demonstrated.

**Demo B — router-side bonding (blocked on hardware access).**
MT6000: WAN ethernet + wifi-STA wwan → `ss-redir --mptcp` → vps2 → client buys
access through the bonded path via the TollGate portal. Blocked on: the router
running internet + the StarGate AP being in range.

**Demo C — TollGate integration (design + config-level).**
The `mptcp_bonding` config section driving a Go orchestrator that starts/stops
`ss-redir`, registers subflows, and installs nftables rules, with graceful
fallback to normal routing. The Go package (`src/mptcp_bonding/`) is not yet
written; the config schema is.

### Honest gaps

- No router-side bonded throughput number yet (needs two independent uplinks).
- StarGate was not visible in any scan (both MT6000s + two laptops, passive and
  directed probes) at the time of writing.
- The nftables collision in §5.2 is identified but not yet resolved.
- `src/mptcp_bonding/` manager package does not exist yet.
