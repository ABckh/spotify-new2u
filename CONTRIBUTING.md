# Contributing

Thanks for helping. Bug reports, fixes and small improvements are welcome; for
anything bigger, open an issue first so we can agree on the approach.

## Set up

- Server: [docs/development.md](docs/development.md) runs it locally with
  Docker Compose and lists the checks.
- iPhone app: [docs/ios.md](docs/ios.md).

## Before you open a pull request

Run the same checks as CI:

```sh
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

`pytest` needs a PostgreSQL database in `DATABASE_URL` (the compose one works).
CI also runs `pip-audit` on both lock files and a full server setup and deploy
on a throwaway machine.

CI does not build the iPhone app. If you change it, run its tests (see
[docs/ios.md](docs/ios.md)) and say in the pull request that they pass.

## Rules of the code

- **No secrets in git**: no `.env`, keys, tokens, client IDs or signing files.
  Every setting comes from the environment (`.env.example` lists them).
- **Migrations expand first and remove later**, so the release before still
  runs on the migrated database (see "Migrations must work with the previous
  release" in [docs/self-hosting.md](docs/self-hosting.md)).
- **Every stored field is declared.** A new database column must be added to
  `src/app/privacy/stored_fields.py` with a plain-language label, or
  `tests/test_privacy_stored_fields.py` fails. That list is what users see as
  "what we store about you".
- **Logs hold nothing the user played and no sign-in secrets**: no track,
  album or artist names, no tokens, no callback query strings
  (`tests/test_privacy_logs.py`). Counts and our own IDs are fine.
- **Dependencies are pinned** with hashes; see "Dependencies" in
  [docs/development.md](docs/development.md) to change one.

## Old references in comments

Comments and test names often cite `WHO-<number>` and short codes like `F3`,
`S-4` or `W5`. They point to the private tracker this project was planned in
before it was open-sourced, and there is nothing public to follow them to.
Read them as history. New work is tracked in GitHub issues.

## License

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE), like the rest of the project.
