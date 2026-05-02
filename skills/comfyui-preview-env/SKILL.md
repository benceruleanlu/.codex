---
name: comfyui-preview-env
description: Spin up isolated ComfyUI backend instances and optional ComfyUI_frontend Vite preview servers for Playwright, Chrome DevTools MCP, and local UI verification. Use this when you need a self-managed test backend, a preview URL for current frontend workspace changes, or machine-readable port/URL output for agents.
---

# ComfyUI Preview Env

## When To Use

Use this skill when you need any of the following:

- A self-managed ComfyUI backend instead of relying on a manually started local install.
- An isolated backend instance for a specific agent, task, or review.
- A preview URL for the current `ComfyUI_frontend` workspace that Chrome DevTools MCP or Playwright can open.
- Machine-readable `port`, `url`, and `TEST_COMFYUI_DIR` values for automation.

## Commands

Run the bundled launcher. Use `python3` on this Mac:

```bash
python3 /Users/ben/.codex/skills/comfyui-preview-env/scripts/comfyui_preview_env.py --help
```

Core commands:

- `start`: start backend only
- `preview`: start backend plus a Vite dev server for the current frontend workspace
- `status`: show one instance
- `list`: show all known instances
- `stop`: stop one instance

`preview` accepts `--frontend-mode` with only these values:

- `localhost`: preview talks to the local backend
- `cloud`: preview talks to `https://testcloud.comfy.org/`
- `desktop`: preview uses `vite.electron.config.mts` and `DISTRIBUTION=desktop`

Useful feature flags:

- `--enable-manager`: install and start backend with ComfyUI Manager enabled.
- `--manager-package`: override the Manager package spec; defaults to `comfyui-manager==4.2.1`.
- `--frontend-install missing|always|never`: controls `pnpm install`; defaults to `missing` for faster review startup and to avoid symlinked `node_modules` prompts.
- `--backend-arg`: repeatable pass-through arg for `main.py`.
- `--backend-env KEY=VALUE`: repeatable backend environment override.
- `--frontend-env KEY=VALUE`: repeatable frontend environment override, useful for deterministic Manager/Algolia review setup.

## Recommended Flow

For Playwright or Chrome DevTools preview of current frontend changes, prefer `preview`:

```bash
python3 /Users/ben/.codex/skills/comfyui-preview-env/scripts/comfyui_preview_env.py \
  preview \
  --name pr-10303 \
  --frontend-repo "$PWD" \
  --frontend-mode localhost \
  --json
```

For Manager UI review, prefer:

```bash
python3 /Users/ben/.codex/skills/comfyui-preview-env/scripts/comfyui_preview_env.py \
  preview \
  --name pr-11713-manager \
  --frontend-repo "$PWD" \
  --frontend-mode localhost \
  --enable-manager \
  --json
```

For Desktop/Electron frontend behavior against a local backend:

```bash
python3 /Users/ben/.codex/skills/comfyui-preview-env/scripts/comfyui_preview_env.py \
  preview \
  --name desktop-ui-check \
  --frontend-repo "$PWD" \
  --frontend-mode desktop \
  --json
```

For backend-only work:

```bash
python3 /Users/ben/.codex/skills/comfyui-preview-env/scripts/comfyui_preview_env.py \
  start \
  --name queue-debug \
  --json
```

## What The Launcher Manages

- A cached ComfyUI bare repo under `~/.codex`
- Isolated backend worktrees per instance
- Shared backend virtualenvs keyed by backend commit
- Per-instance runtime metadata, logs, ports, and process ids
- Per-run logs under `runs/<run_id>/logs/`
- `tools/devtools` symlinked into the backend when a frontend repo is available
- Optional Vite dev server wired to the backend through `DEV_SERVER_COMFYUI_URL`
- Optional Manager-enabled backend startup for Manager UI review

## Output Contract

With `--json`, expect:

- `backend.url`
- `backend.port`
- `run_id`
- `paths.worktree_dir`
- `paths.logs_dir`
- `env.TEST_COMFYUI_DIR`
- `env.PLAYWRIGHT_TEST_URL`
- `frontend.url` and `frontend.port` when `preview` is used
- `frontend_mode` when `preview` is used
- `features.manager.enabled` and `features.manager.package`
- `backend.args`
- `frontend.vite_config`, `frontend.mode`, and `frontend.install` when `preview` is used

Use `frontend.url` for Chrome DevTools MCP and Playwright when previewing local frontend changes. Use `backend.url` only when backend-direct mode is enough.

## Notes

- Default backend repo: `https://github.com/Comfy-Org/ComfyUI.git`
- Default backend ref: `master`
- `preview` defaults to `--frontend-install missing`, so it runs `pnpm install --frozen-lockfile` only when `node_modules` is absent. Use `--frontend-install always` when dependency freshness matters more than startup speed.
- Prefer unique instance names per agent or task to avoid collisions.
