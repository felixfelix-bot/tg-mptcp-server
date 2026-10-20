#!/usr/bin/env python3
"""tg_meterd — the metering daemon: read the counters, journal the delta, re-arm before empty.

Writes nothing to the datapath on the read path. The only mutation is a re-arm, and every re-arm
is journaled BEFORE it runs, so a crash between "I spent X" and "I re-armed" is recoverable rather
than silently losing or duplicating value.

WHY THE COUNTER IS THE DURABLE IDENTITY
---------------------------------------
`delete quota` is refused (`Device or resource busy`) while any rule references it, so a quota
cannot be mutated in place and cannot be deleted while in use. Re-arming therefore means creating a
NEW quota object and replacing the metered rule by handle. Quota names are short-lived; the COUNTER
is not — it survives rule replacement because it is a separate object. So:

  * the COUNTER carries the billing truth and is never recreated;
  * the QUOTA is a disposable budget, replaced on every arm;
  * the JOURNAL records the counter value at each transition, so the sum across re-arms is the
    amount delivered even though no single object ever held it all.

That last point is the whole reason this daemon exists: a naive meter that reads `quota used`
loses everything already spent each time it re-arms, and a meter that reads only the live counter
loses everything spent before the last re-arm.

BILL FROM THE COUNTER, NEVER FROM `quota used`
----------------------------------------------
Measured on the target: with a 384-byte budget and ten 128-byte datagrams, the counter read
`packets 3 bytes 384` (actually delivered) while the quota read `used 384` on a budget that had also
absorbed the datagram that was DROPPED. Quota `used` reflects budget consumed, including traffic the
client never received. Charging for that overcharges the customer.

THE TWO SILENT FAILURES THIS DOES NOT PREVENT (they are policy, not arithmetic)
-------------------------------------------------------------------------------
1. An accept-policy metered chain is a FREE TUNNEL — everything works, nothing is billed. Detected by
   `tg_meter.py --verify`, which this daemon runs at startup and refuses to run without.
2. A metered rule keyed on an egress interface bills the server's own internet-side retransmissions.
   Also detected by `--verify`.

Usage:
    tg_meterd.py --once                 # one tick, print state, exit (for cron / testing)
    tg_meterd.py --daemon --interval 1
    tg_meterd.py --report               # what has been billed, from the journal
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tg_meter  # the single writer; this daemon never calls nft directly

STATE_DIR = Path(os.environ.get("TG_METER_STATE_DIR", "/var/lib/tg-meter"))
BYTES_PER_SAT = int(os.environ.get("TG_METER_BYTES_PER_SAT", "1048576"))  # policy, not kernel
TICK = float(os.environ.get("TG_METER_TICK_SECONDS", "1"))
LOW_WATER = int(os.environ.get("TG_METER_LOW_WATER_BYTES", "262144"))  # re-arm threshold
_ran = True


def _stop(_sig: int, _frm: Any) -> None:
    global _ran
    _ran = False


@dataclass
class Tick:
    client: str
    counter: str
    arm: int
    budget: int
    delivered_bytes: int
    delivered_packets: int
    delta_bytes: int
    sat_owed: int
    remaining: int
    rearmed: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MeterDaemon:
    """Tracks per-client spend across re-arms by journaling counter deltas."""

    def __init__(self, clients: list[str], match_fmt: str = "ip saddr {client}") -> None:
        self.clients = clients
        self.match_fmt = match_fmt
        self.prev: dict[str, int] = {}      # counter value at the last observation
        self.spent: dict[str, int] = {}     # cumulative billed bytes, across re-arms
        self._load()

    # --- durable state ---
    def _path(self) -> Path:
        return STATE_DIR / "meterd-state.json"

    def _load(self) -> None:
        p = self._path()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.prev = {k: int(v) for k, v in d.get("prev", {}).items()}
                self.spent = {k: int(v) for k, v in d.get("spent", {}).items()}
            except (json.JSONDecodeError, ValueError):
                # A corrupt state file must not silently reset billing to zero. Refuse to start
                # rather than under-bill; the journal still holds the events and a human decides.
                raise tg_meter.MeterError(
                    f"state file {p} is corrupt; refusing to start with billing reset to zero. "
                    "The journal is authoritative — reconcile from it before clearing this file."
                )

    def _save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = self._path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"prev": self.prev, "spent": self.spent}, indent=2, sort_keys=True))
        tmp.replace(p)

    # --- one tick ---
    def tick(self, client: str) -> Tick:
        c = tg_meter.read_client(client)
        if not c.counter:
            return Tick(client=client, counter="", arm=0, budget=0, delivered_bytes=0,
                        delivered_packets=0, delta_bytes=0, sat_owed=0, remaining=0,
                        error="client not provisioned")

        prior = self.prev.get(client, 0)
        # A counter that went BACKWARDS means either a restart (counters do not survive reboot) or
        # an out-of-band reset. Credit nothing for the drop, and record it: silently subtracting
        # would erase real revenue, silently ignoring it would double-bill on the next tick.
        if c.delivered_bytes < prior:
            tg_meter.journal("counter_regression", client=client, was=prior,
                             now=c.delivered_bytes, arm=c.arm)
            prior = 0
        delta = max(0, c.delivered_bytes - prior)
        self.prev[client] = c.delivered_bytes
        self.spent[client] = self.spent.get(client, 0) + delta
        sat = self.spent[client] // BYTES_PER_SAT

        remaining = max(0, c.budget - c.delivered_bytes)
        rearmed = False
        if remaining <= LOW_WATER:
            # JOURNAL FIRST. If we die after this line but before the re-arm, the journal shows the
            # intent and the counter value, so recovery re-arms rather than losing the client.
            tg_meter.journal("rearm_intent", client=client, arm=c.arm,
                             delivered=c.delivered_bytes, budget=c.budget,
                             spent_total=self.spent[client])
            try:
                tg_meter.arm(client, c.budget, self.match_fmt.format(client=client))
                rearmed = True
                self.prev[client] = 0  # the new rule starts a fresh quota; counter is unchanged
            except tg_meter.MeterError as exc:
                return Tick(client=client, counter=c.counter, arm=c.arm, budget=c.budget,
                            delivered_bytes=c.delivered_bytes, delivered_packets=c.delivered_packets,
                            delta_bytes=delta, sat_owed=sat, remaining=remaining,
                            error=f"rearm failed: {exc}")

        return Tick(client=client, counter=c.counter, arm=c.arm, budget=c.budget,
                    delivered_bytes=c.delivered_bytes, delivered_packets=c.delivered_packets,
                    delta_bytes=delta, sat_owed=sat, remaining=remaining, rearmed=rearmed)

    def tick_all(self) -> list[Tick]:
        out = [self.tick(c) for c in self.clients]
        self._save()
        return out


def report() -> dict[str, Any]:
    """Total billed per client, summed from the JOURNAL (authoritative across restarts)."""
    jp = tg_meter.journal_path()
    totals: dict[str, int] = {}
    if not jp.exists():
        return {"clients": {}, "note": "no journal yet"}
    with jp.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "reset":
                totals[rec["client"]] = 0
    return {"clients": totals, "journal": str(jp)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TollGate bonding meter daemon")
    ap.add_argument("--client", action="append", default=[],
                    help="client address (repeatable); default from TG_METER_CLIENTS")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval", type=float, default=TICK)
    ap.add_argument("--skip-verify", action="store_true",
                    help="NOT recommended: bypass the shape check that catches a free tunnel")
    a = ap.parse_args(argv)

    if a.report:
        print(json.dumps(report(), indent=2))
        return 0

    if not a.skip_verify:
        problems = tg_meter.verify()
        if problems:
            print(json.dumps({"ok": False, "refusing_to_meter": problems}, indent=2))
            return 1

    clients = a.client or [c for c in os.environ.get("TG_METER_CLIENTS", "").split(",") if c]
    if not clients:
        print(json.dumps({"ok": False, "error": "no clients configured"}), file=sys.stderr)
        return 2

    d = MeterDaemon(clients)
    if a.once:
        print(json.dumps([t.to_dict() for t in d.tick_all()], indent=2))
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while _ran:
        for t in d.tick_all():
            print(json.dumps(t.to_dict()), flush=True)
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
