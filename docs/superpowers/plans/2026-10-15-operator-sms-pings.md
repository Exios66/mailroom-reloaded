# Operator SMS Pings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a mailroom is running — live production or a research/experiment run — text the operator's phone a compact 10-minute digest of run metrics, events and Modal/OpenRouter provider state, plus an immediate alert (and a later "recovered" text) whenever the 60-second watcher scan finds something that needs a human. Healthy scans stay silent.

**Architecture:** A new `pipeline/pager/` package in `llm-mailroom` (Part A) does four things: it collects a `RunSnapshot` (catalog, bins, heartbeat, outbox, pause flag, provider health, research progress); it runs pure alert rules with latch/cooldown/resolve state; it renders SMS text in one fixed house style; and it sends through a pluggable transport (Twilio REST, email-to-SMS gateway, or console). The pager runs **outside** the app process, inside the existing `pipeline.watchdog` loop (or standalone with `python -m pipeline.pager`), so it can still text when the watcher dies. Provider status is collected **passively**: every LLM call already goes through `llm/retry.py`, which records outcomes to a shared file. That way the pager never wakes a scaled-to-zero Modal GPU. Part B ports the pure modules unchanged to `mailroom-reloaded` and swaps the snapshot collector for one that reads its run ledger.

**Tech Stack:** Python 3.11, `httpx` (already a dependency, used for Twilio + OpenRouter REST), stdlib `fcntl`/`json`/`zoneinfo`, structlog, pytest. No new runtime dependency.

**Spec:** the operator request of 2026-10-15 (session transcript) — summarized in "Requirements" below; this plan is the spec of record until a design doc supersedes it.

## Requirements (from the request)

| # | Requirement |
|---|---|
| R1 | Operator phone number is configurable; pings are SMS. |
| R2 | Works for **production/live** mailroom and **research/experimental** runs. |
| R3 | Periodic digest **every 10 minutes** while the mailroom is live. |
| R4 | Digest includes the **Modal job / OpenRouter** status. |
| R5 | Digest carries **full run metrics & events**, concise and easy to read; "pretty" formatting. |
| R6 | Local/dev server running the mailroom integrates and delivers the SMS. |
| R7 | Integrated watcher **scans every 60 s**; it adds ping content **only** on failures needing attention. |
| R8 | Healthy scans are **silent** (no operator overload). |
| R9 | Look-and-feel follows the Gmail intake echo/status emails (status emoji header, labeled rows, "why / next steps"). |

## Global Constraints

- Kill-switch default **off**: `MAILROOM_PAGER=1` *and* a valid `MAILROOM_PAGER_TO` are both required to send anything.
- Fail-soft: no pager code path may raise into its host (watchdog, watcher, run scripts); failures log `pager_*` structlog events and return.
- Hermetic tests: no network. Every send goes through `set_transport_override()`, and every HTTP call takes an injectable `httpx.Client`.
- Secrets (`TWILIO_AUTH_TOKEN`, `OPENROUTER_API_KEY`) are never logged; phone numbers are logged only through `mask_number()` → `+1•••••1234`.
- **Never actively probe a Modal tier by default.** A GET on a scaled-to-zero `*.modal.run` endpoint triggers a GPU cold start (spend). Only passive health is used, plus the read-only `modal container list --json` when the CLI is on PATH. Active probing is opt-in with `MAILROOM_PAGER_PROBE_MODAL=1`.
- Respect the 2026-09-04 email anti-spam directive (`status_notify.py`): **no periodic emails.** The email-gateway transport is an SMS route the operator explicitly configures, and it never goes to `MAILROOM_STATUS_EMAIL`.
- Exactly one pager sends per data dir: `data/pager.lock` (`flock`, non-blocking). A second host logs `pager_lock_held` and stays passive.
- Pager state survives restarts: `data/pager_state.json` (atomic write via temp + `os.replace`). A restart must not resend an alert that is already latched or reset the digest clock.
- SMS body ≤ `MAILROOM_PAGER_MAX_CHARS` (default **640**). Overflow is trimmed from the lowest-priority section, never mid-line, and ends with `…+N more`.
- Default display timezone is the host's local tz; override with `MAILROOM_PAGER_TZ` (IANA name).
- Every behavior change ships with its test, a `CHANGELOG.md` entry and a docs update in the same commit (repo AGENTS.md law).

## Message house style (fixed copy — tests pin it)

Emoji style (default). `ascii` style swaps each emoji for a bracket tag (`🟢`→`[OK]`, `🟠`→`[WARN]`, `🔴`→`[CRIT]`, `💤`→`[idle]`, `✅`→`[OK]`, `🚨`→`[ALERT]`, `🧪`→`[RUN]`, `📬`→`[MAIL]`, `🟡`→`[REV]`, `▓`→`#`, `░`→`-`). That keeps a message in GSM-7: 153 chars per segment instead of 67.

**Production digest** (every 10 min):
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
The header status word is one of `🟢 HEALTHY`, `🟠 DEGRADED` (any open warning), `🔴 ATTENTION` (any open critical), or `⏸ PAUSED` (ingestion paused).

**Research digest** (`mode=research`; one block per active run, at most 2, then `+N more runs`):
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
`acc` is printed only when the run reports ground-truth accuracy. Without it the line starts at `review`.

**Alert** (watcher scan; sent immediately, critical first):
```
🚨 MAILROOM ALERT · 14:23 CDT
🔴 Modal extract tier DOWN
5 calls failed in 3m (HTTP 502)
🟠 3 docs failed since 14:20
last: claim_0042.pdf — extraction timeout
→ next: check data/watcher.out · GET /health
```
**Recovered** (once, when every latched alert key in the message has cleared):
```
✅ MAILROOM RECOVERED · 14:41 CDT
Modal extract tier — OK after 18m
```
**Run start / finish** (research mode only; one text each):
```
🧪 RUN STARTED · pilot-real-1019 · 100 docs
🧪 RUN FINISHED · pilot-real-1019 · 1h04m
acc 0.88 · review 5% · failed 1 · $3.30
```

## Review Focus

1. **Overnight idle production** — 144 digests/day at ~5 UCS-2 segments each costs real money. With `MAILROOM_PAGER_DIGEST_WHEN_IDLE=0`, a window with zero documents and no open alert sends nothing, except one digest per hour titled `· IDLE`. (Default stays `1` per R3.) Test: `test_idle_window_suppressed_except_hourly` in Task 9.
2. **Flapping provider** — a Modal tier that alternates 503/200 across scans must not produce alert/recover/alert texts every minute. A key must stay clear for 2 consecutive scans before it counts as resolved, and the re-alert cooldown is 30 min. Test: `test_flapping_key_does_not_spam` in Task 8.
3. **Twilio failure mid-incident** — a 5xx/timeout on the alert send must not mark the alert as delivered. The latch reverts so the next scan retries, the same as watchdog.py's `down_alerted` revert. Test: `test_failed_send_keeps_alert_pending` in Task 9.
4. **Watcher dead** — the app process is gone, so the catalog may still be readable but the heartbeat is stale. The pager (in the watchdog process) must still send one `🔴 Watcher DOWN` SMS and must not crash on a locked/missing catalog DB. Test: `test_snapshot_survives_missing_catalog` in Task 5 and `test_watchdog_down_also_pages` in Task 9.
5. **Non-E.164 / unset phone number** — `608-555-1234` or an empty value must disable the pager with one `pager_misconfigured` warning at start and must never attempt a send. Test: `test_invalid_number_disables` in Task 1.

---

# Part A — `llm-mailroom` (branch `claude/mailroom-operator-sms-pings-o63gx2`)

All paths are relative to the repo root. Run tests with `PYTHONPATH=src python -m pytest src/tests/<file> -q`.

## File map

| File | Responsibility |
|---|---|
| `src/pipeline/pager/__init__.py` | re-exports `Pager`, `PagerConfig`, `report_run_progress` |
| `src/pipeline/pager/config.py` | env → `PagerConfig`, number validation/masking |
| `src/pipeline/pager/transport.py` | `Transport` protocol, Twilio / email-gateway / console, segment counting, test seam |
| `src/pipeline/provider_health.py` | passive per-provider LLM outcome log shared across processes |
| `src/pipeline/pager/providers.py` | `ProviderStatus` derivation: Modal (passive + CLI), OpenRouter `/key`, quota breaker |
| `src/pipeline/pager/progress.py` | research-run progress files (`data/pager_runs/<run_id>.json`) |
| `src/pipeline/pager/snapshot.py` | `RunSnapshot` collection (catalog, bins, heartbeat, outbox, pause, providers, runs) |
| `src/pipeline/pager/rules.py` | pure alert rules + latch/cooldown/resolve state machine |
| `src/pipeline/pager/format.py` | pure renderers for digest / research digest / alert / recovered / run start-finish |
| `src/pipeline/pager/service.py` | `Pager.tick(now)` scheduler, state file, lock |
| `src/pipeline/pager/__main__.py` | CLI: `run`, `preview`, `test` |
| Modify `src/llm/retry.py` | record each attempt's outcome via `provider_health.record_llm_outcome` |
| Modify `src/storage/catalog.py` | add `window_stats(since, run_id=None)` and `recent_failures(since, limit)` |
| Modify `src/pipeline/watchdog.py` | host `Pager.tick` in its loop; page on DOWN |
| Modify `src/scripts/run_pilot.py`, `src/scripts/run_experiment.py` | progress reporting + `--page` flag |
| Modify `deploy/docker-compose.full.yml`, `.env.example`, `AGENTS.md` Commands, `docs/operator-pings.md` (new), `CHANGELOG.md` | ops surface |
| Tests: `src/tests/test_pager_*.py`, `src/tests/test_provider_health.py` | one file per module |

---

### Task 1: Pager configuration

**Files:**
- Create: `src/pipeline/pager/__init__.py`, `src/pipeline/pager/config.py`
- Test: `src/tests/test_pager_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PagerConfig` with fields and env sources:
    `enabled: bool` (`MAILROOM_PAGER`, default False) · `to: str` (`MAILROOM_PAGER_TO`, E.164) ·
    `transport: Literal["twilio","email_gateway","console"]` (`MAILROOM_PAGER_TRANSPORT`, default `"twilio"`) ·
    `mode: Literal["production","research"]` (`MAILROOM_PAGER_MODE`; default `"research"` when `OBSERVABILITY_ENVIRONMENT` ∈ {`pilot`,`misc`,`mock`}, else `"production"`) ·
    `digest_minutes: float = 10.0` (`MAILROOM_PAGER_DIGEST_MINUTES`, floor 1) · `scan_seconds: float = 60.0` (`MAILROOM_PAGER_SCAN_SECONDS`, floor 15) ·
    `alert_cooldown_minutes: float = 30.0` · `max_chars: int = 640` (floor 160) · `style: Literal["emoji","ascii"] = "emoji"` ·
    `digest_when_idle: bool = True` · `tz: str` (`MAILROOM_PAGER_TZ`, default host local) · `probe_modal: bool = False` ·
    `openrouter_min_credit: float = 1.0` (`MAILROOM_PAGER_OPENROUTER_MIN_CREDIT`) ·
    `twilio_sid`, `twilio_token`, `twilio_from`, `twilio_messaging_service_sid` (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`) ·
    `gateway_address: str | None` (`MAILROOM_PAGER_GATEWAY_ADDRESS`, e.g. `6085551234@vtext.com`).
  - `PagerConfig.from_env(env: Mapping[str, str] | None = None) -> PagerConfig`
  - `PagerConfig.problems() -> list[str]` — empty means sendable.
  - `PagerConfig.active -> bool` — `enabled and not problems()`.
  - `mask_number(number: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_off():
    cfg = PagerConfig.from_env({})
    assert cfg.enabled is False and cfg.active is False
    assert (cfg.digest_minutes, cfg.scan_seconds, cfg.max_chars) == (10.0, 60.0, 640)

def test_invalid_number_disables():
    cfg = PagerConfig.from_env({"MAILROOM_PAGER": "1", "MAILROOM_PAGER_TO": "608-555-1234",
                                "MAILROOM_PAGER_TRANSPORT": "console"})
    assert cfg.active is False
    assert any("E.164" in p for p in cfg.problems())

def test_twilio_requires_credentials_and_sender():
    cfg = PagerConfig.from_env({"MAILROOM_PAGER": "1", "MAILROOM_PAGER_TO": "+16085551234"})
    assert {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER|TWILIO_MESSAGING_SERVICE_SID"} <= {
        p.split(" ")[0] for p in cfg.problems()}

def test_mode_follows_observability_environment():
    assert PagerConfig.from_env({"OBSERVABILITY_ENVIRONMENT": "pilot"}).mode == "research"
    assert PagerConfig.from_env({"OBSERVABILITY_ENVIRONMENT": "live"}).mode == "production"

def test_floors_and_mask():
    cfg = PagerConfig.from_env({"MAILROOM_PAGER_SCAN_SECONDS": "2", "MAILROOM_PAGER_MAX_CHARS": "50"})
    assert cfg.scan_seconds == 15 and cfg.max_chars == 160
    assert mask_number("+16085551234") == "+1•••••1234"
```

- [ ] **Step 2: Run** `PYTHONPATH=src python -m pytest src/tests/test_pager_config.py -q` — expected FAIL (`ModuleNotFoundError: pipeline.pager`).
- [ ] **Step 3: Implement** `config.py`. E.164 is `^\+[1-9]\d{7,14}$`. Each `problems()` string starts with the missing env var name (the test splits on the first space). Unparseable numbers fall back to the default, as `_env_float` does in `watchdog.py`.
- [ ] **Step 4: Run** the tests — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): operator SMS pager configuration`.

---

### Task 2: SMS transports

**Files:**
- Create: `src/pipeline/pager/transport.py`
- Test: `src/tests/test_pager_transport.py`

**Interfaces:**
- Consumes: `PagerConfig` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class SendResult: ok: bool; transport: str; segments: int; detail: str = ""`
  - `class Transport(Protocol): name: str; def send(self, to: str, body: str) -> SendResult`
  - `TwilioTransport(cfg, client: httpx.Client | None = None)` — `POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json`, basic auth `(sid, token)`, form `To`, `Body`, and `MessagingServiceSid` if set, else `From`; timeout 10 s; `ok` iff HTTP 201. `detail` = Twilio `sid` on success, else `"HTTP <code>: <message>"`.
  - `EmailGatewayTransport(cfg, smtp_factory=None)` — reuses the Gmail-channel SMTP settings through `status_notify._smtp_config()`; plain text, no subject line (gateways prepend it to the SMS), body only.
  - `ConsoleTransport(stream=sys.stdout)` — prints a boxed message; always `ok=True`.
  - `build_transport(cfg: PagerConfig) -> Transport`
  - `set_transport_override(t: Transport | None) -> None` — test/smoke seam honoured by `build_transport`.
  - `count_segments(body: str) -> tuple[Literal["GSM-7","UCS-2"], int]` — GSM-7: 160 single / 153 per part; UCS-2: 70 / 67, counting UTF-16 code units (emoji = 2).

- [ ] **Step 1: Write the failing tests** — use `httpx.MockTransport` for Twilio:

```python
def test_twilio_posts_form_and_reports_sid(): ...
    # assert request.url.path == "/2010-04-01/Accounts/ACx/Messages.json"
    # assert parse_qs(request.content)["To"] == ["+16085551234"]; From used when no service sid
    # handler returns 201 {"sid": "SM1"} → SendResult(ok=True, transport="twilio", segments=1, detail="SM1")

def test_twilio_error_is_soft():   # 400 {"message": "Invalid To"} → ok False, detail startswith "HTTP 400"
def test_twilio_timeout_is_soft(): # handler raises httpx.ReadTimeout → ok False, no raise
def test_secret_never_in_detail(): # token "tok-SECRET" absent from detail and captured logs

def test_count_segments():
    assert count_segments("a" * 160) == ("GSM-7", 1)
    assert count_segments("a" * 161) == ("GSM-7", 2)
    assert count_segments("🟢" + "a" * 68) == ("UCS-2", 1)
    assert count_segments("🟢" + "a" * 69) == ("UCS-2", 2)

def test_override_wins(): set_transport_override(fake); assert build_transport(cfg) is fake
```

- [ ] **Step 2: Run** `... test_pager_transport.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `transport.py`. Every exception becomes `SendResult(ok=False, …)` and is logged with `to=mask_number(to)`.
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): Twilio, email-gateway and console SMS transports`.

---

### Task 3: Passive provider health

**Files:**
- Create: `src/pipeline/provider_health.py`
- Modify: `src/llm/retry.py` (`retry_chat_completion` — after each attempt)
- Test: `src/tests/test_provider_health.py`

**Interfaces:**
- Produces:
  - `provider_key(base_url: str | None, model: str | None) -> str` — `*.modal.run` URL with `-mailroom-vllm-<tier>` → `"modal:<tier>"`; gateway URL (`retry._is_gateway_url`) with model alias `mailroom-<tier>` → `"modal:<tier>"`; `openrouter.ai` → `"openrouter"`; anything else → `"other:<host>"`.
  - `record_llm_outcome(base_url: str | None, model: str | None, *, ok: bool, status: int | None, latency_s: float | None, error: str | None = None, now: float | None = None) -> None` — never raises.
  - `@dataclass(frozen=True) class ProviderHealth: key: str; calls_10m: int; errors_10m: int; consecutive_errors: int; last_ok_at: float | None; last_error_at: float | None; last_error: str | None; last_status: int | None; p50_latency_s: float | None`
  - `read_provider_health(now: float | None = None, window_s: float = 600) -> dict[str, ProviderHealth]`
- Storage: `data/provider_health.json` = `{key: [[ts, ok, status, latency, error], …]}`, capped at 200 events per key. The in-process buffer is flushed with an atomic replace at most every 5 s, and immediately on an ok→error transition. Flushes are guarded by `fcntl.flock` on `data/provider_health.lock` so the app and research scripts can write concurrently; each writer merges its buffer into the on-disk file.
- Kill-switch: `MAILROOM_PROVIDER_HEALTH=0` makes `record_llm_outcome` a no-op (set in `conftest.py` for hermetic tests, except in this test file).

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_key_mapping():
    assert provider_key("https://ws--mailroom-vllm-extract.modal.run/v1", "x") == "modal:extract"
    assert provider_key("http://litellm:4000/v1", "mailroom-fast") == "modal:fast"
    assert provider_key("https://openrouter.ai/api/v1", "qwen/qwen3") == "openrouter"

def test_window_counts_and_consecutive(tmp_base_dir):
    for i, ok in enumerate([True, False, False, False]):
        record_llm_outcome(URL, "m", ok=ok, status=None if ok else 502, latency_s=1.0, now=1000 + i)
    h = read_provider_health(now=1010)["modal:extract"]
    assert (h.calls_10m, h.errors_10m, h.consecutive_errors, h.last_status) == (4, 3, 3, 502)

def test_old_events_age_out(tmp_base_dir): # event at t=0 absent from read at now=601
def test_corrupt_file_is_ignored(tmp_base_dir): # write "{" → read returns {} and next record rewrites
def test_retry_records_each_attempt(monkeypatch): # fake client 503 then OK → two outcomes recorded
```

- [ ] **Step 2: Run** `... test_provider_health.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `provider_health.py`, then call `record_llm_outcome` once per attempt in `retry_chat_completion`: on success with `ok=True`, in the `except` with the attempt's `_status_code(exc)` and `type(exc).__name__`. Take `base_url` from `str(getattr(client, "base_url", ""))`; take latency from `time.monotonic()` around the create call.
- [ ] **Step 4: Run** this file and the retry suite: `... test_provider_health.py src/tests/test_llm_retry.py src/tests/test_llm_quota.py -q` — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): passive per-provider LLM health shared across processes`.

---

### Task 4: Provider status (Modal + OpenRouter)

**Files:**
- Create: `src/pipeline/pager/providers.py`
- Test: `src/tests/test_pager_providers.py`

**Interfaces:**
- Consumes: `ProviderHealth`, `read_provider_health` (Task 3); `PagerConfig` (Task 1); `llm.quota.get_breaker()`.
- Produces:
  - `@dataclass(frozen=True) class ProviderStatus: name: str; state: Literal["up","warm","idle","degraded","down","unknown"]; detail: str = ""`
  - `openrouter_key_info(api_key: str | None, client: httpx.Client | None = None) -> dict | None` — `GET https://openrouter.ai/api/v1/key` (use `openrouter_utils.openrouter_base_url()`), timeout 5 s. Returns `{"limit_remaining": float | None, "usage": float, "is_free_tier": bool}`, or `{"error": "HTTP 401"}`, or `None` when no key is set. **Verify the field names against current OpenRouter docs at implementation time** (openrouter-* skill). The result is cached for 5 min in-process, so a 60 s scan costs one call per 5 min.
  - `modal_warm_containers(app: str = "mailroom-vllm", run=subprocess.run) -> dict[str, int] | None` — runs `modal container list --json` (timeout 10 s) only when `shutil.which("modal")`. It counts containers per tier by function name `serve_<tier>` (match the names in `deploy/modal_vllm.py`) and returns `None` on any failure. Read-only: it does not wake GPUs.
  - `provider_statuses(cfg: PagerConfig, health: dict[str, ProviderHealth], *, or_info: dict | None, warm: dict[str, int] | None) -> list[ProviderStatus]` — pure.
- Derivation rules (pure function; the tests pin them):

| Provider | `down` | `degraded` | `warm` | `idle` | `up` |
|---|---|---|---|---|---|
| `modal:<tier>` (only tiers named in taxonomy `tier:` or seen in health) | `consecutive_errors ≥ 3` | `errors_10m/calls_10m ≥ 0.25` with `calls_10m ≥ 4`, or any 503 in window (`detail="cold-start 503s N"`) | `calls_10m == 0` and `warm[tier] > 0` | `calls_10m == 0` and no warm container | otherwise |
| `openrouter` | `/key` → 401/403, or `consecutive_errors ≥ 3` | `limit_remaining < cfg.openrouter_min_credit`, or the quota breaker is open (`detail="free pool rate-limited"`) | — | — | otherwise (`detail="$X.XX left"` when limited, `"no limit"` otherwise) |

- [ ] **Step 1: Write the failing tests** — one per table cell (e.g. `test_modal_down_after_three_consecutive_errors`, `test_modal_idle_when_no_calls_and_no_containers`, `test_openrouter_low_credit_degraded`, `test_openrouter_401_down`, `test_modal_cli_absent_returns_none`), plus `test_openrouter_info_cached_5min` (a MockTransport counts calls: two calls within 300 s → 1 request).
- [ ] **Step 2: Run** `... test_pager_providers.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `providers.py`. When `cfg.probe_modal` is set, add a `GET <tier_url>/models` per tier (timeout 5 s) **only for tiers with `warm > 0`**, so even the opt-in probe never cold-starts a GPU.
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): Modal tier and OpenRouter status derivation`.

---

### Task 5: Run snapshot (+ catalog window queries)

**Files:**
- Modify: `src/storage/catalog.py` (append two functions)
- Create: `src/pipeline/pager/progress.py`, `src/pipeline/pager/snapshot.py`
- Test: `src/tests/test_pager_snapshot.py`

**Interfaces:**
- Consumes: Tasks 1–4; `pipeline.bins` (`count_inbox_pending`, `review_dir`, `failed_dir`, `processing_dir`, `get_pause_info`, `watcher_heartbeat_age`); `pipeline.watchdog.read_heartbeat`, `_pid_alive`; `mail_outbox` (add `count_dead() -> int` there if absent).
- Produces:
  - `catalog.window_stats(since: datetime, run_id: str | None = None) -> dict` → `{"received": int, "archived": int, "review": int, "failed": int, "cost_usd": float, "p50_latency_s": float | None, "first_pass_rate": float | None}`. A document counts toward its current terminal `stage` when `updated_at >= since`; `received` counts `created_at >= since`.
  - `catalog.recent_failures(since: datetime, limit: int = 3) -> list[dict]` → `[{"filename", "stage", "reason"}]` for `stage in ("failed","review")`, newest first. `reason` comes from `gmail_intake.friendly_reason(escalation_reason)[0]` when available, otherwise the first 60 chars.
  - `progress.report_run_progress(run_id: str, *, label: str, total: int, done: int, metrics: dict | None = None, finished: bool = False) -> None` — atomic write of `data/pager_runs/<safe run_id>.json` with `started_at` preserved from the first write; never raises.
  - `progress.active_runs(now: float, stale_s: float = 1800) -> list[RunProgress]` — `@dataclass(frozen=True) RunProgress: run_id, label, total, done, started_at, updated_at, finished, metrics: dict`.
  - `@dataclass(frozen=True) class RunSnapshot: taken_at: float; mode: str; watcher: WatcherInfo; queues: dict[str, int]; window: dict; window_minutes: float; stuck: int; paused: dict | None; outbox_dead: int; providers: list[ProviderStatus]; runs: list[RunProgress]; failures: list[dict]; boss: dict | None`
  - `@dataclass(frozen=True) class WatcherInfo: alive: bool | None; heartbeat_age_s: float | None; pid: int | None; uptime_s: float | None; sha: str | None`
  - `collect_snapshot(cfg: PagerConfig, *, window_minutes: float, now: float | None = None, or_client: httpx.Client | None = None) -> RunSnapshot` — sync wrapper. Catalog coroutines run via `asyncio.run` in a worker thread. **Each source sits in its own try/except** and degrades to `None`/0 plus a `pager_snapshot_source_failed` log, so one broken source never blanks the digest.

- [ ] **Step 1: Write the failing tests**

```python
def test_window_stats_counts_terminal_in_window(seeded_catalog): ...  # 3 archived, 1 review, 1 failed in window; 2 outside
def test_window_stats_filters_run_id(seeded_catalog): ...
def test_progress_roundtrip_and_stale(tmp_base_dir): ...            # stale run (updated 31m ago) excluded
def test_snapshot_survives_missing_catalog(tmp_base_dir, monkeypatch):
    monkeypatch.setattr(catalog, "window_stats", boom)
    snap = collect_snapshot(cfg, window_minutes=10, now=NOW)
    assert snap.window == {} and snap.queues["inbox"] >= 0          # other sections still filled
def test_snapshot_watcher_dead(tmp_base_dir): ...                  # heartbeat 300s old + pid gone → alive False
```

- [ ] **Step 2: Run** `... test_pager_snapshot.py -q` — expected FAIL.
- [ ] **Step 3: Implement** the two catalog functions, `progress.py`, `snapshot.py`. `boss` carries the most recent ops-monitor finding. Make `OpsMonitor._sweep` write its `findings` dict to `data/ops_monitor_last.json` (one-line atomic write), and have the snapshot read it when it is fresher than 2× the sweep interval.
- [ ] **Step 4: Run** this file and `src/tests/test_observability.py` — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): run snapshot collector, catalog window stats, research progress files`.

---

### Task 6: Research run integration

**Files:**
- Modify: `src/scripts/run_pilot.py`, `src/scripts/run_experiment.py`
- Test: `src/tests/test_pager_research_hooks.py`

**Interfaces:**
- Consumes: `report_run_progress` (Task 5); `Pager` (Task 9) only behind the `--page` flag (import lazily so this task can land first with the flag raising `SystemExit("pager not available")` until Task 9 merges — or simply order Task 6 after Task 9 when executing).
- Produces: both scripts call `report_run_progress(run_id, label=<script + flags>, total=N, done=i, metrics=…)` after each sample, plus a final call with `finished=True`. `metrics` keys: `acc` (class accuracy so far, when ground truth exists), `review_rate`, `failed`, `cost_usd`, `p50_latency_s`. `--page` starts `Pager.start_background(cfg_overrides={"mode": "research"})` for the life of the run, unless another host holds `data/pager.lock`. In that case the external pager picks up the progress file anyway.

- [ ] **Step 1: Write the failing test** — run `run_pilot.main(["--mock", "--max-docs", "3"])` (use the existing mock fixtures) with `tmp_base_dir`; assert one file under `pager_runs/` with `done == total == 3`, `finished is True`, and `"review_rate" in metrics`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** the hooks (a few lines per loop) and the flag.
- [ ] **Step 4: Run** `... test_pager_research_hooks.py src/tests/test_experiment_gate_script.py -q` — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): research runs report progress; --page flag`.

---

### Task 7: Message renderers

**Files:**
- Create: `src/pipeline/pager/format.py`
- Test: `src/tests/test_pager_format.py` (+ golden files `src/tests/fixtures/pager/*.txt`)

**Interfaces:**
- Consumes: `RunSnapshot`, `RunProgress`, `ProviderStatus` (Tasks 4–5); `Alert` (Task 8 — define `Alert` in `rules.py` first as a bare dataclass if Task 7 executes before Task 8).
- Produces (all pure, all `-> str`, all honour `style`, `max_chars`, `tz`):
  - `render_digest(snap: RunSnapshot, *, open_alerts: list[Alert], alerts_in_window: int, resolved_in_window: int, cfg: PagerConfig) -> str` — production or research layout, chosen by `snap.mode`.
  - `render_alert(alerts: list[Alert], snap: RunSnapshot, cfg: PagerConfig) -> str`
  - `render_recovered(resolved: list[tuple[Alert, float]], cfg: PagerConfig) -> str` — `(alert, open_seconds)`.
  - `render_run_event(run: RunProgress, event: Literal["started","finished"], cfg: PagerConfig) -> str`
  - `fit(sections: list[tuple[int, list[str]]], max_chars: int) -> str` — `(priority, lines)`; drops whole lines from the lowest-priority section until the body fits, then appends `…+N more`.
- Copy is fixed by "Message house style" above. Section priority (highest kept first): header → alerts/WATCH → window metrics → providers → queues.
- Durations: `3h12m`, `18m`, `45s`. Money: `$0.21` (2 dp), `$0.033/doc` (3 dp). Percentages: integer.

- [ ] **Step 1: Write the failing tests** — golden-file comparisons for the five house-style samples (build snapshots with fixed `now` and `tz="America/Chicago"`), plus:

```python
def test_ascii_style_is_gsm7(): assert count_segments(render_digest(..., cfg=ascii_cfg))[0] == "GSM-7"
def test_fit_drops_lowest_priority_whole_lines(): ...
def test_digest_header_states(): # open critical → "🔴 ATTENTION"; paused → "⏸ PAUSED"; warning → "🟠 DEGRADED"
def test_research_without_acc_omits_acc(): ...
def test_every_render_within_max_chars(hypothesis or 50 random snapshots): assert len(out) <= cfg.max_chars
```

- [ ] **Step 2: Run** `... test_pager_format.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `format.py`. The progress bar is 10 cells (`▓`/`░`), rounded down. ETA = `(total-done) × elapsed/done`, shown only once `done ≥ 3`.
- [ ] **Step 4: Run** — expected PASS. Paste one rendered digest into the PR description so a reviewer sees the look.
- [ ] **Step 5: Commit** `feat(pager): SMS renderers in the mailroom status house style`.

---

### Task 8: Watcher alert rules

**Files:**
- Create: `src/pipeline/pager/rules.py`
- Test: `src/tests/test_pager_rules.py`

**Interfaces:**
- Consumes: `RunSnapshot` (Task 5).
- Produces:
  - `@dataclass(frozen=True) class Alert: key: str; severity: Literal["critical","warning"]; title: str; detail: str = ""; next_step: str = ""`
  - `RULES: tuple[Callable[[RunSnapshot, dict], list[Alert]], ...]` — each rule reads the snapshot plus the previous `baseline` dict (for deltas).
  - `@dataclass class AlertState: open: dict[str, dict]  # key → {"since", "last_sent", "clear_streak", "alert"}; baseline: dict` with `to_json()/from_json()`.
  - `evaluate(snap: RunSnapshot, state: AlertState, now: float, *, cooldown_s: float) -> tuple[list[Alert], list[tuple[Alert, float]], AlertState]` — returns `(to_send, recovered, new_state)`. Pure.
- Rules (key → severity → condition → `next_step` copy):

| key | sev | fires when |
|---|---|---|
| `watcher_down` | critical | `watcher.alive is False`, or heartbeat age > `MAILROOM_WATCHDOG_STALE_SECONDS` (30 s) on 2 consecutive scans |
| `provider_down:<name>` | critical | any `ProviderStatus.state == "down"` |
| `provider_degraded:<name>` | warning | `state == "degraded"` for 2 consecutive scans |
| `docs_failed` | warning (critical if ≥ 5) | `queues["failed"]` grew since the baseline (`detail` = count + newest failure line) |
| `review_backlog` | warning | `queues["review"] ≥ MAILROOM_PAGER_REVIEW_MAX` (default 20) |
| `stuck_docs` | warning | `snap.stuck > 0` |
| `ingestion_paused` | critical | `snap.paused` is set (actor + reason in `detail`) |
| `outbox_dead` | warning | `outbox_dead` grew since the baseline |
| `boss_critical` | critical | `snap.boss.severity == "critical"` |
| `run_stalled:<run_id>` | warning | research run unfinished and `updated_at` older than 15 min |

- State machine: a new key → send now. A key still open → resend only when `now - last_sent ≥ cooldown_s` (default 1800 s). A key absent for **2 consecutive scans** → closed and returned in `recovered`. Healthy (no keys, nothing recovered) → `([], [], state)`. **This is the silence guarantee (R8).**

- [ ] **Step 1: Write the failing tests** — one per table row; plus:

```python
def test_healthy_scan_is_silent(): to_send, rec, _ = evaluate(healthy, AlertState.empty(), NOW, cooldown_s=1800); assert to_send == rec == []
def test_open_alert_not_resent_within_cooldown(): ...
def test_open_alert_resent_after_cooldown(): ...
def test_flapping_key_does_not_spam():  # down, up, down, up over 4 scans → exactly 1 alert, 0 recovered
def test_recovered_after_two_clear_scans(): ...
def test_state_json_roundtrip(): ...
```

- [ ] **Step 2: Run** `... test_pager_rules.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `rules.py`.
- [ ] **Step 4: Run** — expected PASS.
- [ ] **Step 5: Commit** `feat(pager): watcher alert rules with latch, cooldown and recovery`.

---

### Task 9: Pager service, watchdog hosting, CLI

**Files:**
- Create: `src/pipeline/pager/service.py`, `src/pipeline/pager/__main__.py`
- Modify: `src/pipeline/watchdog.py` (`main()` loop + `send_status_email` call sites)
- Test: `src/tests/test_pager_service.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class Pager(cfg: PagerConfig, *, transport: Transport | None = None, clock=time.time)`
    - `tick(now: float | None = None) -> list[SendResult]` — if `now ≥ next_scan`: collect a snapshot (window = digest window), `evaluate`, send the alert/recovered texts. If `now ≥ next_digest`: send the digest (window = `digest_minutes`). Run start/finish events come from `progress.active_runs` transitions recorded in state. Persists state. Never raises.
    - `start_background(cfg_overrides: dict | None = None) -> threading.Thread | None` — daemon loop calling `tick` every `min(scan_seconds, 15)` s; `None` if the lock is held or config is inactive.
    - `page_now(alert: Alert) -> SendResult` — out-of-band send used by the watchdog DOWN path (respects latch/cooldown via state).
  - Persistent state `data/pager_state.json`: `{"next_scan", "next_digest", "last_idle_digest", "alerts": AlertState.to_json(), "runs_seen": {run_id: "started"|"finished"}, "window_counts": {...}}`.
  - Lock: `data/pager.lock` via `fcntl.flock(LOCK_EX|LOCK_NB)` held for the process lifetime (the same pattern as `watcher.acquire_watcher_lock`).
  - CLI (`python -m pipeline.pager`):
    - `run` — foreground loop (for dev servers and research boxes without the watchdog).
    - `preview [--mode production|research] [--alert]` — collect a real snapshot and print the SMS through `ConsoleTransport`, with segment count and encoding; never sends.
    - `test` — send `✅ MAILROOM PAGER TEST · <host> · <time>` to the configured number and print the `SendResult`; exit 1 on failure.
- Send semantics:
  - An alert is marked `last_sent` **only when** `SendResult.ok` (Review Focus 3).
  - The digest clock advances even on send failure (no burst of back-dated digests), but the failure is logged as `pager_digest_failed`.
  - `digest_when_idle=False` and the window has `received == 0` and no open alerts → skip, except when `now - last_idle_digest ≥ 3600` (then send it with the `· IDLE` title suffix) (Review Focus 1).
- Watchdog hosting: in `watchdog.main()`, build `Pager(PagerConfig.from_env())` once; call `pager.tick()` every loop iteration (the watchdog polls every 20 s; the pager's own clock gates scans/digests). On the `down`/`remind` actions, also call `pager.page_now(Alert("watcher_down", "critical", "Watcher DOWN — heartbeat lost", detail=<last heartbeat row>, next_step="relaunch: python -m pipeline.watcher"))`. The email path is unchanged.

- [ ] **Step 1: Write the failing tests** with a fake clock + recording transport + monkeypatched `collect_snapshot`:

```python
def test_digest_every_10_minutes():          # ticks at t=0..3600 step 20 → 6 digests (t=600,…,3600); first digest at t=600
def test_healthy_scans_send_nothing_between_digests(): # 60 scans, healthy → only digest sends
def test_alert_sent_on_first_failing_scan_then_silent_until_cooldown(): ...
def test_failed_send_keeps_alert_pending():  # transport.ok False on first try → re-sent next scan
def test_idle_window_suppressed_except_hourly(): ...
def test_state_survives_restart(tmp_base_dir): # new Pager after 5 min → no duplicate alert, digest at original t=600
def test_second_pager_is_passive(tmp_base_dir): # lock held → start_background returns None
def test_watchdog_down_also_pages(monkeypatch): # evaluate→"down" ⇒ page_now called once; email still sent
def test_run_started_and_finished_texts_once(): ...
```

- [ ] **Step 2: Run** `... test_pager_service.py -q` — expected FAIL.
- [ ] **Step 3: Implement** `service.py`, `__main__.py` and the watchdog wiring.
- [ ] **Step 4: Run** the whole pager suite and the watchdog/outbox suites: `PYTHONPATH=src python -m pytest src/tests -q -k "pager or provider_health or watchdog or mail_outbox or status"` — expected PASS. Then manual: `MAILROOM_PAGER=1 MAILROOM_PAGER_TO=+15555550100 MAILROOM_PAGER_TRANSPORT=console PYTHONPATH=src python -m pipeline.pager preview` prints a digest.
- [ ] **Step 5: Commit** `feat(pager): pager service hosted by the watchdog; run/preview/test CLI`.

---

### Task 10: Ops surface, deployment and docs

**Files:**
- Modify: `deploy/docker-compose.full.yml` (`watchdog` service env: pass `MAILROOM_PAGER*`, `TWILIO_*`, `OPENROUTER_API_KEY`), `deploy/README.md`, `AGENTS.md` (Commands block), `CHANGELOG.md`
- Create: `docs/operator-pings.md`
- Modify: `.env.example` (add a commented pager block)
- Modify: `src/api/main.py` — add `GET /ops/pager/preview` (token-protected like the existing ops routes) returning `{"text": str, "encoding": str, "segments": int}` for a dry-run digest
- Test: `src/tests/test_pager_api.py`, plus extend `src/tests/test_docs_truth.py` if it enumerates env vars/commands

**Interfaces:**
- Consumes: `Pager`, `collect_snapshot`, `render_digest`, `count_segments`.

- [ ] **Step 1: Write the failing test** — `GET /ops/pager/preview` with an auth token → 200 with `text` starting `"📬 MAILROOM"`; without a token → 401; the response never contains the Twilio token.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** the route, compose env, and docs. `docs/operator-pings.md` must cover:
  - **Quick start (local/dev server):** set env → `python -m pipeline.pager test` → `python -m pipeline.watchdog` (or `python -m pipeline.pager run`) next to `python -m api.main`.
  - **Twilio setup:** account SID/token, a sender number or Messaging Service, and the US carrier rules (A2P 10DLC registration for a local long code, or toll-free verification; unregistered US traffic is filtered).
  - **Free fallback:** the carrier email-to-SMS gateway via `MAILROOM_PAGER_TRANSPORT=email_gateway` (reuses the Gmail channel SMTP; best-effort delivery, no delivery receipts).
  - **Every env knob** with its default.
  - **Cost note:** ~5 UCS-2 segments per emoji digest × 144/day. Use `MAILROOM_PAGER_STYLE=ascii` or `MAILROOM_PAGER_DIGEST_WHEN_IDLE=0` to cut it.
  - **Sample messages:** the house-style samples.
  - **Research runs:** `run_pilot.py --real --page`.
  - **Troubleshooting:** `preview`, `pager_*` log events, `data/pager_state.json`.
- [ ] **Step 4: Run** `PYTHONPATH=src python -m pytest src/tests/test_pager_api.py src/tests/test_docs_truth.py -q` and then the full suite `PYTHONPATH=src python -m pytest src/tests -q` — expected PASS.
- [ ] **Step 5: Commit** `docs(pager): operator SMS pings guide, compose wiring, preview endpoint`.

---

# Part B — `mailroom-reloaded` (branch `claude/mailroom-operator-sms-pings-o63gx2`)

Same behavior, same copy, same env names. The pure modules are ported; only the data sources differ. Run tests with `uv run pytest tests/<path> -q`. Per that repo's AGENTS.md §2, `mock` provider by default; no live spend in tests.

## File map

| File | Responsibility |
|---|---|
| `src/mailroom_reloaded/ops/__init__.py`, `ops/pager/{config,transport,format,rules}.py` | **verbatim ports** of Part A Tasks 1, 2, 7, 8 (imports adjusted to `mailroom_reloaded.*`; settings read via `get_settings().base_dir`) |
| `src/mailroom_reloaded/ops/provider_health.py` | port of Task 3; hooked in `llm/retry.py::_record_retry_event` (errors) and `llm/client.py::record_usage` (successes) |
| `src/mailroom_reloaded/ops/pager/snapshot.py` | collector over **`Ledger`** (`open_runs`, `metric_rows`, `entries`), `Bins`, the watcher lock/heartbeat, `jev_status` |
| `src/mailroom_reloaded/ops/pager/service.py` | port of Task 9 (no watchdog in this repo — see Task 13) |
| `tests/ops/test_pager_parity.py` | renders Part A's golden fixtures from equivalent snapshots → byte-identical text |

### Task 11: Port pure modules + parity fixture

**Files:** create the `ops/pager/{config,transport,format,rules}.py` ports and `ops/provider_health.py`; copy Part A's `src/tests/fixtures/pager/*.txt` to `tests/fixtures/pager/`; tests `tests/ops/test_pager_{config,transport,format,rules}.py` (port Part A's tests) and `tests/ops/test_pager_parity.py`.

**Interfaces:** identical to Part A Tasks 1, 2, 3, 7, 8. `provider_key` additionally maps `settings.vllm_base_url` (single-tier Modal app `mailroom-vllm`) → `"modal:vllm"`.

- [ ] **Step 1:** Port the tests first; run `uv run pytest tests/ops -q` — expected FAIL.
- [ ] **Step 2:** Port the modules; run again — expected PASS, including parity.
- [ ] **Step 3:** Hook `record_llm_outcome` into `_record_retry_event` (error attempts) and `record_usage` (success). Run `uv run pytest tests/llm tests/ops -q` — expected PASS. Run `uv run pytest tests/test_dependency_fence.py -q` — expected PASS: `httpx` must already be an allowed dependency, otherwise use `urllib.request` for Twilio/OpenRouter in this repo and note the deviation in the module docstring.
- [ ] **Step 4: Commit** `feat(ops): port operator SMS pager core (config, transport, format, rules)`.

### Task 12: Ledger-backed snapshot

**Files:** create `src/mailroom_reloaded/ops/pager/snapshot.py`; test `tests/ops/test_pager_snapshot.py`.

**Interfaces:**
- Produces `collect_snapshot(cfg, *, window_minutes, now=None) -> RunSnapshot` (the same dataclass as Part A, defined in `ops/pager/rules.py` or a shared `ops/pager/model.py`).
  - Queues come from the `Bins` directories.
  - Window stats come from `Ledger.metric_rows` / document entries with timestamps ≥ window start, scoped to `live_run_id()` in production mode.
  - Research runs are `Ledger.open_runs(kind="eval")`. Progress is `done` = documents recorded for the run and `total` from the `run_opened` payload. **Add `total` to `open_run`'s payload in `eval/runner.py::run_eval`.** Accuracy comes from the eval rows already inserted by `_insert`.
  - Watcher liveness comes from `watcher.lock` being held (try a non-blocking `flock`: success means no watcher) plus the API `/ready` probe when `MAILROOM_PAGER_API_URL` is set.
  - The Modal tier comes from passive health plus `modal container list --json` (`mailroom-vllm`).
- Each source is fail-soft, as in Part A Task 5.

- [ ] **Step 1:** Tests: window counts over a seeded ledger (`tests/fakes` / `helpers.py` ledger fixtures), an eval run appearing as `RunProgress` with `total`, and `test_snapshot_survives_missing_ledger`. Run — FAIL.
- [ ] **Step 2:** Implement; run `uv run pytest tests/ops tests/pipeline/test_run_ledger_units.py tests/eval -q` — PASS.
- [ ] **Step 3: Commit** `feat(ops): ledger-backed pager snapshot for live and eval runs`.

### Task 13: Hosting, CLI, docs

**Files:** create `ops/pager/service.py`; modify `cli.py` (add a `pager` command group on the existing CLI app: `run`, `preview`, `test`), `.env.example` (pager block), `api/app.py` `lifespan` (start `Pager.start_background()` when `MAILROOM_PAGER=1` and `MAILROOM_PAGER_EMBED` is not `0`, mirroring `_embed_watcher_enabled`), `eval/runner.py` (`--page` → `start_background(mode="research")`), `docs/OPERATIONS.md` + `docs/CONFIGURATION.md` + `docs/RUNBOOK.md` (pager section), `CHANGELOG.md`; tests `tests/ops/test_pager_service.py`, extend `tests/test_cli.py`.

Hosting caveat to document: the embedded pager dies with the API process, so `mailroom pager run` as a separate process is the recommended production setup (it is the only way to get the `watcher_down` text). The embedded mode is for dev/research convenience.

- [ ] **Step 1:** Port Part A's Task 9 service tests (minus the watchdog test); add `test_cli_pager_preview_prints_digest` (Typer `CliRunner`, console transport) and `test_lifespan_starts_pager_only_when_enabled`. Run — FAIL.
- [ ] **Step 2:** Implement; run `uv run pytest tests -q` and `make lint` (or the repo's `scripts/dev_test.sh`) — PASS.
- [ ] **Step 3: Commit** `feat(ops): pager service, CLI and API embedding; docs`.

---

## Execution order & dependencies

`1 → 2 → 3 → 4 → 5 → 8 → 7 → 9 → 6 → 10` for Part A. (Rules before format, so `Alert` exists; research hooks after the service, so `--page` works.) Then `11 → 12 → 13` for Part B. Part B can start once Part A's Tasks 1–3, 7 and 8 have merged, because it copies them.

## Out of scope (follow-ups, not built here)

- Two-way SMS commands (`PAUSE`, `ACK`, `STATUS` replies via a Twilio inbound webhook). This is a natural next step and needs a public webhook URL plus signature validation.
- Quiet hours / per-severity routing to multiple numbers.
- Push channels other than SMS (ntfy, Slack, Pushover). The `Transport` protocol already admits them.
