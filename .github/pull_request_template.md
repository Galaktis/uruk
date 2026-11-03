## What and why

<!-- What does this change do, and why is it needed? Link the issue it closes: "Closes #123". -->

## How it was tested

<!-- The tests you added or ran, and anything you checked by hand (Studio, MCP, rendered site). -->

## Checklist

- [ ] `uv run ruff format .`, `uv run ruff check .` and `uv run ty check` pass
- [ ] `uv run coverage run -m pytest && uv run coverage report` passes (coverage stays at 95% or more), and new behaviour is covered by an outside-in test
- [ ] Docs updated where behaviour changed (README, ARCHITECTURE.md, configuration in all three places)
- [ ] `tenants/seed/default_site.json` regenerated if `scripts/default_site/` changed
