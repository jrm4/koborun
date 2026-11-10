#!/usr/bin/env python3
"""koboldctl - monitor a running KoboldCpp server and hot-swap models on the fly.

Talks to the KoboldCpp HTTP API (default http://127.0.0.1:5001). Model
switching uses the admin endpoint POST /api/admin/reload_config, so the
server should be started with admin mode enabled, e.g.:

    ./koboldcpp --admin --admindir ./models --adminpassword secret

Configuration via flags or environment variables:
    KOBOLDCPP_URL             server base URL (default http://127.0.0.1:5001)
    KOBOLDCPP_PASSWORD        regular --password of the server (Bearer auth)
    KOBOLDCPP_ADMIN_PASSWORD  --adminpassword of the server
    KOBOLDCPP_MODEL_DIR       extra local dirs to scan for models (os.pathsep separated)
"""

import argparse
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:5001"
MODEL_EXTS = (".gguf", ".kcpps", ".kcppt")
SPECIAL_TARGETS = ("unload_model", "initial_model")


class KoboldError(Exception):
    pass


class KoboldClient:
    def __init__(self, url, password=None, admin_password=None, timeout=10):
        self.url = url.rstrip("/")
        self.tokens = [t for t in (admin_password, password) if t]
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        attempts = self.tokens if self.tokens else [None]
        for i, tok in enumerate(attempts):
            req = urllib.request.Request(self.url + path, data=body, method=method)
            req.add_header("Content-Type", "application/json")
            if tok:
                req.add_header("Authorization", "Bearer " + tok)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code == 401 and i + 1 < len(attempts):
                    continue
                if e.code == 401:
                    raise KoboldError(
                        "401 Unauthorized from " + path
                        + " - set --password/--admin-password (or KOBOLDCPP_PASSWORD/KOBOLDCPP_ADMIN_PASSWORD)"
                    )
                raise KoboldError("HTTP %d from %s: %s" % (e.code, path, e.read()[:200]))
            except urllib.error.URLError as e:
                raise KoboldError("cannot reach %s (%s) - is KoboldCpp running?" % (self.url, e.reason))
        raise KoboldError("unreachable")

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, payload=None):
        return self._request("POST", path, payload if payload is not None else {})


def safe_get(client, path):
    try:
        return client.get(path)
    except KoboldError as e:
        return {"__error__": str(e)}


def norm_name(s):
    s = os.path.basename(str(s or "")).strip().lower()
    for ext in MODEL_EXTS + (".bin", ".safetensors"):
        if s.endswith(ext):
            s = s[: -len(ext)]
            break
    return s


def fmt_dur(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm %02ds" % (h, m, s)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def fmt_speed(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    return "%.1f tok/s" % v if v > 0 else "-"


def find_local_processes():
    procs = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return procs
    for entry in sorted(proc_root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        try:
            argv = shlex.split(raw.replace(b"\0", b" ").decode(errors="ignore"))
        except ValueError:
            continue
        if not argv:
            continue
        bases = [os.path.basename(a) for a in argv[:2]]
        if not any(b == "koboldcpp" or b == "koboldcpp.py" or b.startswith("koboldcpp-") for b in bases):
            continue

        def flagval(name):
            if name in argv:
                i = argv.index(name)
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    return argv[i + 1]
            return None

        procs.append({
            "pid": int(entry.name),
            "port": flagval("--port") or "5001",
            "model": flagval("--model") or "-",
            "admin": "--admin" in argv,
            "router": "--routermode" in argv,
            "admindir": flagval("--admindir") or "-",
        })
    return procs


def scan_model_dirs(dirs):
    found = []
    for d in dirs:
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        for f in sorted(p.iterdir()):
            if f.is_file() and f.suffix.lower() in MODEL_EXTS:
                found.append(str(f.relative_to(p)))
            elif f.is_dir():
                for sf in sorted(f.iterdir()):
                    if sf.is_file() and sf.suffix.lower() in MODEL_EXTS:
                        found.append(str(sf.relative_to(p)))
    return sorted(set(found))


def gather(client, model_dirs):
    caps = safe_get(client, "/api/extra/version")
    model = safe_get(client, "/api/v1/model")
    perf = safe_get(client, "/api/extra/perf")
    maxctx = safe_get(client, "/api/v1/config/max_context_length")
    maxlen = safe_get(client, "/api/v1/config/max_length")
    admin_opts = safe_get(client, "/api/admin/list_options")
    ollama = safe_get(client, "/v1/models")
    server_models = []
    if isinstance(admin_opts, list):
        server_models = [str(x) for x in admin_opts]
    elif isinstance(ollama, dict):
        server_models = [m.get("id", "") for m in ollama.get("data", []) if m.get("id")]
    local_models = scan_model_dirs(model_dirs)
    return {
        "caps": caps if isinstance(caps, dict) else {"__error__": str(caps)},
        "model": model if isinstance(model, dict) else {"__error__": str(model)},
        "perf": perf if isinstance(perf, dict) else {"__error__": str(perf)},
        "maxctx": maxctx if isinstance(maxctx, dict) else {},
        "maxlen": maxlen if isinstance(maxlen, dict) else {},
        "server_models": server_models,
        "local_models": local_models,
        "procs": find_local_processes(),
    }


def current_model_name(data):
    return str(data.get("model", {}).get("result", "") or "")


def merged_model_list(data):
    names = list(data.get("server_models", []))
    for m in data.get("local_models", []):
        if m not in names:
            names.append(m)
    cur = current_model_name(data)
    cur_n = norm_name(cur)
    if cur and all(norm_name(n) != cur_n for n in names):
        names.insert(0, cur)
    return names


def load_succeeded(target, before, current):
    if current is None:
        return False
    c = str(current).strip().lower()
    if target == "unload_model":
        return c in ("inactive", "none", "")
    if not c:
        return False
    if norm_name(current) == norm_name(target):
        return True
    if target == "initial_model":
        return c != before.strip().lower()
    if target.lower().endswith((".kcpps", ".kcppt")):
        return c != before.strip().lower()
    return False


def request_load(client, target, baseconfig=None):
    payload = {"filename": target}
    if baseconfig:
        payload["baseconfig"] = baseconfig
    resp = client.post("/api/admin/reload_config", payload)
    if not isinstance(resp, dict) or not resp.get("success"):
        raise KoboldError(
            "server rejected the switch (response: %s). Model switching needs KoboldCpp started with "
            "--admin --admindir <dir>, and the name must exist in that dir (see 'koboldctl models')."
            % json.dumps(resp)
        )


def wait_for_model(client, target, timeout, poll=1.0, quiet=False):
    before_resp = safe_get(client, "/api/v1/model")
    before = str(before_resp.get("result", "") or "") if isinstance(before_resp, dict) else ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        resp = safe_get(client, "/api/v1/model")
        cur = resp.get("result") if isinstance(resp, dict) else None
        if isinstance(cur, str) and load_succeeded(target, before, cur):
            return cur
    return None


def cmd_status(args, client, model_dirs):
    data = gather(client, model_dirs)
    caps = data["caps"]
    model = data["model"]
    perf = data["perf"]
    err = None
    if "__error__" in model:
        err = model["__error__"]
    print("Server      : %s" % client.url)
    if "__error__" in caps:
        print("  ERROR: %s" % caps["__error__"])
        return 1
    print("KoboldCpp   : v%s  (api result=%s)" % (caps.get("version", "?"), caps.get("result", "?")))
    flags = []
    if caps.get("protected"):
        flags.append("password-protected")
    admin = caps.get("admin", 0)
    if admin:
        flags.append("admin mode (%s)" % ("with admin password" if admin == 2 else "no admin password"))
    if caps.get("router"):
        flags.append("router mode")
    for cap in ("llm", "vision", "audio", "tts", "embeddings", "transcribe", "multiplayer", "websearch"):
        if caps.get(cap):
            flags.append(cap)
    print("Capabilities: %s" % (", ".join(flags) if flags else "-"))
    if err:
        print("Model       : ERROR: %s" % err)
    else:
        print("Model       : %s" % (model.get("result", "?")))
    print("Max context : %s   Max length: %s" % (data["maxctx"].get("value", "?"), data["maxlen"].get("value", "?")))
    if "__error__" in perf:
        print("Performance : ERROR: %s" % perf["__error__"])
    else:
        idle = perf.get("idle", 1)
        state = "IDLE" if idle else "BUSY (queue=%s)" % perf.get("queue", 0)
        print("Performance : prompt %s | gen %s | %s" % (
            fmt_speed(perf.get("last_process_speed")),
            fmt_speed(perf.get("last_eval_speed")),
            state,
        ))
        print("Activity    : uptime %s | idle for %s | total gens %s | last stop: %s" % (
            fmt_dur(perf.get("uptime", 0)),
            fmt_dur(perf.get("idletime", 0)),
            perf.get("total_gens", "?"),
            perf.get("stop_reason") or "-",
        ))
    procs = data["procs"]
    print("Processes   : %d local koboldcpp process(es)" % len(procs))
    for p in procs:
        print("  pid %-7d port %-6s admin=%s router=%s model=%s" % (
            p["pid"], p["port"], p["admin"], p["router"], p["model"]))
    return 0 if not err else 1


def cmd_models(args, client, model_dirs):
    data = gather(client, model_dirs)
    names = merged_model_list(data)
    cur = current_model_name(data)
    cur_n = norm_name(cur)
    if not names:
        print("No models found. Server list empty and no local files in: %s" % ", ".join(model_dirs))
        return 1
    print("Available models (server: %s):" % client.url)
    for n in names:
        mark = "*" if norm_name(n) == cur_n else " "
        tag = ""
        if n in SPECIAL_TARGETS:
            tag = "  (special)"
        print(" %s %s%s" % (mark, n, tag))
    if cur:
        print("(* = currently loaded: %s)" % cur)
    return 0


def cmd_load(args, client, model_dirs):
    name = args.name
    target = {"unload": "unload_model", "initial": "initial_model"}.get(name, name)
    try:
        request_load(client, target, baseconfig=args.baseconfig)
    except KoboldError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("Switch requested: %s" % target)
    if args.no_wait:
        return 0
    print("Waiting for the model to load (timeout %ds)..." % args.wait)
    final = wait_for_model(client, target, args.wait)
    if final is None:
        print("Timed out waiting; check 'koboldctl status'.", file=sys.stderr)
        return 1
    print("Loaded: %s" % final)
    return 0


def cmd_unload(args, client, model_dirs):
    args.name = "unload"
    args.baseconfig = None
    return cmd_load(args, client, model_dirs)


def cmd_abort(args, client, model_dirs):
    try:
        resp = client.post("/api/extra/abort", {"genkey": ""})
    except KoboldError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("abort: success=%s done=%s" % (resp.get("success"), resp.get("done")))
    return 0 if str(resp.get("success")) == "true" else 1


def cmd_procs(args, client, model_dirs):
    procs = find_local_processes()
    if not procs:
        print("No local koboldcpp processes found.")
        return 0
    for p in procs:
        print("pid %-7d http://127.0.0.1:%s  admin=%s router=%s" % (p["pid"], p["port"], p["admin"], p["router"]))
        print("           model=%s  admindir=%s" % (p["model"], p["admindir"]))
    return 0


def render_block(data, client):
    lines = []
    caps = data["caps"]
    model = data["model"]
    perf = data["perf"]
    lines.append("koboldctl - %s   [%s]" % (client.url, time.strftime("%H:%M:%S")))
    if "__error__" in caps or "__error__" in model:
        err = model.get("__error__") or caps.get("__error__")
        lines.append("ERROR: %s" % err)
        return lines
    admin = caps.get("admin", 0)
    lines.append("KoboldCpp v%s | model: %s | max ctx %s | max len %s | admin=%s router=%s" % (
        caps.get("version", "?"), model.get("result", "?"),
        data["maxctx"].get("value", "?"), data["maxlen"].get("value", "?"),
        admin, bool(caps.get("router"))))
    if "__error__" not in perf:
        idle = perf.get("idle", 1)
        state = "IDLE" if idle else "BUSY queue=%s" % perf.get("queue", 0)
        lines.append("perf: prompt %s | gen %s | %s | uptime %s | gens %s | stop: %s" % (
            fmt_speed(perf.get("last_process_speed")), fmt_speed(perf.get("last_eval_speed")),
            state, fmt_dur(perf.get("uptime", 0)), perf.get("total_gens", "?"),
            perf.get("stop_reason") or "-"))
    for p in data["procs"]:
        lines.append("proc: pid %d port %s model %s" % (p["pid"], p["port"], p["model"]))
    return lines


def cmd_watch_plain(args, client, model_dirs):
    interval = args.interval
    try:
        while True:
            data = gather(client, model_dirs)
            print("\n".join(render_block(data, client)))
            names = merged_model_list(data)
            cur_n = norm_name(current_model_name(data))
            print("models: " + ", ".join(("*%s*" % n) if norm_name(n) == cur_n else n for n in names))
            print("(ctrl-c to quit; for the interactive picker run in a real terminal)")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_watch(args, client, model_dirs):
    if not sys.stdout.isatty():
        return cmd_watch_plain(args, client, model_dirs)
    try:
        import curses
    except ImportError:
        return cmd_watch_plain(args, client, model_dirs)
    return curses.wrapper(tui_main, client, model_dirs, args.interval, args.wait)


def tui_main(stdscr, client, model_dirs, interval, wait_timeout):
    import curses
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    stdscr.nodelay(True)
    sel = 0
    msg = ""
    data = gather(client, model_dirs)
    last_refresh = time.time()

    def put(y, x, text, attr=0):
        try:
            stdscr.addstr(y, x, text[: max(0, curses.COLS - x - 1)], attr)
        except curses.error:
            pass

    while True:
        now = time.time()
        if now - last_refresh >= interval:
            data = gather(client, model_dirs)
            last_refresh = now
        names = merged_model_list(data)
        cur_n = norm_name(current_model_name(data))
        if names:
            sel = max(0, min(sel, len(names) - 1))

        stdscr.erase()
        block = render_block(data, client)
        for i, line in enumerate(block[:6]):
            put(i, 0, line, curses.color_pair(1) | curses.A_BOLD if i == 0 else 0)
        y = len(block) + 1
        put(y, 0, "Models (up/down + enter = load, u = unload, a = abort, r = refresh, q = quit):",
            curses.A_BOLD)
        y += 1
        for i, n in enumerate(names):
            if y >= curses.LINES - 2:
                put(y, 2, "... %d more" % (len(names) - i))
                break
            is_cur = norm_name(n) == cur_n
            attr = curses.A_BOLD | (curses.color_pair(2) if is_cur else 0)
            if i == sel:
                attr |= curses.A_REVERSE
            mark = "*" if is_cur else " "
            put(y, 0, "%s %d. %s" % (mark, i + 1, n), attr)
            y += 1
        if msg:
            put(curses.LINES - 1, 0, msg, curses.color_pair(4))
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            break
        elif ch == ord("r"):
            last_refresh = 0
        elif ch == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif ch == curses.KEY_DOWN:
            sel = min(max(0, len(names) - 1), sel + 1)
        elif ch in (curses.KEY_ENTER, 10, 13) and names:
            target = {"unload": "unload_model", "initial": "initial_model"}.get(names[sel], names[sel])
            msg = "switching to %s ..." % target
            stdscr.refresh()
            try:
                request_load(client, target)
                final = wait_for_model(client, target, wait_timeout)
                msg = ("loaded: %s" % final) if final else "timed out waiting for %s" % target
            except KoboldError as e:
                msg = "error: %s" % e
            last_refresh = 0
        elif ch == ord("u"):
            msg = "unloading model ..."
            stdscr.refresh()
            try:
                request_load(client, "unload_model")
                final = wait_for_model(client, "unload_model", wait_timeout)
                msg = ("unloaded" if final is not None else "timed out unloading")
            except KoboldError as e:
                msg = "error: %s" % e
            last_refresh = 0
        elif ch == ord("a"):
            try:
                resp = client.post("/api/extra/abort", {"genkey": ""})
                msg = "abort: success=%s" % resp.get("success")
            except KoboldError as e:
                msg = "error: %s" % e
            last_refresh = 0
        time.sleep(0.05)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="koboldctl",
        description="Monitor a KoboldCpp server and hot-swap models on the fly.",
    )
    p.add_argument("--url", default=os.environ.get("KOBOLDCPP_URL", DEFAULT_URL),
                   help="KoboldCpp base URL (env KOBOLDCPP_URL, default %(default)s)")
    p.add_argument("--password", default=os.environ.get("KOBOLDCPP_PASSWORD"),
                   help="server --password, sent as Bearer token (env KOBOLDCPP_PASSWORD)")
    p.add_argument("--admin-password", default=os.environ.get("KOBOLDCPP_ADMIN_PASSWORD"),
                   help="server --adminpassword (env KOBOLDCPP_ADMIN_PASSWORD)")
    p.add_argument("--model-dir", action="append", default=None,
                   help="local dir to scan for models (repeatable; env KOBOLDCPP_MODEL_DIR is split on %s)"
                        % os.pathsep)
    p.add_argument("--timeout", type=float, default=10, help="per-request HTTP timeout in seconds")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("status", help="one-shot status of server, model, performance and local processes")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", help="live dashboard; in a terminal, pick a model and press enter to hot-swap")
    sp.add_argument("--interval", type=float, default=2.0, help="refresh interval seconds (default 2)")
    sp.add_argument("--wait", type=int, default=300, help="seconds to wait for a model switch to finish")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("models", help="list models known to the server and found locally")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("load", help="hot-swap to a model (admin mode required); 'unload'/'initial' are special")
    sp.add_argument("name", help="model file name from 'koboldctl models', or 'unload' / 'initial'")
    sp.add_argument("--baseconfig", default=None, help="optional override base .kcpps config")
    sp.add_argument("--wait", type=int, default=300, help="seconds to wait for load to finish")
    sp.add_argument("--no-wait", action="store_true", help="return immediately after requesting the switch")
    sp.set_defaults(func=cmd_load)

    sp = sub.add_parser("unload", help="unload the current model (admin mode required)")
    sp.add_argument("--wait", type=int, default=300)
    sp.add_argument("--no-wait", action="store_true")
    sp.set_defaults(func=cmd_unload)

    sp = sub.add_parser("abort", help="abort the currently running generation")
    sp.set_defaults(func=cmd_abort)

    sp = sub.add_parser("procs", help="list local koboldcpp processes")
    sp.set_defaults(func=cmd_procs)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args((argv or []) + ["status"])
    model_dirs = list(args.model_dir or [])
    env_dirs = os.environ.get("KOBOLDCPP_MODEL_DIR", "")
    if env_dirs:
        model_dirs.extend(d for d in env_dirs.split(os.pathsep) if d)
    if not model_dirs:
        model_dirs = ["./models"]
    client = KoboldClient(args.url, args.password, args.admin_password, args.timeout)
    return args.func(args, client, model_dirs) or 0


if __name__ == "__main__":
    sys.exit(main())
