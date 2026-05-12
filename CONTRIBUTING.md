# Contributing

Thanks for your interest in contributing to action_inbox_ai.

## Getting started

```bash
git clone https://github.com/ritwikkanodia/action_inbox_ai.git
cd action_inbox_ai
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
```

See [README.md](./README.md) for OAuth setup and how to run the project.

## Making changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep commits focused — one logical change per commit.
3. Test manually: start the poller (`python main.py`) and the web UI (`flask --app app run --debug --port 5001`) and verify nothing is broken.
4. Open a pull request against `main` with a clear description of what changed and why.

## Adding a new source

Each source lives under `pollers/<name>/`. The minimal interface is a `poll(user)` function that writes todos to the database via `db.py`. Look at `pollers/fathom/` for a simple example.

## Code style

- Python: follow PEP 8. No formatter is enforced yet, but keep style consistent with the surrounding code.
- Keep new dependencies minimal. Add anything new to `requirements.txt` with a pinned version.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected behaviour, and actual behaviour. Include relevant log output where possible.

## License

By contributing you agree that your contributions will be licensed under the [MIT License](./LICENSE).
