# Gmail intake

`mailroom-reloaded` normally ingests documents dropped into `inbox/`. The
optional **Gmail intake** adds a second door: it lists unread mail with
attachments, decodes the supported ones, and writes each into `inbox/` so the
existing watcher and pipeline process them unchanged. It also accepts Gmail
Pub/Sub push notifications at `POST /v1/intake/gmail`.

This flow is a scope amendment beyond the original design (which listed Gmail as
out of scope) and is off by default. The Google client stack is an **optional
extra**, so nothing in the core install depends on it:

```sh
uv sync --extra gmail          # or: pip install 'mailroom-reloaded[gmail]'
```

## Supported attachments

Extension allow-list (configurable): `.pdf`, `.docx`, `.txt`, `.png`, `.jpg`,
`.jpeg`. Each attachment is capped at 25 MB by default. Note the pipeline's
text layer currently ingests `.txt` and `.pdf` (plus OCR for scanned PDFs); other
extensions are accepted into `inbox/` but will fail ingest as unsupported.

## Google Cloud setup

1. **Project + API.** Create (or pick) a Google Cloud project, then enable the
   **Gmail API** and the **Cloud Pub/Sub API** in *APIs & Services*.
2. **OAuth consent screen.** Configure it as *External* (or *Internal* for a
   Workspace org). Add the scope
   `https://www.googleapis.com/auth/gmail.readonly`. For External in *Testing*,
   add your mailbox as a test user.
3. **OAuth client.** Create an *OAuth client ID* of type **Desktop app** and
   download the JSON. Point the intake at it with
   `MAILROOM_GMAIL_CREDENTIALS=/path/to/client_secret.json`.
4. **Pub/Sub topic.** Create a topic, e.g. `projects/<project>/topics/gmail-push`.
   Grant the Gmail push service account `roles/pubsub.publisher` on it:
   `gmail-api-push@system.gserviceaccount.com`.
5. **Subscription.** Deploy an authenticated relay and create a **push**
   subscription targeting its HTTPS endpoint. Configure Pub/Sub authentication
   with a service account and the relay's expected audience. The relay must
   validate Google's OIDC token (signature, issuer, audience and service-account
   identity), then forward the unchanged JSON envelope to
   `POST https://your-host/v1/intake/gmail`, replacing the Authorization header
   with `Bearer <MAILROOM_API_TOKEN>`. Set that shared secret on the API and relay,
   and have the relay acknowledge Pub/Sub only after the API returns HTTP 204.
   Pub/Sub cannot send an arbitrary fixed bearer header; the API's `require_token`
   guard accepts a static secret and does not validate Google OIDC tokens.
   This relay is deployment infrastructure you must provide; use local polling
   below if it is unavailable.
6. **Register the mailbox watch.** Call `users.watch` once (and at least every
   7 days) with the topic:

   ```sh
   curl -sS -X POST \
     "https://gmail.googleapis.com/gmail/v1/users/me/watch" \
     -H "Authorization: Bearer $ACCESS_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"topicName":"projects/<project>/topics/gmail-push","labelIds":["INBOX"]}'
   ```

   Docs: <https://developers.google.com/gmail/api/guides/push> and
   <https://developers.google.com/gmail/api/reference/rest/v1/users/watch>.

## Local demo

```sh
# 1. Authorize once at a terminal (opens a browser; caches a token).
mailroom gmail auth

# 2. Pull new attachments into inbox/ and print their doc_ids.
mailroom gmail poll

# 3. Optional: drain the inbox in the same command.
mailroom gmail poll --process

# 4. Or poll on an interval until Ctrl-C.
mailroom gmail watch --interval 30 --process
```

`mailroom gmail auth`, `poll`, and `watch` fail with a message naming the
`gmail` extra when it is not installed, and `auth` raises a clear error instead
of hanging when there is no interactive terminal (CI-safe).

### On-demand HTTP upload

With the API running (`mailroom serve`):

```sh
curl -sS -X POST http://127.0.0.1:8000/v1/intake/gmail/poll
# {"doc_ids": ["<16-hex>", ...], "count": 1}
```

The created `doc_id` is the content hash of the attachment and matches the id
the pipeline will use, so `GET /v1/documents/{doc_id}` works after the watcher
drains the inbox.

## Configuration

All settings are read from the environment (optional; defaults derive from
`MAILROOM_BASE_DIR`):

| Variable | Meaning | Default |
| --- | --- | --- |
| `MAILROOM_GMAIL_CREDENTIALS` | OAuth client secrets JSON | `<base_dir>/gmail_credentials.json` |
| `MAILROOM_GMAIL_TOKEN` | Cached token JSON | `<base_dir>/gmail_token.json` |
| `MAILROOM_GMAIL_QUERY` | Gmail search query | `is:unread has:attachment` |
| `MAILROOM_GMAIL_EXTENSIONS` | Comma-separated allow-list | `.pdf,.docx,.txt,.png,.jpg,.jpeg` |
| `MAILROOM_GMAIL_MAX_ATTACHMENT_BYTES` | Per-attachment cap | `26214400` (25 MB) |
| `MAILROOM_GMAIL_STATE` | Processed-message state file | `<base_dir>/gmail_state.json` |

## Idempotency and state

Processed Gmail message ids are persisted in a small JSON file at
`<base_dir>/gmail_state.json` (`{"kind": "mailroom.gmail.state/v1",
"processed": [...]}`), written with an atomic replace. Polling holds a
process-wide thread lock and an exclusive file lock beside the state file across
fetching, ingestion and state updates. A message is marked processed after its
attachments are handled, so repeated polls and duplicate
Pub/Sub deliveries never re-ingest the same mail. Deleting the file re-imports
the messages the query still matches.

## Limitations

- The Pub/Sub push route acknowledges immediately and ingests in a FastAPI
  background task; if the process dies mid-fetch the message may be retried by
  Pub/Sub and is deduplicated by the state file.
- Only the `gmail.readonly` scope is requested; the intake never marks mail as
  read, applies labels, or modifies the mailbox.
- Push notifications require a publicly reachable endpoint (or a tunnel) plus
  Cloud Pub/Sub setup; `mailroom gmail watch` (local polling) is the demo path.
- Coordination requires all callers to share the same state file on a filesystem
  supporting `flock`. Replicas with separate state files can ingest duplicate mail.
