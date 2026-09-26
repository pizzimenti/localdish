# working on localdish (people and coding agents alike)

Read [DESIGN.md](DESIGN.md) first. It is the contract.

## rules

- **Standard library only.** No imports outside Python's standard library, in the package or the tests.
- **Python 3.9 compatible.** Put `from __future__ import annotations` at the top of every module. Do not use:
  `match`; `X | Y` outside annotations; parenthesized context managers; `zip(strict=)`; `str.removeprefix`
  is fine (3.9).
- **Tests are offline:** `python3 -m unittest discover -s tests -q` must pass with no network and no dish. Use the
  fixtures and fakes. A test never opens a socket to anything but `127.0.0.1`.
- **Be gentle with real gear.** Manual live checks follow the cadence table in DESIGN.md. A control (restart, settings,
  stow, speed test) is **never** sent to a real device from a test or a script. Controls are tried live by hand, with
  the dish's owner present.
- **No personal data in the repo.** Raw captures hold device ids, client names, MACs and addresses. Keep them outside
  the repository (the `captures/` folder is ignored). Commit only scrubbed fixtures, and no credentials, ever.
- **Interface text is lowercase** except proper names. Every number carries its unit. Say what is stale.
- **Small, readable code.** Comments say why, not what. Match the surrounding style.
- **Changes** go in `CHANGELOG.md` under `## unreleased`. Releases are annotated tags (`v0.1.0`) whose message is that
  version's changelog.
