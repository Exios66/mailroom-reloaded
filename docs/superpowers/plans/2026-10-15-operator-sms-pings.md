# Operator SMS Pings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (the model/effort per task is fixed in **Execution** below). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a mailroom runs (live production or a research/experiment run), text the operator's own mobile or Google Voice number. Send a digest of run metrics, events and Modal/OpenRouter status on a configurable cadence (default 10 min). A configurable watcher scan (default 60 s) stays silent while healthy and texts only on failures that need a human, followed by one "recovered" text.

**Architecture:** Each repo gets three small modules and no new package directory.
- **Pure core** (`pager_core.py`): config resolution, alert rules with the frequency state machine, and the SMS renderers. It is copied verbatim between the two repos.
- **Transport** (`sms_notify.py` / `sms.py`): sends through Twilio REST, the email gateway, or the console.
- **Host** (`pager.py`): collects the repo-specific snapshot, runs the scheduler, and provides the CLI.

Every tunable lives in a `pager:` block of the repo's existing `taxonomy.yaml`. Selected env vars override it, following the repos' documented order: env → taxonomy → default. Provider status is passive: LLM call outcomes are recorded at the existing retry choke point, so the pager never wakes a scaled-to-zero Modal GPU.

**Tech Stack:** Python 3.11, `httpx` (already a dependency in both repos), stdlib `fcntl`/`zoneinfo`/`json`, structlog, pytest. **No new dependencies.**

**Spec:** the operator requests of 2026-10-15 in this session. R1–R12 below are the spec of record.

## Requirements

| # | Requirement |
|---|---|
| R1 | Recipient is the operator's **own mobile number or Google Voice number** (either, or both). |
| R2 | Works for **production/live** and **research/experimental** runs. |
| R3 | Periodic digest, default **every 10 minutes**, while the mailroom is live. |
| R4 | Digest includes **Modal job / OpenRouter** status. |
| R5 | Digest carries full run metrics & events, concise, with emoji house-style formatting (like the Gmail echo/status emails). |
| R6 | The local/dev server running the mailroom delivers the SMS. |
| R7 | Watcher scans default **every 60 s**; adds content **only** on failures needing attention. |
| R8 | Healthy scans are **silent**. |
| R9 | **Full frequency configuration**: digest cadence, scan cadence, re-alert cooldowns, recovery debounce, idle behavior, quiet hours, hourly cap, mute, and per-rule enable/threshold. |
| R10 | Implemented by subagents: **Sonnet @ high** or **Haiku @ max**. |
| R11 | Concise, minimal footprint, placed in each repo's existing layout. No bloat. |
| R12 | Same behavior, copy and config keys in **both** repos. |
| R13 | Text alerts are **toggleable and default OFF**. |
| R14 | When a **live production** mailroom starts, the operator is **asked** whether to enable text alerts, and for the number to use. |
| R15 | **CLI commands in both repos** cover: turn on/off, set/add/remove the number, show status, and send a test text (to the saved number or an ad-hoc one). |

## Global Constraints

- **No new dependencies, no new package directories, no new API routes, no new docs pages, no fixture directories.** Docs go into existing files only.
- Size budget (source lines, excluding tests). Exceeding a budget by more than 20% needs a reason in the commit body.

  | Module | Max lines |
  |---|---|
  | `pager_core.py` | 380 |
  | `sms_notify.py` / `sms.py` | 150 |
  | `pager.py` (llm-mailroom) | 430 (snapshot + host + settings + prompt + CLI) |
  | `pager.py` (mailroom-reloaded) | 340 (CLI verbs live in `cli.py`) |
  | `provider_health.py` / `health.py` | 90 |

- `pager_core.py` is **byte-identical** in both repos, apart from its one-line module docstring header naming the repo. It imports only the stdlib: no repo imports, no I/O, no clock reads. `now` is always a parameter.
- Default **off** (R13). Sending requires the resolved `enabled` **and** at least one valid recipient.
- **Operator settings file** `<base_dir>/pager_settings.json` = `{"enabled": bool, "recipients": [str], "updated_at": iso, "updated_by": "cli"|"prompt"|"flag"}`. Only the CLI, the start prompt and `--pager/--no-pager` write it, via an atomic replace. For `enabled` and `recipients`, the precedence is **settings file → env → taxonomy → default**, so an interactive toggle always wins over a stale env var. Every other key keeps env → taxonomy → default. A running pager re-reads the file whenever its mtime changes, so toggles apply within one scan and no restart is needed.
- Fail-soft: pager code never raises into its host (watchdog, watcher, API lifespan, run scripts). Failures log one `pager_*` structlog event.
- Hermetic tests: no network. Sends go through `set_transport_override()`, and HTTP takes an injectable `httpx.Client` (`httpx.MockTransport` in tests).
- Secrets are never logged. Numbers are logged only via `mask_number()` → `+1•••••1234`.
- **Never actively probe a Modal tier by default.** A request to a scaled-to-zero `*.modal.run` URL cold-starts a GPU. Use passive health plus the read-only `modal container list --json`; the opt-in probe only touches tiers that are already warm.
- Email anti-spam directive (2026-09-04, `pipeline/status_notify.py`): the pager never emails `MAILROOM_STATUS_EMAIL`.
- One sender per data dir: `fcntl.flock` on `<base_dir>/pager.lock`. State persists at `<base_dir>/pager_state.json` (temp file + `os.replace`).
- Each behavior change carries its test, a `CHANGELOG.md` line and a doc update in the same commit (both repos' AGENTS.md).

## Recipients: personal number or Google Voice (R1)

| Recipient | `transport: twilio` (recommended) | `transport: email_gateway` (free fallback) | `transport: console` |
|---|---|---|---|
| Personal mobile (`+1…`) | ✅ | ✅ Use the carrier gateway address (e.g. `6085551234@vtext.com`, T-Mobile `@tmomail.net`). Best-effort; some carriers (AT&T, 2025) have retired their gateways. | dev only |
| Google Voice (`+1…`) | ✅ GV receives SMS from Twilio **long-code / toll-free** senders. GV does **not** receive short codes, so never configure a short code. | ❌ GV has no carrier gateway | dev only |

Recipient rules that the implementation enforces and the operator docs state:
- `recipients` is a list. `MAILROOM_PAGER_TO="+16085551234,+14145550000"` sends each text to every entry.
- For `twilio`/`console`, entries must be E.164 (`^\+[1-9]\d{7,14}$`). For `email_gateway`, entries must be email addresses. A mismatched entry is dropped with a `pager_recipient_invalid` warning. Zero valid entries disables the pager.
- A Google Voice number is **receive-only** here. GV has no sending API, so it can never be the *sender*.
- Twilio **trial** accounts only deliver to numbers verified in the Twilio console, so verify the mobile/GV number there. Trial messages carry a "Sent from your Twilio trial account" prefix.
- US delivery from a Twilio number requires **A2P 10DLC** (sole-proprietor brand) or **toll-free verification**. A verified toll-free sender is the simplest choice for one operator texting themselves.
- In Google Voice, save the Twilio sender as a contact so GV's spam filter doesn't hide the texts.

## Configuration (R9): `pager:` block in `taxonomy.yaml`

Added to `llm-mailroom/src/config/taxonomy.yaml` and `mailroom-reloaded/src/mailroom_reloaded/config/taxonomy.yaml` with **exactly** these keys and defaults. `pager_core.PagerConfig.resolve` validates them. An unknown key logs a `pager_config_unknown_key` warning, and a bad value falls back to the default with `pager_config_invalid`.

```yaml
pager:
  enabled: false                 # default OFF; `pager on|off` (settings file) > env MAILROOM_PAGER > this
  recipients: []                 # `pager number …` (settings file) > env MAILROOM_PAGER_TO (comma list) — mobile and/or Google Voice, E.164
  transport: twilio              # twilio | email_gateway | console        env MAILROOM_PAGER_TRANSPORT
  mode: auto                     # auto | production | research           env MAILROOM_PAGER_MODE
  style: emoji                   # emoji | ascii (ascii = GSM-7, ~2.3x cheaper)   env MAILROOM_PAGER_STYLE
  max_chars: 640                 # per text, floor 160
  timezone: ""                   # IANA name; blank = host local          env MAILROOM_PAGER_TZ
  digest:
    production_minutes: 10       # 0 disables digests                    env MAILROOM_PAGER_DIGEST_MINUTES (sets both)
    research_minutes: 10
    align_to_clock: true         # fire at :00/:10/:20… instead of start-relative
    when_idle: true              # false: skip windows with 0 docs and no open alerts…
    idle_heartbeat_minutes: 60   # …but still send one "· IDLE" digest this often (0 = never)
  scan:
    seconds: 60                  # floor 15                               env MAILROOM_PAGER_SCAN_SECONDS
    degraded_after_scans: 2      # warnings must persist N scans before texting
    recover_after_scans: 2       # an alert must stay clear N scans before "recovered"
  alerts:
    critical_cooldown_minutes: 15  # re-text a still-open critical
    warning_cooldown_minutes: 60   # re-text a still-open warning
    send_recovered: true
    max_texts_per_hour: 12         # all texts; see "Frequency state machine"
  quiet_hours:
    window: ""                   # "22:00-07:00" (may cross midnight); blank = off   env MAILROOM_PAGER_QUIET_HOURS
    allow: critical              # none | critical | all — what may text during quiet hours
    catch_up_digest: true        # one digest when quiet hours end
  rules:                         # each: enabled + its thresholds; severity fixed per rule
    watcher_down:        {enabled: true}
    provider_down:       {enabled: true}
    provider_degraded:   {enabled: true, error_rate: 0.25, min_calls: 4}
    openrouter_credit:   {enabled: true, below_usd: 1.0}
    docs_failed:         {enabled: true, critical_at: 5}
    review_backlog:      {enabled: true, threshold: 20}
    stuck_docs:          {enabled: true, minutes: 15}
    ingestion_paused:    {enabled: true}
    outbox_dead:         {enabled: true}      # llm-mailroom only; ignored in mailroom-reloaded
    run_stalled:         {enabled: true, minutes: 15}
  providers:
    openrouter_cache_minutes: 5
    probe_modal: false           # opt-in; only probes tiers that already have a warm container
```

Secrets are env-only: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` **or** `TWILIO_MESSAGING_SERVICE_SID`. Email-gateway sending reuses each repo's existing SMTP settings. `mode: auto` resolves to `research` when the environment name is `pilot`/`misc`/`mock`/`eval`, and to `production` otherwise. The environment name comes from `OBSERVABILITY_ENVIRONMENT` in llm-mailroom and `obs.run_context.environment_name()` in mailroom-reloaded.

### Toggle, number and test — CLI (R13, R15)

The same verbs exist in both repos. In `llm-mailroom` they run as `PYTHONPATH=src python -m pipeline.pager <verb>` (argparse); in `mailroom-reloaded` as `mailroom pager <verb>` (a typer sub-app on the existing `cli.py` app). Changes take effect in a running mailroom within one scan.

| Command | Effect |
|---|---|
| `pager status [--full]` | `ON`/`OFF`, masked recipients, transport, deliverable or not (with the missing env var names), mute/quiet state, last text sent. `--full` adds every resolved key and its source (`settings`/`env`/`taxonomy`/`default`). |
| `pager on [--to NUMBER]` | Enable. `--to` replaces the saved recipients. With no saved number on a TTY, it prompts for one; with none on a non-TTY, it exits 2 with `no number saved — use: pager on --to +1…`. It warns, but still enables, when Twilio credentials are missing. |
| `pager off` | Disable. The saved numbers are kept. |
| `pager number set NUMBER` | Replace the recipients with one number. |
| `pager number add NUMBER` / `pager number remove NUMBER` | Edit the list. For example, keep both a mobile and a Google Voice number. |
| `pager number list` | Masked list; `--show` prints the numbers unmasked. |
| `pager test [--to NUMBER]` | Send `✅ MAILROOM PAGER TEST · <host> · <time>` to the saved numbers, or to `--to` without saving it. Works while the pager is OFF. Prints each `SendResult`; exits 1 if any send failed. |
| `pager preview [--mode production\|research] [--alert]` | Print a real digest or alert to the console; never sends. |
| `pager mute 2h\|30m\|off` | Suppress everything except criticals (which still obey `quiet_hours.allow`). |
| `pager run` | Run the pager in the foreground (standalone host). |

Number input is validated with `valid_recipients()`. A bad number exits 2 with `not a valid number — use E.164, e.g. +16085551234`. A US 10-digit entry (`6085551234` or `608-555-1234`) is normalized to `+16085551234`, and the CLI echoes the normalized number before saving.

### Live-production start prompt (R14)

Hooked into the production entry points: `llm-mailroom` `python -m api.main` and `python -m pipeline.watcher` (`__main__` blocks), and `mailroom-reloaded` `mailroom serve` and `mailroom watch`. The prompt is shown only when **all** of these hold:
- the resolved mode is `production`;
- stdin and stdout are TTYs;
- `MAILROOM_PAGER_PROMPT` is not `0`;
- neither `--pager` nor `--no-pager` was passed.

Fixed copy:
```
📱 Text alerts are OFF for this live run. Enable them? [y/N]:
   Number to text (mobile or Google Voice, e.g. +16085551234):
   Send a test text now? [Y/n]:
```
When alerts are already ON, the first line reads `📱 Text alerts are ON → +1•••••1234. Keep them on? [Y/n]:`. The number question is skipped when a number is already saved.
- **Timeouts:** each question times out after **30 s** (`select` on stdin) and takes its default, so an unattended TTY start never hangs. The default is always "leave as is", which means OFF on a first run.
- **Persistence:** the answer is written to the settings file (`updated_by: "prompt"`) and persists until the operator changes it.
- **Non-interactive starts** (docker, systemd, CI): no prompt. They log one `pager_prompt_skipped` line with the current state and the hint `enable with: <pager on --to +1…>`.
- **`--pager` / `--no-pager`:** both entry points accept these to set the toggle without a prompt (`updated_by: "flag"`). `--pager` still needs a saved number, or it exits 2 with the same hint.
- **Starting the sender:** if alerts end up ON, the process calls `Pager().start_background()`. The lock decides which host sends, so a running watchdog-hosted pager in `llm-mailroom` keeps the role. Only an external host (the watchdog, or `pager run`) can text `watcher_down` when this process dies; the docs say so.

### Frequency state machine (pinned by tests)

Each scan and each digest tick passes through these gates, in this order:
1. **Mute / quiet hours.**
   - While muted, only criticals pass.
   - Inside the quiet window, `allow` decides: `none` blocks everything, `critical` passes only criticals, `all` passes everything.
   - A blocked alert stays *pending* and goes out when the window ends, if it is still open.
   - When quiet hours end, `catch_up_digest` sends **one** digest. Missed digests are never back-filled.
2. **Debounce.** A warning texts only after `degraded_after_scans` consecutive failing scans. A critical texts on the first failing scan.
3. **Cooldown.** An open alert re-texts only after `critical_cooldown_minutes` or `warning_cooldown_minutes`.
4. **Recovery.** An alert closes after `recover_after_scans` consecutive clear scans. If `send_recovered` is on, the closures are batched into one recovered text.
5. **Hourly cap.** `max_texts_per_hour` uses a rolling 60-min window over sent texts (per send event, not per recipient).
   - At the cap, criticals are **still sent**.
   - Digests and warnings are dropped. Dropped warnings are listed in the next digest's WATCH line as `+N held`.
6. **Healthy.** No open keys and nothing recovered → nothing is sent (R8).
7. **Send failure.** A failed send never marks an alert sent, so the next scan retries it. The digest clock still advances, so a failed send never causes a burst of late digests.

## Message house style (fixed copy — tests pin it)

`ascii` style maps `🟢→[OK] 🟠→[WARN] 🔴→[CRIT] 🟡→[REV] 💤→[idle] ✅→[OK] 🚨→[ALERT] 🧪→[RUN] 📬→[MAIL] ⏸→[PAUSED] 🔕→[MUTED] ▓→# ░→-`.

Production digest:
```
📬 MAILROOM · LIVE · 14:20 CDT
🟢 HEALTHY · up 3h12m · git a1b2c3d

LAST 10 MIN
In 14 → ✅ 11 archived · 🟡 2 review · 🔴 1 failed
p50 48s · $0.21 · first-pass 79%

QUEUES
inbox 3 · processing 1 · review 6 · failed 2

PROVIDERS
Modal fast 🟢 · extract 💤 · vision 💤
OpenRouter 🟢 $4.12 left

WATCH 10m: 0 alerts
```
The header state is `🟢 HEALTHY`, `🟠 DEGRADED` (open warning), `🔴 ATTENTION` (open critical) or `⏸ PAUSED` (ingestion paused); `🔕 MUTED until 16:20` is appended while muted. The `LAST 10 MIN` label follows the configured cadence. The WATCH line reads e.g. `WATCH 10m: 2 alerts (1 resolved) · +3 held`.

Research digest (at most 2 run blocks, then `+N more runs`):
```
🧪 MAILROOM · RESEARCH · 14:20 CDT
run pilot-real-1019 · run_pilot --real
▓▓▓▓▓▓░░░░ 58/100 · ETA ~31m
acc 0.86 · review 6% · failed 1
$1.92 · $0.033/doc · p50 41s

PROVIDERS
Modal extract 🟢 1 warm · 503s 2
OpenRouter 🟢 $4.12 left

WATCH 10m: 1 alert (resolved)
```
`acc` appears only when the run reports ground-truth accuracy. ETA appears only once `done ≥ 3`.

Alert / recovered / run events:
```
🚨 MAILROOM ALERT · 14:23 CDT
🔴 Modal extract tier DOWN
5 calls failed in 3m (HTTP 502)
🟠 3 docs failed since 14:20
last: claim_0042.pdf — extraction timeout
→ next: check data/watcher.out · GET /health
```
```
✅ MAILROOM RECOVERED · 14:41 CDT
Modal extract tier — OK after 18m
```
```
🧪 RUN STARTED · pilot-real-1019 · 100 docs
🧪 RUN FINISHED · pilot-real-1019 · 1h04m
acc 0.88 · review 5% · failed 1 · $3.30
```
Formats: durations `3h12m` / `18m` / `45s`; money `$0.21` (2 dp) and per-doc `$0.033` (3 dp); integer percentages; 10-cell progress bar rounded down. Overflow past `max_chars` drops whole lines from the lowest-priority section first, then appends `…+N more`. Priority, highest first: header → alerts/WATCH → window metrics → providers → queues.

## Review Focus

1. **Quiet hours crossing midnight.** Window `22:00-07:00`, digest due 06:50 → suppressed. At 07:00 exactly one catch-up digest goes out, and a critical at 23:30 still texts when `allow: critical`. Test `test_quiet_hours_cross_midnight_and_catch_up` (Task 2).
2. **Flapping provider.** A tier alternating 503/200 across scans must yield exactly one alert and no alert/recover churn. Test `test_flapping_key_does_not_spam` (Task 2).
3. **Hourly cap during an incident.** At `max_texts_per_hour`, criticals still send, while digests and warnings are held and counted. Test `test_cap_lets_criticals_through_and_holds_rest` (Task 2).
4. **Twilio failure mid-incident.** The alert stays pending and is retried on the next scan, with no duplicate once it succeeds. Test `test_failed_send_keeps_alert_pending` (Task 5).
5. **Watcher process dead.** The pager runs in the watchdog. The catalog may be missing or locked and the heartbeat stale; one `🔴 Watcher DOWN` text is still sent and no source failure blanks the digest. Tests `test_snapshot_survives_missing_catalog` (Task 4) and `test_watchdog_down_also_pages` (Task 5).

---

## Execution (R10)

Dispatch with **superpowers:subagent-driven-development**. The controller (this session) only dispatches, relays interfaces, and runs the final verification; it writes no feature code. Pass `model` and `effort` on every `Agent` call:

| Task | Implementer | Reviewer (one combined spec + quality review) |
|---|---|---|
| 1 Transport + config wiring (A) | `haiku` · `max` | `sonnet` · `high` |
| 2 `pager_core.py` rules + renderers (A) | `sonnet` · `high` | `sonnet` · `high` |
| 3 Provider health (A) | `haiku` · `max` | `sonnet` · `high` |
| 4 Snapshot (A) | `sonnet` · `high` | `sonnet` · `high` |
| 5 Pager host + watchdog + research hooks + docs (A) | `sonnet` · `high` | `sonnet` · `high` |
| 6 Port core + transport + health (B) | `haiku` · `max` | `haiku` · `max` (parity is mechanical: `diff` + tests) |
| 7 Ledger snapshot + host + CLI + docs (B) | `sonnet` · `high` | `sonnet` · `high` |
| Final whole-branch review, per repo | — | `sonnet` · `high` |

Rules for usage efficiency:
- Each implementer prompt contains **only**: its task section, the Global Constraints, the Configuration block, the house style (Tasks 2/6 only), and the Interfaces it consumes. Never the whole plan.
- One combined review per task, not two passes. A failed review re-dispatches the **same** implementer via `SendMessage` with the findings; it is not a fresh agent.
- Each implementer runs only its own test file plus the named neighbor suites. The full suite runs once per repo, in the final review.
- Order: `1 → 2 → 3 → 4 → 5` (llm-mailroom), then `6 → 7` (mailroom-reloaded). Task 6 may start as soon as Tasks 1–3 are approved.

---

# Part A — `llm-mailroom`

Branch `claude/mailroom-operator-sms-pings-o63gx2`. Test runner: `PYTHONPATH=src python -m pytest <file> -q`. Fixture for an isolated data dir: `temp_base_dir` (`src/tests/conftest.py`).

| File | Change |
|---|---|
| `src/config/taxonomy.yaml` | add the `pager:` block (above) after `free_quota:` |
| `src/pipeline/sms_notify.py` | **new** — transports + recipient validation (sibling of `status_notify.py`) |
| `src/pipeline/pager_core.py` | **new** — pure config resolution, rules/state machine, renderers |
| `src/llm/provider_health.py` | **new** — passive LLM outcome log (sibling of `llm/quota.py`) |
| `src/llm/retry.py` | record each attempt in `retry_chat_completion` |
| `src/storage/catalog.py` | add `window_stats()` + `recent_failures()` |
| `src/pipeline/pager.py` | **new** — snapshot, scheduler, lock/state, CLI (`python -m pipeline.pager`) |
| `src/pipeline/watchdog.py` | host `Pager.tick()`; page on DOWN |
| `src/scripts/run_pilot.py`, `src/scripts/run_experiment.py` | progress reporting + `--page` |
| `deploy/docker-compose.full.yml` (watchdog env), `.env.example`, `AGENTS.md` (Commands), `deploy/README.md` (one "Operator SMS pings" section), `CHANGELOG.md` | ops surface |
| Tests: `src/tests/test_sms_notify.py`, `test_pager_core.py`, `test_provider_health.py`, `test_pager.py` | one per module |

### Task 1: SMS transport + taxonomy block

**Files:** Modify `src/config/taxonomy.yaml`; Create `src/pipeline/sms_notify.py`; Test `src/tests/test_sms_notify.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class SendResult: ok: bool; transport: str; segments: int; detail: str = ""`
- `class Transport(Protocol): name: str; def send(self, to: str, body: str) -> SendResult`
- `TwilioTransport(sid: str, token: str, *, from_number: str | None, service_sid: str | None, client: httpx.Client | None = None)`
  - Sends `POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json` with basic auth and form fields `To`, `Body`, plus `MessagingServiceSid` if set, otherwise `From`. Timeout 10 s.
  - `ok` iff HTTP 201. `detail` is the message `sid` on success, otherwise `"HTTP <code>: <message>"`.
- `EmailGatewayTransport(smtp_cfg: dict, smtp_factory=None)` — `smtp_cfg` comes from `status_notify._smtp_config()`. Sends plain text with an empty subject.
- `ConsoleTransport(stream=sys.stdout)` — prints the body framed by `─` rules plus an `encoding · N segments` footer; always ok.
- `build_transport(name: str, env: Mapping[str, str] | None = None) -> Transport | None` — `None` (plus a `pager_transport_unconfigured` warning listing the missing env var names) when credentials are absent.
- `set_transport_override(t: Transport | None) -> None` — test seam that `build_transport` honours.
- `valid_recipients(raw: Iterable[str], transport: str) -> list[str]` — E.164 for `twilio`/`console`, email for `email_gateway`. Drops and warns as described in "Recipients".
- `mask_number(s: str) -> str` (`+16085551234` → `+1•••••1234`; for an email, mask the local part: `60•••34@vtext.com`).
- `count_segments(body: str) -> tuple[Literal["GSM-7","UCS-2"], int]` — GSM-7 160 / 153 per part; UCS-2 70 / 67, counting UTF-16 units.

- [ ] **Step 1: Write failing tests**
```python
def test_twilio_posts_form_and_reports_sid(): ...        # MockTransport: path, To, From; 201 {"sid":"SM1"} → ok, detail "SM1"
def test_twilio_uses_messaging_service_when_set(): ...   # MessagingServiceSid present, From absent
def test_twilio_error_and_timeout_are_soft(): ...         # 400 → ok False "HTTP 400…"; ReadTimeout → ok False, no raise
def test_secret_never_logged_or_returned(caplog): ...     # "tok-SECRET" absent everywhere
def test_valid_recipients_mobile_and_google_voice():
    assert valid_recipients(["+16085551234", "+14145550000"], "twilio") == ["+16085551234", "+14145550000"]
    assert valid_recipients(["608-555-1234", ""], "twilio") == []
    assert valid_recipients(["6085551234@vtext.com", "+16085551234"], "email_gateway") == ["6085551234@vtext.com"]
def test_count_segments():
    assert count_segments("a" * 160) == ("GSM-7", 1) and count_segments("a" * 161) == ("GSM-7", 2)
    assert count_segments("🟢" + "a" * 68) == ("UCS-2", 1) and count_segments("🟢" + "a" * 69) == ("UCS-2", 2)
def test_build_transport_missing_creds_returns_none(): ...
def test_override_wins(): ...
def test_taxonomy_has_pager_block():  # load_config()["pager"] has keys digest, scan, alerts, quiet_hours, rules, providers
```
- [ ] **Step 2: Run** `PYTHONPATH=src python -m pytest src/tests/test_sms_notify.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `sms_notify.py` and add the taxonomy block verbatim from "Configuration".
- [ ] **Step 4: Run** the same command and `src/tests/test_docs_truth.py` → PASS.
- [ ] **Step 5: Commit** `feat(pager): SMS transports (Twilio, email gateway, console) + pager taxonomy block`.

### Task 2: `pager_core.py` — config, frequency state machine, renderers

**Files:** Create `src/pipeline/pager_core.py`; Test `src/tests/test_pager_core.py`.

**Interfaces — Produces** (stdlib only, pure):
- Config:
  - `@dataclass(frozen=True) class PagerConfig` — one field per leaf of the `pager:` block, using nested frozen dataclasses `Digest`, `Scan`, `Alerts`, `QuietHours`, `Providers`, with `rules: dict[str, dict]`. Also `quiet: tuple[time, time] | None`, parsed from `quiet_hours.window`, and `sources: dict[str, str]` (dotted key → `env|taxonomy|default`).
  - `PagerConfig.resolve(block: Mapping | None, env: Mapping[str, str], *, environment: str, settings: Mapping | None = None) -> PagerConfig` — `settings` is the operator settings file's dict and wins for `enabled`/`recipients`. Applies the env overrides listed in "Configuration", plus the floors (`scan.seconds ≥ 15`, `max_chars ≥ 160`, minutes ≥ 0).
  - `normalize_number(raw: str) -> str | None` — strips spaces, dashes, dots and parentheses. Bare US 10-digit numbers (and 11-digit numbers starting with 1) become `+1…`. Returns `None` unless the result is E.164.
  - `PagerConfig.digest_minutes(mode: str) -> float`.
- Data model:
  - `@dataclass(frozen=True) class ProviderStatus: name: str; state: Literal["up","warm","idle","degraded","down","unknown"]; detail: str = ""`
  - `@dataclass(frozen=True) class RunProgress: run_id: str; label: str; total: int; done: int; started_at: float; updated_at: float; finished: bool; metrics: dict`
  - `@dataclass(frozen=True) class RunSnapshot: taken_at: float; mode: str; watcher_alive: bool | None; heartbeat_age_s: float | None; uptime_s: float | None; sha: str | None; queues: dict[str, int]; window: dict; stuck: int; paused: dict | None; outbox_dead: int | None; providers: list[ProviderStatus]; runs: list[RunProgress]; failures: list[dict]; openrouter: dict | None`
  - `@dataclass(frozen=True) class Alert: key: str; severity: Literal["critical","warning"]; title: str; detail: str = ""; next_step: str = ""`
- Rules and state:
  - `evaluate_rules(snap: RunSnapshot, cfg: PagerConfig, baseline: dict) -> list[Alert]` — one helper per rule in `cfg.rules`, skipping disabled rules. Keys: `watcher_down`, `provider_down:<name>`, `provider_degraded:<name>`, `openrouter_credit`, `docs_failed` (critical at `critical_at`), `review_backlog`, `stuck_docs`, `ingestion_paused`, `outbox_dead` (skipped when `snap.outbox_dead is None`), `run_stalled:<run_id>`.
  - `@dataclass class PagerState` (`open`, `pending`, `sent_log: list[float]`, `next_digest`, `last_idle_digest`, `muted_until`, `in_quiet`, `held`, `runs_seen`, `baseline`) with `to_json() -> dict` / `from_json(d) -> PagerState`.
  - `@dataclass(frozen=True) class Outbox: texts: list[tuple[str, str, list[str]]]`, where each entry is `(kind, body, alert_keys)` and `kind ∈ {"alert","recovered","digest","run"}`.
  - `plan_scan(snap, cfg, state, now) -> tuple[Outbox, PagerState]` — applies the **Frequency state machine** gates 1–6.
  - `plan_digest(snap, cfg, state, now) -> tuple[Outbox, PagerState]` — handles digest due (with `align_to_clock`), idle suppression and heartbeat, quiet/mute/cap, and catch-up.
  - `mark_sent(state, kind, alert_keys, now, ok: bool) -> PagerState` — gate 7: on failure, keys stay pending.
  - `next_digest_at(cfg, mode, now) -> float`.
- Renderers (pure, honour `style`/`max_chars`/`timezone`):
  - `render_digest(snap, cfg, state, now) -> str`
  - `render_alert(alerts, snap, cfg, now) -> str`
  - `render_recovered(items: list[tuple[Alert, float]], cfg, now) -> str`
  - `render_run_event(run, event, cfg, now) -> str`
  - `fit(sections: list[tuple[int, list[str]]], max_chars: int) -> str`

- [ ] **Step 1: Write failing tests** (house-style goldens as inline triple-quoted strings, `timezone="America/Chicago"`, fixed `now`):
```python
def test_resolve_precedence_env_over_taxonomy_over_default(): ...   # + sources map
def test_settings_file_wins_for_enabled_and_recipients():
    cfg = PagerConfig.resolve({"enabled": True}, {"MAILROOM_PAGER": "1"}, environment="live",
                              settings={"enabled": False, "recipients": ["+16085551234"]})
    assert cfg.enabled is False and cfg.recipients == ("+16085551234",)
    assert cfg.sources["enabled"] == "settings"
def test_default_is_off(): assert PagerConfig.resolve(None, {}, environment="live").enabled is False
def test_normalize_number():
    assert normalize_number("608-555-1234") == normalize_number("(608) 555 1234") == "+16085551234"
    assert normalize_number("+44 20 7946 0958") == "+442079460958"
    assert normalize_number("12345") is None
def test_resolve_floors_and_bad_values(): ...                       # scan.seconds 2 → 15; "abc" → default + warning
def test_quiet_window_parse_cross_midnight(): ...                   # "22:00-07:00"
def test_mode_auto(): ...                                           # pilot/eval → research; live → production
def test_healthy_scan_is_silent(): ...                              # Outbox([]) and no state change but baseline
def test_critical_texts_first_scan_warning_after_debounce(): ...
def test_cooldowns_per_severity(): ...                              # critical 15m, warning 60m
def test_flapping_key_does_not_spam(): ...                          # down/up/down/up → 1 alert, 0 recovered
def test_recovered_batched_after_n_clear_scans(): ...
def test_quiet_hours_cross_midnight_and_catch_up(): ...             # Review Focus 1
def test_mute_blocks_all_but_critical(): ...
def test_cap_lets_criticals_through_and_holds_rest(): ...           # Review Focus 3; digest shows "+N held"
def test_digest_aligned_to_clock_and_custom_cadence(): ...          # 10 → :10,:20; 15 → :15,:30
def test_digest_disabled_when_minutes_zero(): ...
def test_idle_suppressed_except_heartbeat(): ...                    # when_idle false, heartbeat 60
def test_each_rule_toggle_and_threshold(): ...                      # parametrized over cfg.rules
def test_golden_production_digest(): ...  test_golden_research_digest(): ...
def test_golden_alert(): ...  test_golden_recovered(): ...  test_golden_run_events(): ...
def test_ascii_style_is_gsm7(): ...
def test_fit_drops_lowest_priority_whole_lines(): ...
def test_state_json_roundtrip(): ...
```
- [ ] **Step 2: Run** `PYTHONPATH=src python -m pytest src/tests/test_pager_core.py -q` → FAIL.
- [ ] **Step 3: Implement** `pager_core.py`. Its first docstring line is `"""Operator SMS pager core (shared verbatim with mailroom-reloaded/obs/pager_core.py)."""`, and it must not import from `pipeline`, `llm`, `storage`, or any third-party package.
- [ ] **Step 4: Run** the tests → PASS. Run `wc -l src/pipeline/pager_core.py` and confirm ≤ 380.
- [ ] **Step 5: Commit** `feat(pager): pure pager core — config resolution, frequency state machine, SMS renderers`.

### Task 3: Passive provider health

**Files:** Create `src/llm/provider_health.py`; Modify `src/llm/retry.py` (`retry_chat_completion`, per attempt); Test `src/tests/test_provider_health.py`.

**Interfaces — Produces:**
- `provider_key(base_url: str | None, model: str | None) -> str`:
  - `*--mailroom-vllm-<tier>.modal.run` → `modal:<tier>`
  - gateway URL (`retry._is_gateway_url`) with model `mailroom-<tier>` → `modal:<tier>`
  - `openrouter.ai` → `openrouter`
  - anything else → `other:<host>`
- `record_llm_outcome(base_url, model, *, ok: bool, status: int | None, latency_s: float | None, now: float | None = None) -> None` — never raises; no-op when `MAILROOM_PROVIDER_HEALTH=0`, which the `conftest.py` `_set_test_env` fixture sets.
- `read_provider_health(now: float | None = None, window_s: float = 600) -> dict[str, dict]` — per key: `calls`, `errors`, `consecutive_errors`, `last_status`, `last_ok_at`, `count_503`, `p50_latency_s`.
- Storage: `<base_dir>/provider_health.json`, `{key: [[ts, ok, status, latency], …]}`, capped at 200 events per key.
  - Flushed by merge-under-`flock(<base_dir>/provider_health.lock)` + `os.replace`.
  - Flushes at most every 5 s, and immediately on an ok→error transition.

- [ ] **Step 1: Write failing tests:** `test_provider_key_mapping`, `test_window_counts_and_consecutive`, `test_old_events_age_out`, `test_corrupt_file_ignored_then_rewritten`, `test_two_writers_merge` (two buffers flushed → union), `test_retry_records_each_attempt` (fake client 503 then OK → 2 events, `count_503 == 1`). Re-enable recording inside this file with `monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "1")`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** In `retry_chat_completion`, take `base_url = str(getattr(client, "base_url", "") or "")`, time each create with `time.monotonic()`, and record in both the success path and the `except` path (`_status_code(exc)`).
- [ ] **Step 4: Run** `PYTHONPATH=src python -m pytest src/tests/test_provider_health.py src/tests/test_llm_retry.py src/tests/test_llm_quota.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(pager): passive per-provider LLM health at the retry choke point`.

### Task 4: Snapshot collection (Modal, OpenRouter, catalog, bins, research progress)

**Files:** Modify `src/storage/catalog.py`; Create `src/pipeline/pager.py` (snapshot section only); Test `src/tests/test_pager.py`.

**Interfaces — Consumes:** `pager_core.RunSnapshot`, `ProviderStatus`, `RunProgress`, `PagerConfig` (Task 2); `read_provider_health` (Task 3); `pipeline.bins` (`count_inbox_pending`, `review_dir`, `failed_dir`, `processing_dir`, `get_pause_info`, `watcher_heartbeat_age`); `pipeline.watchdog.read_heartbeat`, `_pid_alive`; `llm.quota.get_breaker()`.

**Interfaces — Produces:**
- `catalog.window_stats(since: datetime, run_id: str | None = None) -> dict` → `received` (`created_at ≥ since`), `archived`/`review`/`failed` (current stage, `updated_at ≥ since`), `cost_usd`, `p50_latency_s`, `first_pass_rate`.
- `catalog.recent_failures(since: datetime, limit: int = 3) -> list[dict]` → `{"filename", "stage", "reason"}`, newest first. `reason` is `gmail_intake.friendly_reason(...)[0]` when available, else the first 60 chars.
- `mail_outbox.count_dead() -> int` (one `SELECT COUNT(*)`, added there).
- `pager.report_run_progress(run_id: str, *, label: str, total: int, done: int, metrics: dict | None = None, finished: bool = False) -> None` — atomic write to `<base_dir>/pager_runs/<safe id>.json`, keeping the first `started_at`; never raises.
- `pager.openrouter_info(cfg, client: httpx.Client | None = None) -> dict | None` — `GET {openrouter_base_url()}/key`, timeout 5 s, cached `providers.openrouter_cache_minutes`. Returns `{"limit_remaining", "usage", "is_free_tier"}`, `{"error": "HTTP 401"}`, or `None` without a key. Check the field names against current OpenRouter docs using the `openrouter-models` skill.
- `pager.modal_warm(app: str = "mailroom-vllm", run=subprocess.run) -> dict[str, int] | None` — only when `shutil.which("modal")`. Runs `modal container list --json` (timeout 10 s) and counts per tier by function name `serve_<tier>` (`deploy/modal_vllm.py`).
- `pager.provider_statuses(cfg, health, or_info, warm) -> list[ProviderStatus]` — pure, using this table:

  | Provider | down | degraded | warm | idle | up |
  |---|---|---|---|---|---|
  | `modal:<tier>` | `consecutive_errors ≥ 3` | error rate ≥ `rules.provider_degraded.error_rate` with `calls ≥ min_calls`, or any 503 (`detail="cold-start 503s N"`) | 0 calls and `warm[tier] > 0` | 0 calls and no warm container | otherwise |
  | `openrouter` | `/key` 401/403, or `consecutive_errors ≥ 3` | quota breaker open (`"free pool rate-limited"`) | — | — | `detail="$X.XX left"` or `"no limit"` |

  Low credit is a rule (`openrouter_credit`), not a state.
- `pager.collect_snapshot(cfg, *, window_minutes: float, now: float | None = None) -> RunSnapshot` — **each source in its own try/except** → `None`/`0` plus `pager_snapshot_source_failed`. Catalog coroutines run via `asyncio.run` in a worker thread.

- [ ] **Step 1: Write failing tests:** `test_window_stats_counts_terminal_in_window`, `test_window_stats_filters_run_id`, `test_progress_roundtrip_and_stale` (unfinished, updated > `run_stalled.minutes` ago, still listed so the rule can fire), `test_provider_status_table` (parametrized, one case per cell), `test_openrouter_info_cached`, `test_modal_cli_absent_returns_none`, `test_snapshot_survives_missing_catalog` (Review Focus 5), `test_snapshot_watcher_dead`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m pytest src/tests/test_pager.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the test plus `src/tests/test_mail_outbox.py` → PASS.
- [ ] **Step 5: Commit** `feat(pager): snapshot collection — catalog window stats, Modal/OpenRouter status, run progress`.

### Task 5: Pager host — scheduler, watchdog, CLI, research hooks, docs

**Files:** Modify `src/pipeline/pager.py` (append `Pager`, settings file, start prompt, CLI), `src/pipeline/watchdog.py`, `src/api/main.py` (`__main__` only), `src/pipeline/watcher.py` (`__main__` only), `src/scripts/run_pilot.py`, `src/scripts/run_experiment.py`, `deploy/docker-compose.full.yml`, `.env.example`, `AGENTS.md`, `deploy/README.md`, `CHANGELOG.md`; Test `src/tests/test_pager.py` (append).

**Interfaces — Consumes:** Tasks 1–4.

**Interfaces — Produces:**
- `class Pager(cfg: PagerConfig | None = None, *, transport: Transport | None = None)` — `cfg` defaults to `load_pager_config()`.
  - `tick(now: float | None = None) -> list[SendResult]`:
    1. Scan if due: `collect_snapshot` → `plan_scan`.
    2. Digest if due: `plan_digest`.
    3. Run started/finished events: `runs_seen` transitions → `render_run_event`.
    4. Send every `Outbox` text to every recipient.
    5. `mark_sent`.
    6. Persist state.

    Never raises.
  - `start_background() -> threading.Thread | None` — daemon thread that calls `tick` every `min(scan.seconds, 15)` s. Returns `None` when inactive or when the lock is held.
  - `page_now(alert: Alert) -> list[SendResult]` — goes through `plan_scan` gates using a synthetic snapshot override.
- `load_pager_config(env: Mapping | None = None) -> PagerConfig` — `PagerConfig.resolve(load_config().get("pager"), env or os.environ, environment=os.environ.get("OBSERVABILITY_ENVIRONMENT", ""), settings=read_settings())`. `Pager.tick` re-resolves when the settings file's mtime changes.
- `read_settings() -> dict` and `write_settings(*, enabled: bool | None = None, recipients: list[str] | None = None, by: str) -> dict` — operate on `<base_dir>/pager_settings.json` (see Global Constraints). Writes are atomic, and an unreadable file reads as `{}`.
- `prompt_on_start(cfg: PagerConfig, *, flag: bool | None, input_fn=input, isatty=None, timeout_s: float = 30.0) -> PagerConfig` — implements "Live-production start prompt" exactly, including the copy, defaults, timeout, the non-TTY log line, and the test-text offer. Returns the re-resolved config.
- `main(argv: list[str] | None = None) -> int` — the CLI, implementing every verb of the "Toggle, number and test — CLI" table with argparse subcommands (`number` has its own `set|add|remove|list` subparsers).
- Production start hooks:
  - In `src/api/main.py` `__main__` and `src/pipeline/watcher.py` `__main__`, add `--pager/--no-pager` (argparse, `default=None`).
  - Before serving/watching, call `cfg = prompt_on_start(load_pager_config(), flag=args.pager)`.
  - If `cfg.active`, call `Pager(cfg).start_background()`.
  - No other change to these files.
- Watchdog: `main()` builds one `Pager()` and calls `tick()` every loop. The `down`/`remind` branch also calls `page_now(Alert("watcher_down","critical","Watcher DOWN — heartbeat lost", detail=<last-heartbeat row>, next_step="relaunch: python -m pipeline.watcher"))`. The email path is unchanged.
- Research: both scripts call `report_run_progress` after each sample and once with `finished=True`, with metrics `acc` (only with ground truth), `review_rate`, `failed`, `cost_usd`, `p50_latency_s`. `--page` runs `Pager(load_pager_config({**os.environ, "MAILROOM_PAGER_MODE": "research"})).start_background()`. If another host holds the lock, it logs that and continues; the external pager still reads the progress files.
- Docs, kept concise and placed only in existing files:
  - `deploy/README.md` gets one "Operator SMS pings" section covering a 5-step quick start, the Recipients table (mobile vs Google Voice), Twilio trial verification, 10DLC/toll-free, and cost (`ascii` ≈ 2.3× fewer segments).
  - `.env.example` gets a commented pager block.
  - `AGENTS.md` Commands gets 4 lines: `pager status`, `pager on --to +1…` / `pager off`, `pager test`, `pager run`.
  - `deploy/README.md` section also documents the start prompt, `--pager/--no-pager`, and `MAILROOM_PAGER_PROMPT=0`.
  - `CHANGELOG.md` gets one entry.
  - The compose watchdog service passes `MAILROOM_PAGER*`, `TWILIO_*`, `OPENROUTER_API_KEY`.

- [ ] **Step 1: Write failing tests** (fake clock, recording transport, monkeypatched `collect_snapshot`):
```python
def test_digest_every_10_minutes_aligned(): ...               # ticks every 20s for 1h → 6 digests at :10…:00
def test_custom_cadence_from_taxonomy(): ...                  # digest.production_minutes 30 → 2 digests/h
def test_healthy_scans_send_nothing_between_digests(): ...
def test_sends_to_every_recipient(): ...                      # mobile + Google Voice → 2 sends per text
def test_failed_send_keeps_alert_pending(): ...               # Review Focus 4
def test_state_survives_restart(temp_base_dir): ...           # no duplicate alert; digest clock kept
def test_mute_cli_writes_state(): ...
def test_second_pager_is_passive(temp_base_dir): ...
def test_watchdog_down_also_pages(monkeypatch): ...           # Review Focus 5; email still sent
def test_run_started_and_finished_texts_once(): ...
def test_run_pilot_reports_progress(temp_base_dir): ...       # run_pilot.main(["--mock","--max-docs","3"]) → done == total == 3, finished
def test_cli_preview_and_status_never_send(capsys): ...
def test_cli_on_off_persists_and_running_pager_picks_up(temp_base_dir): ...  # off → tick sends nothing; on → next scan sends
def test_cli_number_set_add_remove_list_masked(temp_base_dir, capsys): ...  # "608-555-1234" saved as "+16085551234"
def test_cli_bad_number_exits_2(): ...
def test_cli_on_without_number_non_tty_exits_2(): ...
def test_cli_test_to_adhoc_number_does_not_save(temp_base_dir): ...
def test_prompt_enable_with_number_and_test(temp_base_dir): ...             # inputs "y", "608 555 1234", "" → enabled, saved, 1 test send
def test_prompt_default_keeps_off_on_timeout(temp_base_dir): ...            # input_fn raises TimeoutError → still OFF
def test_prompt_skipped_non_tty_logs_hint(caplog): ...
def test_prompt_skipped_in_research_mode_and_with_flags(): ...              # --no-pager writes enabled False, no prompt
def test_prompt_already_on_asks_keep(temp_base_dir): ...
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**, then the docs/compose edits.
- [ ] **Step 4: Run** `PYTHONPATH=src python -m pytest src/tests/test_pager.py src/tests/test_pager_core.py src/tests/test_sms_notify.py src/tests/test_provider_health.py src/tests/test_docs_truth.py src/tests/test_experiment_gate_script.py -q` → PASS. Then manual: `MAILROOM_PAGER=1 MAILROOM_PAGER_TO=+15555550100 MAILROOM_PAGER_TRANSPORT=console PYTHONPATH=src python -m pipeline.pager preview` prints a digest.
- [ ] **Step 5: Commit** `feat(pager): pager service hosted by the watchdog, CLI, research --page, docs`.

---

# Part B — `mailroom-reloaded`

Branch `claude/mailroom-operator-sms-pings-o63gx2`. Test runner: `uv run pytest <path> -q`. This repo's AGENTS.md §2 applies: `mock` provider by default, no live spend.

| File | Change |
|---|---|
| `src/mailroom_reloaded/config/taxonomy.yaml` | the same `pager:` block (`outbox_dead` present but ignored) |
| `src/mailroom_reloaded/obs/pager_core.py` | **verbatim copy** of Part A `pager_core.py` (docstring header line may differ) |
| `src/mailroom_reloaded/obs/sms.py` | port of `sms_notify.py`; `EmailGatewayTransport` takes SMTP settings from `MAILROOM_SMTP_*` env and is unavailable when unset (this repo has no SMTP channel) |
| `src/mailroom_reloaded/llm/health.py` | port of `provider_health.py`; `settings.vllm_base_url` → `modal:vllm`; hooked in `llm/retry.py::_record_retry_event` (errors) and `llm/client.py::record_usage` (successes) |
| `src/mailroom_reloaded/obs/pager.py` | ledger-backed snapshot + `Pager` host |
| `src/mailroom_reloaded/cli.py` | `pager` sub-app (all CLI verbs) + `--pager/--no-pager` and start prompt on `serve`/`watch` |
| `src/mailroom_reloaded/api/app.py` | `lifespan`: opt-in background pager |
| `src/mailroom_reloaded/eval/runner.py` | `total` in `run_opened` payload; `--page` |
| `docs/CONFIGURATION.md`, `docs/OPERATIONS.md`, `.env.example`, `CHANGELOG.md` | one section each, linking to llm-mailroom's `deploy/README.md` for Twilio/Google Voice setup |
| Tests: `tests/obs/test_pager_core.py` (copied), `tests/obs/test_sms.py`, `tests/llm/test_health.py`, `tests/obs/test_pager.py` | |

### Task 6: Port core, transport, health

**Interfaces:** identical to Part A Tasks 1–3, plus `provider_key(settings.vllm_base_url, …) == "modal:vllm"`.

- [ ] **Step 1:** Copy Part A's `test_pager_core.py`, `test_sms_notify.py` and `test_provider_health.py` into the paths above, adjusting imports. Add `test_core_is_verbatim_port` in `tests/obs/test_pager_core.py`: if `../llm-mailroom/src/pipeline/pager_core.py` exists next to this checkout, it must equal this file apart from line 1; otherwise skip. Run `uv run pytest tests/obs tests/llm/test_health.py -q` → FAIL.
- [ ] **Step 2:** Copy and port the modules, add the taxonomy block, and wire the two LLM hooks.
- [ ] **Step 3:** Run `uv run pytest tests/obs tests/llm tests/test_dependency_fence.py tests/test_settings.py -q` → PASS. The dependency fence must stay green with `httpx` imported in `obs/sms.py` and `obs/pager.py`. If the fence forbids `httpx` in `obs/`, use `urllib.request` there and note it in the module docstring.
- [ ] **Step 4: Commit** `feat(obs): port operator SMS pager core, transport and provider health`.

### Task 7: Ledger snapshot, host, CLI, docs

**Interfaces — Produces:**
- `obs.pager.collect_snapshot(cfg, *, window_minutes, now=None) -> RunSnapshot`:
  - Queues come from `Bins` directories.
  - Window stats come from `Ledger.metric_rows` and document entries ≥ the window start, scoped to `live_run_id()` in production mode.
  - Research runs come from `Ledger.open_runs(kind="eval")`. `done` is the count of documents recorded for the run; `total` comes from the `run_opened` payload; `acc` comes from the eval rows.
  - Watcher liveness: if a non-blocking `flock` on `<base_dir>/watcher.lock` *succeeds*, no watcher is running, so `watcher_alive=False`.
  - `outbox_dead=None`.
  - Providers come from Part A's `provider_statuses` logic, kept in `obs/pager.py` with `modal_warm(app="mailroom-vllm")` and `openrouter_info`.
- `obs.pager.Pager` — same contract as Part A Task 5, minus the watchdog.
- `load_pager_config()` reads `load_taxonomy().raw.get("pager")`, uses `environment_name()`, and passes `settings=read_settings()`. `read_settings`/`write_settings`/`prompt_on_start` are ported from Part A Task 5 with the same contract and copy.
- CLI: a `pager` typer sub-app on the existing `cli.py` app, implementing **every** verb of the "Toggle, number and test — CLI" table with the same arguments, exit codes and output (`mailroom pager on --to …`, `mailroom pager number add …`, `mailroom pager test --to …`, …).
- Production start hooks: `serve` and `watch` gain `--pager/--no-pager` (`typer.Option(None)`). Before starting, call `prompt_on_start(load_pager_config(), flag=…)`. If the result is active, `watch` starts `Pager().start_background()`; `serve` leaves it to the `lifespan` hook below, so it doesn't start twice.
- `api/app.py` `lifespan` starts `Pager().start_background()` when the config is active and `MAILROOM_PAGER_EMBED` is not `0`, mirroring `_embed_watcher_enabled`. Docs state that only `mailroom pager run`, as its own process, can text `watcher_down` when the API dies.
- `eval/runner.py`: `run_eval` passes `total=len(docs)` into `open_run`'s payload; the CLI eval command gains `--page`.

- [ ] **Step 1: Write failing tests:** `test_snapshot_from_seeded_ledger_window`, `test_eval_run_is_progress_with_total`, `test_snapshot_survives_missing_ledger`, `test_watcher_lock_free_means_down`, `test_cli_pager_preview_prints_digest` (CLI runner, console transport), `test_lifespan_starts_pager_only_when_enabled`, the CLI and prompt tests from Part A Task 5 ported to the typer `CliRunner` (`test_cli_on_off_persists_and_running_pager_picks_up`, `test_cli_number_set_add_remove_list_masked`, `test_cli_bad_number_exits_2`, `test_cli_on_without_number_non_tty_exits_2`, `test_cli_test_to_adhoc_number_does_not_save`, `test_prompt_enable_with_number_and_test`, `test_prompt_default_keeps_off_on_timeout`, `test_prompt_skipped_non_tty_logs_hint`), `test_watch_no_pager_flag_skips_prompt`, plus Part A's `test_digest_every_10_minutes_aligned`, `test_failed_send_keeps_alert_pending` and `test_state_survives_restart`, ported. Run → FAIL.
- [ ] **Step 2: Implement**, then the docs.
- [ ] **Step 3: Run** `uv run pytest tests -q` and `make lint` → PASS.
- [ ] **Step 4: Commit** `feat(obs): ledger-backed pager host, CLI and API embedding; docs`.

---

## Out of scope

Two-way SMS replies (`ACK`/`PAUSE` via a Twilio inbound webhook), non-SMS channels (the `Transport` protocol admits them later), and per-rule routing to different numbers.
