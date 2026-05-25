# Security Policy

stratoclave-atelier is a FastAPI service that records agent JSONL
transcripts, organizes them into groups, and exposes REST + WebSocket
APIs for forking, freezing, and cross-session snapshot lookup. It
persists data to Postgres + pgvector and writes immutable JSONL blobs
to a content-addressed local store. Treat it as a trust boundary
between your agent transcripts and any client (UI, CLI, integration)
that talks to it.

## Supported Versions

stratoclave-atelier is currently **alpha**. No stable release has been
cut; only the latest commit on the `main` branch is supported. Once we
cut `v0.1.0`, this section will be updated to reflect supported release
lines.

| Version / Branch       | Supported          |
|------------------------|--------------------|
| `main` (latest commit) | :white_check_mark: |
| everything else        | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub
issues, discussions, or pull requests.**

Use one of the following private channels instead:

1. **Preferred -- GitHub Private Vulnerability Report.** From the
   repository's **Security** tab, click **Report a vulnerability**.
   This opens a confidential advisory visible only to maintainers.
2. **Fallback -- direct email to the maintainers.** If private
   advisories are unavailable for your account, open a regular issue
   titled *"Request for private disclosure channel"* (without
   vulnerability details) and a maintainer will provide an email
   address.

When reporting, please include:

- A clear description of the vulnerability and its impact.
- Step-by-step reproduction, including:
  - The affected commit SHA or version tag.
  - The Postgres / pgvector version, OS, and Python version.
  - A minimal proof-of-concept script or HTTP transcript.
- Whether the issue is already publicly known or has a CVE assigned.
- Your name and affiliation if you want public credit in the advisory.

## Scope

In scope:

- Source code in this repository (`src/stratoclave_atelier/`, `tests/`,
  `migrations/`, `pyproject.toml`, `docker-compose.yml`).
- Default behaviour of the public REST / WebSocket / SSE API and CLI.
- The version freeze pipeline (content-addressed blob store).
- Documentation that, if followed as-written, would produce an
  insecure integration (e.g., disabling auth without warning).

Out of scope:

- Vulnerabilities in Postgres or pgvector -- please use upstream
  channels.
- Vulnerabilities in `stratoclave`, `stratoclave-loom`, or
  `stratoclave-distill` -- those have their own security policies in
  their respective repositories.
- Misuse such as running with `ATELIER_AUTH_MODE=none` on a publicly
  exposed host.

## Our Commitment

When you report a vulnerability through the channels above, we commit
to:

1. **Acknowledge** receipt within **three business days**.
2. **Triage** and provide an initial assessment within **seven
   business days**, including a severity estimate and expected next
   steps.
3. **Keep you informed** of progress at reasonable intervals while we
   investigate and develop a fix.
4. **Coordinate disclosure.** We aim to release fixes and publish a
   public advisory within **90 days** of your report. If you need an
   earlier disclosure date, let us know and we'll work with you.
5. **Credit** you in the advisory and in release notes, unless you
   prefer to remain anonymous.

## Safe Harbor

We will not pursue legal action against security researchers who:

- Make a good-faith effort to avoid privacy violations, data
  destruction, or disruption of service.
- Report vulnerabilities privately before any public disclosure.
- Do not exploit the vulnerability beyond what is necessary to
  demonstrate it.

If you are unsure whether a specific activity is in scope, contact us
first.
