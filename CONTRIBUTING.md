# Contributing

Thanks for helping! Small, focused pull requests are easiest to review.

## Set up

```bash
uv sync
uv run pytest
uv run ruff check .
```

## Ground rules

- Bug fixes come with a test that fails before the fix and passes after it.
- Tests build their own small folders in a temporary directory (`tests/conftest.py`); never commit real files.
- Anything that moves files must keep the three promises: never replace, stay inside the folder, journal first.
- No network access or telemetry, and no runtime dependencies without an issue first.

## Where things are

`config.py` reads and checks the rules file; `core.py` plans, moves and undoes; `cli.py` is the command line;
`doctor.py` the environment report; `example.toml` the rules file `init` writes.
