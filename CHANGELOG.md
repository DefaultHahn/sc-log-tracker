# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.1] - 2026-10-05

### Fixed
- `start.bat` no longer leaves a console window open: it starts the Python version in the background (with `pyw`/`pythonw`), just like the Windows app. Quitting from the dashboard now ends everything.
- Starting a newer version while an older one is still running in the background (for example after downloading an update) now replaces the old one instead of opening its dashboard. Open dashboard tabs reload to the new version on their own.
- On Windows, the tracker could share its port with another program instead of moving on to the next free one.
- Error messages show up as a message box when there is no console (also for the Python version started with `start.bat`).

## [1.2.0] - 2026-10-05

### Added
- **Server history:** a new **Servers** tab lists every shard you joined in the selected time range, with region, start, end, duration and why you left (game closed, moved to another server, disconnected, crash). Rejoins of the same shard within five minutes count as one visit. A **By server** view shows visits and total time per shard, and a bar shows your time per region. Click a visit to see its events.
- The "Now" panel shows the region of the current server.

### Changed
- Every server join is now an event, not just a change of shard. Your history is re-imported once from the logs that still exist (takes a few seconds).

## [1.1.0] - 2026-10-05

### Added
- **Complete history:** every session is stored in a local SQLite archive (`%LOCALAPPDATA%\SC Log Tracker`). On every start the tracker imports new logs from Star Citizen's `logbackups` folder, so sessions played without the tracker are filled in. Sessions are re-imported automatically when a newer version parses them differently.
- **Time range filter** with date and time ("From"/"To"), quick picks (this session, today, 24 hours, 7 days, 30 days, all) and a **history list** of all sessions. The feed shows day headers and loads more as you scroll.
- **Settings:** choose the `Game.log` with a file dialog, from the installations found on the PC, or by pasting a path. The settings open by themselves when no `Game.log` is found.
- **Start with Windows** option (runs in the background).
- **Quit** button in the dashboard, and a single instance: starting the app again opens the running dashboard.
- `--background` and `--data-dir` options.
- Recognition of more notifications: objective failed, contract withdrawn, left party, invitation declined, bricking of whole ships.
- Readable names for NPCs in kill events from older patches.

### Changed
- The Windows app no longer opens a console window. Messages go to `tracker.log` in the data folder.
- The status shows whether the game is actually running, based on the log's own timestamps.
- Hotfix builds show their real version (e.g. `4.7.0-hotfix`) instead of `1.0.x`.
- Legacy logs (4.1 to 4.3) only show kills and vehicle losses that involve you, and no longer list repeated insurance claims.
- POST requests to the local server now require a custom header, and requests for other host names are refused.

### Removed
- The session statistics panel (replaced by the time range and history).
- The console prompt for the `Game.log` path (replaced by the settings dialog).

## [1.0.0] - 2026-10-04

First public release.

### Added
- Live event feed for the Star Citizen `Game.log`: travel, ship, missions, trade, combat, law, party, notices and errors.
- "Now" panel with location, jurisdiction, armistice/monitored zone, ship, quantum target and server.
- Session statistics: play time, quantum jumps, contracts, objectives, deaths, purchases, blueprints, offences and problems.
- Readable names for stations, outposts, moons, jump points, ships, items and shops (e.g. `RR_P3_LEO` → Orbituary).
- Recognition of all HUD notifications seen in 4.10, including multi-line ones (party, money transfers, ship channels).
- Crash reports with the cause, plus disconnects told apart: exit to menu, server drop, inactivity.
- De-duplication of noisy messages: jurisdiction flips at borders, armistice/monitored flapping, party reconnects.
- Raw log view with highlighting, search, "matches only" filter and auto-scroll.
- Replay of old sessions (`--replay`), JSON export, automatic `Game.log` discovery, new-session detection.
- Windows executable built by GitHub Actions for every release.

[Unreleased]: https://github.com/DefaultHahn/sc-log-tracker/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/DefaultHahn/sc-log-tracker/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/DefaultHahn/sc-log-tracker/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/DefaultHahn/sc-log-tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/DefaultHahn/sc-log-tracker/releases/tag/v1.0.0
