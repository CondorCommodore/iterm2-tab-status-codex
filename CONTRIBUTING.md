# Contributing

Thanks for wanting to improve claude-code-iterm2-tab-status. Here's how to get going.

## Dev setup

This project uses [uv](https://docs.astral.sh/uv/) to manage the Python development environment. Please install it first:

```bash
# Install uv (if you haven't already)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and set up:

```bash
git clone https://github.com/<your-fork>/claude-code-iterm2-tab-status.git
cd claude-code-iterm2-tab-status
uv sync                        # creates .venv and installs dev dependencies
uv run pre-commit install      # installs git pre-commit hooks
claude plugin install --local .
```

This installs the plugin from your local checkout. Changes to hook scripts and commands take effect on next Claude Code session start.

Pre-commit hooks will automatically run `ruff` (lint + format) and `shellcheck` on every commit.

> **Note:** Do NOT use `pip install` directly. Always use `uv` to keep the environment reproducible and isolated.

## Running tests

Shell tests (hooks, bootstrap, and plugin structure):

```bash
bash tests/test_hooks.sh
bash tests/test_bootstrap.sh
bash tests/test_plugin_structure.sh
```

Python tests (iTerm2 adapter):

```bash
uv run pytest tests/test_adapter.py -v
```

All must pass before you open a PR. CI will run these automatically on every push and PR.

## Code style

- **Bash** — run `shellcheck` on every `.sh` file. Zero warnings.
- **Python** — run `uv run ruff check` and `uv run ruff format --check .`. Zero warnings.

## Testing changes in iTerm2

1. Run `/iterm2-tab-status:setup` in Claude Code to deploy the adapter to iTerm2 AutoLaunch.
2. Set the debug env var so the adapter logs verbosely:

   ```bash
   export CLAUDE_ITERM2_TAB_STATUS_LOG=DEBUG
   ```

3. Open **Scripts > Manage > Console** in iTerm2 to see log output.
4. Run Claude Code in a tab and trigger a prompt — you should see the status prefix change and the logs confirm it.

Signal files live under `/tmp/claude-tab-status/`. Inspecting them can help debug matching issues.

## PR process

1. Fork the repo and create a feature branch.
2. Make your changes. Keep commits focused.
3. Ensure all tests pass and linters are clean.
4. Open a PR against `main`. Describe what you changed and why.

That's it.
