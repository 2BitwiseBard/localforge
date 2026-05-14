# LocalForge — TODO

Last updated: 2026-05-14. Stripped completed items and items that died
with the 2026-05-12 / 2026-05-14 cleanup sweeps. The target system is
the skeleton-minimal LocalForge described in
`~/Development/NEW-OS-PLAN.md`.

---

## Pre-OS-reinstall (blocking)

OS reinstall to Fedora Workstation is planned for May 2026. Anything in
`/` gets wiped; only files in git (or on the not-yet-existing restic
repo) survive.

- [ ] **External SSD for restic backups.** 1 TB minimum, USB-C NVMe
  enclosure or Samsung T7 / SanDisk Extreme. Blocks the only durable
  backup.
- [ ] **`restic init` + baseline backup + restore drill.** Practice
  `restic restore` to a throwaway dir before the reinstall. An untested
  backup is hope, not a backup.
- [ ] **Wedding video search on cloud** (OneDrive, Google Drive,
  Dropbox, iCloud). Separate from this repo but on the pre-reinstall
  checklist.
- [ ] **Merge `cleanup-trim-2026-05-12` to `main`.** Carries the
  cleanup work through the OS wipe.
- [ ] **Tailscale device key + reauth method noted.** You re-add the
  device fresh post-install.
- [ ] **Snapshot installed package + flatpak lists** for reference:
  `apt list --installed > /mnt/models/backups/pop-pkglist.txt`,
  `flatpak list > /mnt/models/backups/flatpak-list.txt`.
- [ ] **Export browser bookmarks** (Firefox → Bookmarks → Export HTML).
- [ ] **Snapshot `/etc/fstab`** for reference.

---

## Post-reinstall (the trim path to skeleton-minimal)

Roughly in order of value-per-effort. Each step is a separate PR.

### text-generation-webui → llama-swap

The keystone migration. Unblocks streaming, deletes ~14G of webui code,
lets `infrastructure.py`, `client.py`, and `config.py` shrink by ~500
LoC, and eliminates the `:5000` legacy probes in `gpu_pool.py`.

- [ ] Install `llama-swap` binary + write `~/.config/llama-swap.yaml`
  for the active fleet (Qwen3.6-35B-A3B, Qwen3.6-27B, gemma-4-26B-A4B,
  gemma-4-E4B, Qwen3.5-2B).
- [ ] Add `~/.config/systemd/user/llama-swap.service` on a fresh port
  (`:5050`).
- [ ] Write `localforge/backends/llama_swap.py` adapter implementing
  the same surface as the current webui adapter.
- [ ] Swap default backend in `config.yaml`. Smoke test `cc-local`
  end-to-end against llama-swap.
- [ ] Add `stream=True` to `chat()` and the dashboard chat handler.
- [ ] Disable + remove `text-gen-webui.service`; `rm -rf
  ~/Development/text-generation-webui/`.
- [ ] Strip `_webui_settings` / `_webui_preset_name` /
  `_load_webui_settings_from_disk` from `config.py`.
- [ ] Remove the `:5000` probe from `gpu_pool.py`; probe only
  `:5050` (llama-swap) and `:8200` (mesh workers).

### LiteLLM → in-gateway Anthropic→OpenAI proxy

- [ ] Write `localforge/anthropic_proxy.py` (~100 LoC, mounts at
  `/anthropic` on the gateway).
- [ ] Update `cc-local` alias to point at the gateway instead of
  LiteLLM.
- [ ] Disable + remove `litellm.service`. Eliminates an
  unauthenticated `0.0.0.0:4000`.

### Final tool + route trim toward skeleton-minimal

After the mid-trim (2026-05-14, dropped 8 tool modules), the remaining
trim heads to ~10 tools / ~12 routes / ~4k Python LoC total.

- [ ] Drop `tools/{agents_tools,memory,sessions,context,config_tools}`
  unless something still depends on them post-llama-swap.
- [ ] Shrink `tools/infrastructure.py` from ~800 LoC to ~200: keep
  `health_check` and `swap_model`, drop benchmark/slot_info/token
  counts/LoRA/cache_stats/session_stats unless retained for the
  dashboard.
- [ ] Shrink `dashboard/routes.py` from ~2,180 LoC to ~400: drop the
  mesh routes (until mesh activates), the preset/LoRA routes, the
  agent routes (until agents activate), and the
  modes/characters/approval routes.
- [ ] Dashboard tab count 5 → 3 (Status, Config, Notes). Delete
  `dashboard/static/js/{mesh,agents}.js` and the corresponding tab
  panels in `index.html`.
- [ ] Reintroduce Chat tab with streaming after llama-swap is stable.

### Mesh activation arc (M2 spike)

- [ ] Bring up a second machine on Tailscale (any spare laptop;
  4–6 GB GPU is enough for the protocol smoke test).
- [ ] Run the install script from the hub
  (`/api/mesh/install-script`) and verify registration + heartbeats.
- [ ] M3 specialization: hub serves Qwen3.6-35B-A3B (chat); worker A
  serves Qwen3.6-27B (coding).
- [ ] M4 real value: `cc-local` for coding routes to Worker A
  directly; vision routes to whichever node has Gemma 4 26B loaded.

---

## Skeleton-minimal target (`~/Development/NEW-OS-PLAN.md`)

Reference snapshot of where we're going:

- ~4,000 Python LoC (currently 13,546)
- ~10 MCP tools (currently 54)
- Dashboard tabs: Status, Config, Notes (3; currently 5)
- Services: localforge gateway + llama-swap (2; currently 5)
- 0 cron agents (supervisor stays as code; just no enabled agents)
- Mesh code present but inactive until a second worker registers

---

## Out of scope (deleted in cleanup, not coming back unless asked)

- Knowledge graph (`knowledge/`, `kg_*` tools, KG tab)
- Workflow engine (`workflows/`, DAG tools, templates)
- Photos / media (`media/`, photo tab)
- Training subsystem (`tools/training.py`, training tab). Standalone
  Unsloth venv at `~/Development/unsloth-env/` only.
- Telegram bot
- 4-signal RAG (BM25 + dense + SPLADE + ColBERT + reranker)
- Research, news, daily-digest, code-watcher, yaml-schema-validator
  agents
