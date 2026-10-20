"""Traffic generator: push N bytes FROM a given source IP through the metered rule.

Runs ON the VPS. Written as a file (not a heredoc) so shell quoting cannot silently no-op it.
Prints a machine-readable line so the caller can tell "pushed nothing" from "policed".
"""
import socket
import sys
import threading
import time

src = sys.argv[1]
total = int(sys.argv[2])
port = int(sys.argv[3]) if len(sys.argv) > 3 else 47821

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(1)

state = {"got": 0}


def acc():
    c, _ = srv.accept()
    while state["got"] < total:
        b = c.recv(65536)
        if not b:
            break
        state["got"] += len(b)
    c.close()
    srv.close()


t = threading.Thread(target=acc, daemon=True)
t.start()
time.sleep(0.3)

sent = 0
err = ""
try:
    c = socket.socket()
    c.bind((src, 0))          # bind the SOURCE so the nft rule ip saddr <src> matches
    c.connect(("127.0.0.1", port))
    while sent < total:
        chunk = b"x" * min(65536, total - sent)
        c.sendall(chunk)
        sent += len(chunk)
    c.close()
except Exception as exc:
    err = f"{type(exc).__name__}: {exc}"
t.join(10)

print(f"RESULT sent={sent} received={state['got']} error={err!r}")
