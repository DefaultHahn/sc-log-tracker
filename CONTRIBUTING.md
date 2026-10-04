# Contributing

Thanks for helping out! The most useful contributions are **log lines the tracker doesn't understand yet**, especially after a new Star Citizen patch.

## Reporting a missing or wrong event

[Open an issue](https://github.com/DefaultHahn/sc-log-tracker/issues/new/choose) with:

- the Star Citizen version (shown in the tracker's header),
- what happened in game,
- the matching line(s) from your `Game.log` (in the dashboard, click the event or use the raw log search).

**Before you paste log lines, remove personal information:** your handle, your `playerGEID` / player ID, other players' names and any IP addresses. Replace them with something like `Player1`.

## Development setup

You only need Python 3.9+. The tracker itself has no dependencies; the tools below are for development.

```bash
git clone https://github.com/DefaultHahn/sc-log-tracker.git
cd sc-log-tracker
pip install pytest ruff
pytest            # run the tests
ruff check .      # lint
python sc_log_tracker.py --replay examples/demo_game.log --speed 60
```

## Adding a log pattern

Everything lives in [`sc_log_tracker.py`](sc_log_tracker.py):

- **HUD notifications** (lines with `Added notification "...`) are handled in `Parser._notif`. The text arrives already cleaned and joined across lines, so you usually only need one `re.match`.
- **All other lines** go through `Parser._other`. Check for a cheap substring first (`if "<SomeTag>" in line:`), then run the regex.
- **Location codes** are translated in `pretty_loc` / `pretty_dest`. Only add a name to `STATIONS` if you've confirmed it in game.

Every new pattern needs a test in `tests/` with a real (anonymized) sample line. Keep event titles short and in sentence case, and put the details in the second field.

## Pull requests

- One topic per pull request.
- `pytest` and `ruff check .` must pass. GitHub Actions runs both on Windows and Linux.
- Add a line to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md).

## Releasing (maintainers)

1. Bump `__version__` in `sc_log_tracker.py`.
2. Move the `[Unreleased]` notes in `CHANGELOG.md` to a new version heading.
3. Commit, then tag and push: `git tag v1.2.3 && git push origin main v1.2.3`.

The [release workflow](.github/workflows/release.yml) runs the tests, builds `SC-Log-Tracker.exe`, and publishes a GitHub release with the changelog notes.
