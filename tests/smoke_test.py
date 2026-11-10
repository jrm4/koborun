#!/usr/bin/env python3
"""Smoke test: run every koboldctl subcommand against the loopback mock server."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KOBOLDCTL = ROOT / "koboldctl.py"
MOCK = ROOT / "tests" / "mock_koboldcpp.py"
PORT = 15001
BASE = "http://127.0.0.1:%d" % PORT
ENV = dict(os.environ, KOBOLDCPP_ADMIN_PASSWORD="admin-secret", KOBOLDCPP_PASSWORD="user-secret")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (status, name, detail if not cond else ""))
    if not cond:
        failures.append(name)


def run(args, env=None, timeout=30):
    return subprocess.run(
        [sys.executable, str(KOBOLDCTL), "--url", BASE] + args,
        capture_output=True, text=True, timeout=timeout, env=env or ENV,
    )


def main():
    mock = subprocess.Popen([sys.executable, str(MOCK), str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                run(["procs"], timeout=5)
                break
            except Exception:
                time.sleep(0.2)
        with tempfile.TemporaryDirectory() as mdir:
            Path(mdir, "local-only-model-Q6_K.gguf").write_bytes(b"GGUF")
            Path(mdir, "sub").mkdir()
            Path(mdir, "sub", "nested-model.gguf").write_bytes(b"GGUF")

            r = run(["--model-dir", mdir, "status"])
            check("status exit", r.returncode == 0, r.stderr)
            check("status version", "v1.99.0-mock" in r.stdout, r.stdout)
            check("status model", "tiny-test-model-Q4_K_M.gguf" in r.stdout, r.stdout)
            check("status perf", "42.7 tok/s" in r.stdout and "8192" in r.stdout, r.stdout)

            r = run(["--model-dir", mdir, "models"])
            check("models lists server file", "big-test-model-Q8_0.gguf" in r.stdout, r.stdout)
            check("models marks current", "* tiny-test-model-Q4_K_M.gguf" in r.stdout, r.stdout)
            check("models lists local files", "local-only-model-Q6_K.gguf" in r.stdout
                  and os.path.join("sub", "nested-model.gguf") in r.stdout, r.stdout)

            r = run(["load", "big-test-model-Q8_0.gguf", "--wait", "20"])
            check("load exit", r.returncode == 0, r.stdout + r.stderr)
            check("load reports new model", "Loaded: big-test-model-Q8_0.gguf" in r.stdout, r.stdout)

            r = run(["status"])
            check("status after swap", "big-test-model-Q8_0.gguf" in r.stdout, r.stdout)

            r = run(["unload", "--wait", "20"])
            check("unload exit", r.returncode == 0, r.stdout + r.stderr)
            r = run(["status"])
            check("status after unload", "inactive" in r.stdout, r.stdout)

            r = run(["load", "tiny-test-model-Q4_K_M.gguf", "--wait", "20"])
            check("reload after unload", r.returncode == 0 and "Loaded:" in r.stdout, r.stdout + r.stderr)

            r = run(["abort"])
            check("abort", r.returncode == 0 and "success=true" in r.stdout, r.stdout + r.stderr)

            r = run(["procs"])
            check("procs ignores non-koboldcpp procs",
                  r.returncode == 0 and "No local koboldcpp" in r.stdout, r.stdout)

            noauth = dict(os.environ, KOBOLDCPP_URL=BASE)
            r = run(["load", "big-test-model-Q8_0.gguf"], env=noauth)
            check("load rejected without admin password", r.returncode == 1 and "rejected" in r.stderr,
                  r.stdout + r.stderr)
            r = run(["status"], env=noauth)
            check("status shows protected model without password",
                  "protected-model" in r.stdout, r.stdout)

            watch = subprocess.Popen(
                [sys.executable, str(KOBOLDCTL), "--url", BASE, "watch", "--interval", "0.5"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
            time.sleep(2.5)
            watch.send_signal(signal.SIGINT)
            out, _ = watch.communicate(timeout=10)
            check("watch plain mode renders", "koboldctl -" in out and "tok/s" in out, out)

            import pty
            import select
            master, slave = pty.openpty()
            tui = subprocess.Popen(
                [sys.executable, str(KOBOLDCTL), "--url", BASE, "watch", "--interval", "0.5"],
                stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                env=dict(ENV, TERM="xterm"))
            os.close(slave)
            time.sleep(3)
            os.write(master, b"q")
            raw = b""
            deadline = time.time() + 10
            while time.time() < deadline and tui.poll() is None:
                r, _, _ = select.select([master], [], [], 0.5)
                if r:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    raw += chunk
            tui.wait(timeout=10)
            os.close(master)
            text = raw.decode(errors="ignore")
            check("watch TUI renders model picker", "Models" in text and "tok/s" in text, text[-400:])
            check("watch TUI lists models", "big-test-model-Q8_0.gguf" in text, text[-400:])
    finally:
        mock.terminate()
        mock.wait(timeout=5)

    print()
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
