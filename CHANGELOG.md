# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/DefaultHahn/sc-log-tracker/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/DefaultHahn/sc-log-tracker/releases/tag/v1.0.0
