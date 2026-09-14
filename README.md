# Copilot Agent Eval

Automated **batch regression & evaluation runner for Microsoft Copilot Studio /
Azure Bot agents**, implemented on top of the agent's standard
**[Direct Line Web Chat](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-direct-line-3-0-concepts)
protocol** — including agents configured with **"Authenticate manually → Require
users to sign in" (Entra ID)**. It replays a fixed set of questions at a
configurable pace, waits for each reply, runs assertions (keywords / error
phrases / latency SLA), and writes **Excel + self-contained HTML + JUnit XML**
reports you can gate CI with.

> **Technical form.** The runner is agent-internal-agnostic: it does not call
> Copilot Studio authoring APIs or scrape the chat UI. It drives the bot exactly
> like the embedded web chat does — over **Direct Line REST** (start a
> conversation, POST activities, poll activities with a `watermark`), and uses
> **Playwright/Chromium only to complete the Entra ID OAuthCard sign-in** that
> Direct Line requires for authenticated agents. Anything exposed on Direct Line
> can be tested the same way.

> Personal - the evaluation layer of an agent harness

Vibe-coded by a product manager as hands-on harness practice: I treat **evaluation as a first-class harness component** — the runtime lets an agent execute and be steered, while eval/regression is the layer that decides whether it can be trusted to ship. It stays framework-agnostic to the agent's internals, so the same suite works against any bot exposed on the same channel.

## Why

Agent platforms give you a great authoring canvas but almost **no offline,
repeatable regression tooling**. Clicking through dozens of questions by hand
after every prompt/knowledge change is slow and inconsistent — and once the
agent sits behind Entra ID sign-in (and sometimes a second connector consent),
plain HTTP scripts can't even start a conversation. This tool solves three
things:

1. **Replay a question set unattended**, in multi-turn or isolated sessions.
2. **Automate the sign-in dance** (Entra ID OAuthCard, MFA once, optional second
   connector consent) with a real browser via Playwright, then reuse the session.
3. **Produce comparable, CI-friendly artifacts** (pass/fail, latency, raw
   activities, JUnit XML).

## Architecture

```
cases.csv ──► Eval Runner (Python, package: copilot_agent_eval)
                │
                ├─ Direct Line REST API (directline.botframework.com)
                │     ├─ POST /conversations
                │     ├─ POST /conversations/{id}/activities
                │     └─ GET  /conversations/{id}/activities  (watermark poll)
                │
                ├─ Entra ID sign-in (Playwright / Chromium)
                │     └─ OAuthCard → sign-in URL → magic code → verifyState
                │
                └─ Reports: Excel + HTML + JUnit XML (+ raw activities)
```

Two session modes:

- **continuous** (default): one authenticated conversation per `session_group`;
  cases run in order so follow-up turns can reference earlier context
  (multi-turn testing).
- **isolated**: a new conversation (and sign-in) per case; best for independent
  regression checks. Supports `concurrency > 1` for parallel runs.

## Features

- CSV-driven cases with **AND/OR keyword assertions**, **error-phrase fail
  list**, per-case **latency SLA**, priority filter, and explicit case selection.
- Direct Line REST client with watermark-based activity polling and end-of-turn
  silence detection.
- Playwright handling of **one or more sequential OAuthCards** (Entra ID + an
  optional connector/MCP consent), MFA on first run, persistent profile for
  SSO-silent reruns, and first-query recovery so an auth placeholder is never
  mistaken for a real answer.
- Reports: colour-coded **Excel** summary, a **self-contained HTML** report with
  expandable raw activities, and **JUnit XML** for Azure DevOps / GitHub Actions.
  Exit code is `0` only when every case passes — suitable for CI gating.

## Prerequisites

- Python 3.10+
- A **Direct Line secret** (Copilot Studio → Channels → Direct Line)
- An Entra ID account that can sign in to the agent (the same kind you use in
  the hosting page)

## Install

```bash
git clone https://github.com/muzhou91/copilot-agent-eval.git
cd copilot-agent-eval
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml
cp cases.example.csv cases.csv
```

One-shot setup scripts are provided: `bash setup.sh` (macOS/Linux) or
`powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows).

### Secrets via environment variables

`config.yaml` expands `${VAR}` from the environment, so secrets never need to
live in the file (and `config.yaml` is git-ignored by default):

```bash
export DL_SECRET="dl_xxxxxxxxxxxxxxxxxxxx"
export ENTRA_USERNAME="you@yourtenant.onmicrosoft.com"   # optional, CI only
export ENTRA_PASSWORD="..."                              # optional, CI only
```

## Configuration (`config.yaml`)

See `config.example.yaml` for the fully commented template. Key sections:

| Section | Key | Notes |
|---|---|---|
| `directline` | `secret` / `endpoint` | Direct Line global endpoint preconfigured; prefer `${DL_SECRET}`. |
| `directline` | `user.id` | Must start with `dl_` (a Direct Line enhanced-auth requirement). |
| `directline` | `channel_data` | Extra context merged into every message, to replicate what the host page passes (entity/record id, roles). |
| `auth` | `headless` / `manual` | `false`/`true` for local MFA runs; `true`/`false` for an MFA-exempt CI service account. |
| `auth` | `user_data_dir` | Persistent Chromium profile; caches cookies so MFA is only needed once. |
| `runner` | `session_mode` | `continuous` (multi-turn) or `isolated` (per-case, parallelizable). |
| `runner` | `max_wait_seconds` / `quiet_seconds` | Per-turn timeout and end-of-turn silence window. |
| `assertions` | `fail_on_error_phrases` | Replies containing these are marked FAIL. |
| `report` | `formats` | Any of `excel`, `html`, `junit`. |

## Test cases (`cases.csv`)

| Column | Description |
|---|---|
| `case_id` | Unique ID, e.g. `TC001`. |
| `query` | Message to send (quote it if it contains a comma). |
| `expected_keywords` | `;` separates AND groups; `\|` separates OR within a group. `create\|new;request` = `(create OR new) AND request`. |
| `session_group` | Cases sharing a group run in one conversation (multi-turn). |
| `priority` | `P1` / `P2` … filterable with `--priority`. |
| `max_latency_ms` | Optional per-case latency SLA. |
| `notes` | Free text. |

## Run

```bash
python -m copilot_agent_eval                      # full suite
python -m copilot_agent_eval -c config.yaml -t cases.csv
python -m copilot_agent_eval --priority P1        # only P1
python -m copilot_agent_eval --ids TC001,TC003    # specific cases
python -m copilot_agent_eval --auth-only          # just verify Direct Line + sign-in
python -m copilot_agent_eval -v                   # debug logging
```

Reports are written to `reports/`:

- `copilot_agent_results_<ts>.xlsx` — colour-coded results + summary sheet
- `copilot_agent_report_<ts>.html` — self-contained report with expandable raw activities
- `copilot_agent_results_<ts>.xml` — JUnit XML for CI
- `reports/raw_activities/` — full JSON of every turn (adaptive cards included)

### CI gate example

```bash
python -m copilot_agent_eval --priority P1
# exit code 0 = all pass, 1 = any failure → fail the pipeline
```

Unattended CI requires a service account exempted from MFA via Conditional Access
(`auth.manual: false`, `auth.headless: true`); otherwise use the persistent
profile with one interactive MFA login.

## Safety & scope

- Use only against agents **you are authorized to test**. The runner automates
  sign-in solely to exercise your own agent and never modifies data.
- Never commit `config.yaml`, `cases.csv`, `reports/`, or the Playwright auth
  profile — the bundled `.gitignore` excludes them; only the `*.example.*`
  templates are meant to be public.

## Roadmap

- [ ] LLM-as-judge scorer alongside keyword assertions
- [ ] Response-schema / card-shape assertions
- [ ] Trend view across runs (regression delta, latency percentiles)
- [ ] Streaming activity assertions

## License

[MIT](LICENSE)
