# koboldctl

A small, dependency-free Python CLI to **see what your KoboldCpp server is doing** and **switch models on the fly** — no need to restart the process or dig through logs.

It talks to the KoboldCpp HTTP API and adds:

- `status` — server version/capabilities, loaded model, context size, tok/s, queue/busy state, uptime, and any local `koboldcpp` processes
- `watch` — a live dashboard; in a real terminal it also shows a model list where you can move with up/down and press **enter** to hot-swap, `u` to unload, `a` to abort generation
- `models` — everything the server can load (admin dir) plus GGUF files found in local model dirs
- `load <name>` / `unload` — switch models via the admin API and wait until the new model is actually loaded
- `abort` — abort the current generation
- `procs` — list local koboldcpp processes (pid, port, model, admin/router flags)

## Requirements

- Python 3.8+ (standard library only — no pip install needed)
- A running KoboldCpp server

## Enabling on-the-fly model switching

Model switching uses KoboldCpp's admin endpoint (`POST /api/admin/reload_config`), so start the server with admin mode and a folder of models/configs:

```bash
./koboldcpp --admin --admindir ./models --adminpassword changeme
# optionally also: --routermode   (lets API requests pick the model by name too)
```

Drop your `.gguf` files (and optional `.kcpps`/`.kcppt` config presets) into `./models`. `koboldctl load <file>` then hot-swaps without restarting the whole server process. Without `--admin --admindir`, `status`/`watch`/`models`/`abort` still work, but `load`/`unload` will be rejected by the server.

## Usage

```bash
python3 koboldctl.py status
python3 koboldctl.py watch                 # live dashboard (interactive picker in a TTY)
python3 koboldctl.py models                # '*' marks the currently loaded model
python3 koboldctl.py load my-model-Q4_K_M.gguf
python3 koboldctl.py load some-model.kcpps --baseconfig default.kcpps
python3 koboldctl.py unload                # or: load unload
python3 koboldctl.py abort
python3 koboldctl.py procs
```

### Configuration

Flags (also available as environment variables):

| Flag | Env var | Default |
|---|---|---|
| `--url` | `KOBOLDCPP_URL` | `http://127.0.0.1:5001` |
| `--password` | `KOBOLDCPP_PASSWORD` | — |
| `--admin-password` | `KOBOLDCPP_ADMIN_PASSWORD` | — |
| `--model-dir` (repeatable) | `KOBOLDCPP_MODEL_DIR` (split on `:`) | `./models` |

Passwords are sent as `Authorization: Bearer <token>`; the admin password is tried first, then the regular password.

Example for a password-protected remote server:

```bash
export KOBOLDCPP_URL=http://192.168.1.10:5001
export KOBOLDCPP_ADMIN_PASSWORD=changeme
python3 koboldctl.py watch
```

## API endpoints used

- `GET /api/extra/version` — capabilities (version, admin/router mode, features)
- `GET /api/v1/model` — currently loaded model
- `GET /api/extra/perf` — prompt/generation tok/s, queue, idle, uptime, totals
- `GET /api/v1/config/max_context_length`, `GET /api/v1/config/max_length`
- `GET /api/admin/list_options` — loadable files in the admin dir (admin auth)
- `GET /v1/models` — Ollama-style model list (includes admin dir entries in router mode)
- `POST /api/admin/reload_config` `{"filename": "..."}` — hot-swap (`unload_model` / `initial_model` are special values)
- `POST /api/extra/abort` — abort current generation

## Development

`tests/mock_koboldcpp.py` is a loopback mock of the API above; `tests/smoke_test.py` runs every subcommand against it:

```bash
python3 tests/smoke_test.py
```
