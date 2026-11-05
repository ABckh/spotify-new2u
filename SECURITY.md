# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: on GitHub,
open the repository's **Security** tab and choose **Report a vulnerability**.
Say what an attacker can do, and how to reproduce it.

You should get an answer within a week. Once a fix is out, the report can be
published, with credit to you if you want it.

## What counts

- The server: the API, sign-in, the poller, the history import, and how data
  is stored and deleted.
- The deploy and server setup scripts in `deploy/` and `scripts/`.
- The iPhone app.

Only the latest `main` gets fixes. Each instance is run by whoever hosts it,
so a problem that comes only from one instance's own settings or server is for
that host to fix; [docs/self-hosting.md](docs/self-hosting.md) describes the
setup these scripts expect.

## Running your own instance

- Keep `.env`, `.env.prod`, the deploy SSH key and the backup `age` key out of
  git and off shared machines. `.gitignore` already ignores the usual files.
- Never commit `ios/Signing.local.xcconfig`; CI fails if it is tracked.
- If a secret leaks, rotate it: see "Rotating secrets" in
  [docs/self-hosting.md](docs/self-hosting.md).
