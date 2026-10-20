"""Deterministic process fixture for hang/crash/protocol recovery tests."""
import json
import os
from pathlib import Path
import signal
import sys
import time

folder = Path(sys.argv[2])
while True:
    request_path = folder / "request.json"
    if not request_path.exists():
        time.sleep(0.005)
        continue
    request = json.loads(request_path.read_text())
    request_path.unlink()
    operation = request["operation"]
    if operation == "hang":
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (folder / "hung").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    if operation == "crash":
        os._exit(7)
    if operation == "slow":
        time.sleep(request["arguments"].get("seconds", 0.2))
    response = {"id": "obsolete" if operation == "stale" else request["id"],
                "ok": True, "result": {"pid": os.getpid(), "arguments": request["arguments"]}}
    (folder / "response.tmp").write_text(json.dumps(response))
    (folder / "response.tmp").replace(folder / "response.json")
