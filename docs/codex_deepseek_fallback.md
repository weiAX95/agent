# Codex → DeepSeek automatic fallback

`codex-auto` checks the signed-in ChatGPT Codex quota through Codex App Server.
It starts normal Codex while quota remains and starts the local DeepSeek Responses
adapter with the `deepseek` Codex profile after a quota window reaches 100%.

## One-time setup

```bash
codex-auto --setup-key
```

The key is stored at `~/.config/codex-deepseek/env` with permissions `0600`.
It is never written to the Codex TOML profile or this repository.

## Usage

```bash
codex-auto                 # interactive Codex, automatic provider choice
codex-auto --status        # print current quota windows
codex-auto exec "task"     # non-interactive Codex
codex-auto --force-deepseek
codex-auto --threshold 95  # switch before a window is completely exhausted
```

The check happens when a Codex process starts. An already-running interactive
conversation is not migrated between providers. DeepSeek receives model prompts,
code excerpts, tool arguments, and tool results sent through the model request.

Environment overrides:

- `DEEPSEEK_API_KEY`: use a session-only key instead of the saved key file.
- `DEEPSEEK_MODEL`: defaults to `deepseek-chat`.
- `DEEPSEEK_BASE_URL`: defaults to `https://api.deepseek.com`.
- `CODEX_FALLBACK_THRESHOLD`: defaults to `100`.
- `CODEX_DEEPSEEK_PORT`: defaults to `18443`.
