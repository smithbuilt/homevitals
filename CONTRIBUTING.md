# Contributing

Bug reports and fixes are welcome, especially reports of which scales and blood pressure monitors work.

- Never paste passwords, tokens, email addresses, profile IDs or health numbers into an issue. `homevitals --doctor` output and `sync.log` are safe to share after a quick look.
- Development: `uv venv`, `uv pip install -e .`, then `python -m pytest` and `ruff check .`. Every test runs on fake data; none may contact a real server (the test setup blocks the network).
- Keep features general: HomeVitals is used by households with different devices, so device-specific options belong in the settings file, documented in the README.
