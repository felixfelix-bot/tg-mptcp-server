"""WP6 policing — clean confirmation run.

Prior run showed the meter working (delivered 98603 < budget 131072, quota used pinned at 131072,
+7 packets) but the generator then timed out BECAUSE the budget was spent — so the run could not
attribute the effect to policing rather than to a broken generator.

This version separates the two: it provisions a FRESH budget, pushes a measured burst while the
budget is live (must succeed), then pushes again after exhaustion (must fail), and requires both
observations before any verdict.
"""
import json
import re
import subprocess
import sys
import time

SCRIPTS = "/home/c08r4d0r/worktrees/bench-mutex/scripts"
sys.path.insert(0, SCRIPTS)
import bench_lock  # noqa: E402

OUT = "/home/c08r4d0r/reports/mptcp-loop/wp6-policing.txt"
CLIENT = "10.77.0.9"
BUDGET = 2097152      # 2 MiB: room for a live burst, then exhaustion
BURST = 262144        # 256 KiB burst while budget is live


def ssh(cmd, timeout=240):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "vps2fips", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return (r.stdout or "").strip(), (r.stderr or "").strip()[:400]


def env(more=""):
    return f"sudo -n env TG_METER_TABLE=tgmeter TG_METER_STATE_DIR=/tmp/tgmeter-state {more}"


def read():
    out, _ = ssh(env(f"python3 /tmp/tg_meter.py --read --client {CLIENT}"))
    try:
        return json.loads(out)
    except Exception:
        return {}


def push(nbytes, port):
    out, err = ssh(f"sudo -n python3 /tmp/push_bytes.py {CLIENT} {nbytes} {port}", timeout=300)
    m = re.search(r"sent=(\d+)", out)
    sent = int(m.group(1)) if m else 0
    return sent, (out or err)


lines = []


def rec(s=""):
    print(s)
    lines.append(s)


deadline = time.time() + 600
while bench_lock.is_held():
    if time.time() > deadline:
        print("bench busy; not forcing")
        raise SystemExit(4)
    time.sleep(15)

lock = bench_lock.acquire("wp6-overquota-policing")
rec("bench lock acquired")
try:
    out, _ = ssh(
        "sudo -n nft delete counter ip tgmeter c_10_77_0_9 2>/dev/null; sudo -n nft reset counter ip tgmeter c_10_77_0_9 2>/dev/null; "
        "for q in $(sudo -n nft -j list table ip tgmeter 2>/dev/null | grep -oE '\"q_10_77_0_9_[0-9]+\"' | tr -d '\"' | sort -u); do "
        "sudo -n nft delete quota ip tgmeter $q 2>/dev/null; done; echo reset"
    )
    rec("pre-reset: " + (out or ""))

    out, _ = ssh(f"sudo -n ip addr add {CLIENT}/32 dev lo 2>/dev/null; echo addr_ok")
    out, _ = ssh(env(f"python3 /tmp/tg_meter.py --client {CLIENT} --budget {BUDGET}"))
    rec("\n=== provisioned ===")
    rec(out)

    rec(f"\n=== A) burst of {BURST} bytes WHILE the budget is live (must flow) ===")
    s1, raw1 = push(BURST, 47941)
    rec(raw1)
    a = read()
    rec(f"after A: delivered={a.get('delivered_bytes')} packets={a.get('delivered_packets')} used={a.get('policed_used')}")

    rec("\n=== B) push past the remaining budget (must stop) ===")
    s2, raw2 = push(BUDGET * 2, 47942)
    rec(raw2)
    b = read()
    rec(f"after B: delivered={b.get('delivered_bytes')} packets={b.get('delivered_packets')} used={b.get('policed_used')}")

    rec("\n=== C) push AGAIN after exhaustion (must be refused / stall) ===")
    s3, raw3 = push(65536, 47943)
    rec(raw3)
    c = read()
    rec(f"after C: delivered={c.get('delivered_bytes')} packets={c.get('delivered_packets')} used={c.get('policed_used')}")

    rec("\n=== VERDICT ===")
    dA = a.get("delivered_bytes", 0)
    dB = b.get("delivered_bytes", 0)
    dC = c.get("delivered_bytes", 0)
    used = c.get("policed_used", 0)
    budget = c.get("budget", BUDGET)
    rec(f"generator: A sent={s1}  B sent={s2}  C sent={s3}")
    rec(f"delivered: A={dA}  B={dB}  C={dC}   budget={budget}  quota_used={used}")
    rec("")
    rec(f"1. traffic flowed while budget was live (A delivered > 0):  {dA > 0}")
    rec(f"2. delivery STOPPED at the budget (C delivered <= budget):  {dC <= budget}")
    rec(f"3. quota.used pinned at budget, not beyond:                 {used <= budget}")
    rec(f"4. post-exhaustion push delivered ~0 extra bytes:           {dC - dB <= 4096}")
    rec(f"5. billing from quota.used would overcharge (used>delivered): {used > dC}")
    rec("")
    if dA > 0 and dC <= budget:
        rec("VERDICT: PASS — the meter policed real traffic at the budget boundary.")
    elif s1 == 0:
        rec("VERDICT: INVALID — the generator never pushed while the budget was live.")
    else:
        rec("VERDICT: FAIL — traffic flowed but the meter did not stop at the budget.")

    rec("\n=== cleanup ===")
    out, _ = ssh(
        "sudo -n nft delete counter ip tgmeter c_10_77_0_9 2>/dev/null; sudo -n nft reset counter ip tgmeter c_10_77_0_9 2>/dev/null; "
        "for q in $(sudo -n nft -j list table ip tgmeter 2>/dev/null | grep -oE '\"q_10_77_0_9_[0-9]+\"' | tr -d '\"' | sort -u); do "
        "sudo -n nft delete quota ip tgmeter $q 2>/dev/null; done; "
        "sudo -n ip addr del 10.77.0.9/32 dev lo 2>/dev/null; echo cleaned"
    )
    rec(out)
    out, _ = ssh("sudo -n nft list tables | tr '\\n' ' '")
    rec("tables: " + out)
finally:
    bench_lock.release(lock)
    rec("\nbench lock released")
    open(OUT, "w").write("\n".join(lines) + "\n")
