# Jarvis: Bravo Vision Lead Generator 2.0

An AI operations system for a wholesale real estate business. Jarvis handles lead intake, seller
conversations, deal math, follow-ups and buyer disposition, and **a human approves everything that leaves the
building**: messages, offers, contracts, payments, campaigns and deletions.

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2, PostgreSQL (Alembic migrations)
- **AI:** Anthropic Claude with tool calling (default model `claude-sonnet-5-5`, set `BV_CLAUDE_MODEL`)
- **Channels:** Twilio (SMS), Gmail API (email), Google Calendar API
- **CRM:** adapter layer (`local` built in, `hubspot` included); add another by implementing 5 methods
- **Dashboard:** React + Vite (`dashboard/`)

## How it works

```
 CSV / web form / Gmail / CRM ─► Lead Intake Agent ─► normalize ► dedupe ► score (0-100) ► next action
                                                                                 │
 Twilio inbound SMS ─► STOP handling ─► conversation ─► Seller Conversation Agent ─► DRAFT ─┐
 Follow-up Agent (tasks, escalation, campaigns) ───────────────────────────────► DRAFT ─────┤
 Deal Analysis Agent (MAO) ─► offer ───────────────────────────────────────────► REQUEST ───┤
 Buyer Disposition Agent (match + summary) ────────────────────────────────────► DRAFT ─────┤
                                                                                            ▼
                                       Approval queue (dashboard)  ◄─ human approves the exact payload
                                                                                            │
                          send gate: opt-out ► contact hours ► daily caps ► retry ► Twilio/Gmail
```

**Agents cannot send, sign, pay or delete.** They have no such tools. They can only call tools that create a
draft or an approval request. Execution lives in `bravo/approvals.py`, which runs only after a human with the
right role decides, only once, and only if the payload still matches the hash that was reviewed.

### The five agents

| Agent | Does | LLM used for | Deterministic code does |
|---|---|---|---|
| Lead Intake (`agents/intake.py`) | CSV, web form, Gmail, CRM import; phone/address normalization; duplicate detection; motivation score; next action + priority | Extracting fields from free-form emails | Everything else. Extracted phone/email/address must literally appear in the email or they are dropped. |
| Seller Conversation (`agents/seller.py`) | Drafts SMS/email asking about timeline, condition, mortgage, occupancy, price, reason; records answers; summarizes; updates CRM | Understanding replies and writing drafts | Compliance filter, opt-out footer, scoring |
| Deal Analysis (`agents/deal.py`) | MAO = ARV × discount factor − repairs − closing − assignment fee, with every assumption and a sensitivity table | Parsing a plain-language request | All math (`Decimal`). Every figure the model passes must appear in the user's message. |
| Follow-up (`agents/followup.py`) | Status-based tasks, reminders, hot-lead escalation, campaigns | Optional: invoking tools from natural language | The scan itself needs no LLM (`bravo run-cycle`) |
| Buyer Disposition (`agents/disposition.py`) | Matches approved cash buyers, drafts deal summaries, tracks replies | Classifying buyer replies (must quote the reply) | Matching, summary text, seller-info leak check |

### Lead scoring (`services/scoring.py`)

Rule-based so every point is explainable (shown in the dashboard). Signals include seller-initiated contact, timeline
under 30/90/180 days, distress (foreclosure, probate, divorce, liens, relocation, hardship), property condition,
vacancy, price flexibility, and negatives (firm price, listed with an agent, "just curious", asked to stop).
Leads at or above `BV_HOT_LEAD_THRESHOLD` (default 70) are escalated to a priority-1 task.

## Safety and controls

| Requirement | Where it is enforced | Test |
|---|---|---|
| Human approval for offers, contracts, outbound messages, payments, destructive actions | `approvals.py`, `services/actions.py`, `services/outbound.py` | `test_foundation`, `test_outbound`, `test_agents`, `test_api` |
| Every decision, tool call and approval is logged | `agents/base.py`, `audit.py`; `activity_logs` is append-only (ORM blocks update/delete); PII is redacted from log data | `test_every_agent_step_is_audited`, `test_audit_redacts_pii_and_is_append_only` |
| Role-based access control | `rbac.py` + `require(...)` on every route. Self-approval blocked for offers, contracts, payments, deletions | `test_auth_required_and_rbac`, `test_contract_payment_and_destructive_need_admin` |
| Encrypt secrets and sensitive data | Seller/buyer name, phone, email, message bodies, summaries are Fernet-encrypted at rest (key rotation supported); lookups use HMAC blind indexes; secrets can be stored as `enc:` values (`bravo encrypt-secret`) | `test_pii_encrypted_at_rest`, `test_key_rotation_and_blind_index`, `test_enc_secrets_roundtrip` |
| SMS opt-out | STOP/UNSUBSCRIBE/CANCEL/END/QUIT/STOPALL: recorded, lead set to do-not-contact, pending and scheduled sends blocked, pending approvals rejected. Re-checked again at send time. START re-subscribes. Twilio signature verified. | `test_opt_out_blocks_and_cascades`, `test_send_time_guard_blocks_even_if_cascade_missed`, `test_twilio_webhook_signature_stop_and_reply` |
| Contact only in approved hours | `services/hours.py`: window and days from config, evaluated in the recipient's time zone (by property state). Approved messages outside the window are *scheduled*, then sent by `run-cycle`. | `test_contact_hours_recipient_local`, `test_outside_hours_is_scheduled_then_dispatched` |
| Rate limits | Per-user API limit, stricter limit on LLM endpoints, public-endpoint limit, daily SMS caps (global and per lead) | `test_global_rate_limit`, `test_agent_rate_limit`, `test_daily_caps_defer` |
| Retry handling | Exponential backoff with jitter for Twilio, Google and HubSpot (429/5xx/network); permanent errors are not retried; Anthropic SDK retries built in | `test_twilio_retries_*`, `test_gmail_send_retries_*`, `test_hubspot_retry_*` |
| No legal, tax, lending advice or guarantees | Agent system prompt + a hard filter on every draft (guarantees, tax/legal/lending advice, dollar figures, pressure tactics). Non-compliant drafts are rejected and the model must retry. | `test_compliance_checks`, `test_draft_blocked_by_compliance_then_model_retries` |
| Never fabricate comps, buyers, facts, figures | There are no comps or property-data tools. Deal inputs must be user-stated. Intake extractions are grounded in source text. Buyer matching uses only stored facts and reports unknowns instead of guessing. Buyer replies need a verbatim quote. | `test_deal_agent_refuses_invented_numbers`, `test_email_agent_extracts_and_drops_hallucinations`, `test_matching_only_approved_verified_and_never_guesses`, `test_buyer_reply_routing_and_agent_classification` |
| Never expose seller private info to buyers | Buyer summaries are built from stored property facts only, street hidden by default, then scanned for seller name/phone/email/discovery text | `test_deal_summary_never_leaks_seller_info` |
| Prompt injection | Seller/buyer/email text is fenced as untrusted data and agents have no send capability | `test_prompt_injection_in_seller_text_cannot_trigger_sends` |

Other protections: approval payload hash (tampering after review is refused), expiring approvals (72h),
atomic decision (no double execution), an offer cannot exceed the analysis MAO, per-tool savepoints so a failed
tool leaves no partial writes, and `archive` (reversible) vs `purge` (hard delete, PII removed, audit trail kept) as separate destructive actions.

### Roles

| Role | Can |
|---|---|
| `admin` | Everything, including approving contracts, payments, deletions; manages users |
| `manager` | Runs the pipeline; approves messages, campaigns, offers, buyer blasts |
| `acquisitions` | Works leads and conversations, runs deal analysis, requests offers, approves messages |
| `dispositions` | Manages buyers, deal matching, buyer blasts (can approve buyer blasts) |
| `viewer` | Read-only, with phone/email masked |

## Jarvis as its own app, synced with Bravo Vision

Jarvis is a standalone app: its own API, database and browser dashboard, served from one HTTPS link
(`https://<your-domain>`). It stays in step with the Bravo Vision lead generator over a small REST contract, so neither
app needs the other's database.

| Direction | Endpoint | Auth | What it does |
|---|---|---|---|
| Bravo Vision → Jarvis | `POST /api/sync/bravo/leads` `{"leads":[{external_id, name, phone, email, street/city/state/zip or address, notes, timeline, ..., sms_consent, voice_consent, consent_source}]}` | `X-Sync-Key: $BV_BRAVO_SYNC_KEY` | Up to 200 leads per call. Idempotent on `external_id`, so re-pushing updates the same lead. Consent is recorded only if the payload says so. |
| Bravo Vision ← Jarvis (pull) | `GET /api/sync/bravo/leads?since=<ISO time>` | same key | Status, score, priority, next action, discovery answers, consent flags. **No phone, email, or address.** |
| Jarvis → Bravo Vision (push) | `POST {BV_BRAVO_BASE_URL}/api/jarvis/events` (`contact_upsert`, `deal_upsert`, `note`, `stage`) and `GET .../api/jarvis/leads` | `Authorization: Bearer $BV_BRAVO_API_KEY` plus `X-Jarvis-Signature: sha256=<HMAC of body, key BV_BRAVO_SYNC_KEY>` | Set `BV_CRM_PROVIDER=bravo`. Jarvis pushes lead status, conversation summaries and consent changes as they happen. Retries on 429/5xx. |

The first two endpoints are live in Jarvis now. The third is the **client**: Bravo Vision needs `/api/jarvis/events`
(and optionally `/api/jarvis/leads`) to receive it. I did not have Bravo Vision's code or API, so this contract is
my proposal; send me its real API and I will adapt `integrations/crm/bravo.py` to match instead.

## Consent, voice, and automation switches

- **Consent** is stored per lead (`sms_consent`, `voice_consent`, source, time). Record it with
  `POST /api/leads/{id}/consent {"sms_consent": true, "voice_consent": false, "source": "Seller web form submission ID 12345"}`
  or send it in the intake/sync payload. In production (`BV_REQUIRE_CONSENT` blank) SMS is blocked without it, in drafts,
  campaigns, buyer blasts, and again at send time. A seller who texts first is recorded as consenting to replies on that thread.
  **Voice always requires `voice_consent`.** Consent can be revoked at any time.
- **AI voice calls** (`services/voice.py`, `agents/voice.py`): `POST /api/leads/{id}/calls/request` creates an approval showing the exact opening
  line (it says it is an automated assistant and that the person can say stop). Only after a human approves does Jarvis place the call, and only
  if there is voice consent, no opt-out, it is inside contact hours, the voice switch is on, and the lead has had no call today. You do not
  configure Twilio voice webhooks by hand: Jarvis passes `.../api/webhooks/twilio/voice-turn` and `.../voice-status` with each call, and both
  verify Twilio's signature. During the call the model has only `record_discovery`, `update_lead_status` and `end_call`; every spoken reply passes the
  compliance filter (no figures, guarantees, or advice) or the call is ended with a hand-off to a human; "stop calling me" ends the call, opts the
  number out of voice and SMS, and marks the lead do-not-contact. Calls are capped by `VOICE_MAX_DURATION_SECONDS` and `voice_max_turns`. Transcripts are
  encrypted at rest and a review task is created after each call. Each turn calls the model inside the Twilio webhook, so check real-world latency.
- **Kill switches:** with `AUTOMATIC_MESSAGING_ENABLED=false` / `AUTOMATIC_VOICE_ENABLED=false`, approved messages and calls are **held**
  (status `scheduled`, logged as `message.held` / `call.held`) and nothing is transmitted. Keep both off while testing; flip them on and the
  scheduler sends what is waiting.
- **Texas / Central time.** `APP_TIMEZONE=America/Chicago`, quiet hours 8pm to 9am (`QUIET_HOURS_START/END`), Sundays off by default. Contact windows
  are evaluated in the lead's own time zone by property state (Texas = Central), the dashboard shows all times in Central, and Jarvis calls you Bryce
  (`OWNER_NAME`). Texas has its own telemarketing and wholesaling rules; have your attorney confirm the hours and disclosures.

## Putting it on a link

```bash
cd bravo-vision
cp .env.example .env     # set APP_SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, PUBLIC_BASE_URL (=https://DOMAIN), keys
docker compose up --build -d
docker compose run --rm api python -m bravo.cli create-user you@example.com --role admin   # prints your token once
```

Point your domain's DNS at the server; Caddy obtains the HTTPS certificate. Open `https://<your-domain>`, paste your token.
Then set Twilio's incoming-SMS webhook to `https://<your-domain>/api/webhooks/twilio/sms`. Any host that runs Docker works
(a small VPS is enough); I cannot deploy it for you from this environment.

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in; generate BV_ENCRYPTION_KEYS as the file explains
alembic upgrade head            # PostgreSQL schema (BV_DATABASE_URL)
python -m bravo.cli create-user you@example.com --role admin    # prints your API token once
uvicorn bravo.main:app --reload
```

Dashboard: `cd dashboard && npm install && npm run dev`, open http://localhost:5173 and paste your token.

Scheduler (follow-up scan, escalation, reminders, sending deferred approved messages), every 5 minutes:
`python -m bravo.cli run-cycle` (cron, systemd timer, or the `scheduler` service in `docker-compose.yml`).

Docker/production: see "Putting it on a link" above.

### Connecting services

- **Twilio:** set `BV_TWILIO_*`. Point the number's incoming-message webhook at
  `https://<your-host>/api/webhooks/twilio/sms` and set `BV_PUBLIC_BASE_URL` to exactly that origin (used to verify signatures).
  Complete A2P 10DLC registration before texting at volume.
- **Gmail / Calendar:** create an OAuth client, authorize the business account once for the scopes in
  `integrations/google_api.py`, and put the refresh token in `BV_GOOGLE_REFRESH_TOKEN`. Label inbound seller emails
  `leads` (or change the query in `poll_gmail`). Calendar holds are created **without attendees**; inviting a seller
  is an outbound message and goes through approval.
- **Web form:** `POST /api/intake/web-form` with header `X-Form-Key: $BV_WEBFORM_KEY`. Common field names are mapped
  (name, phone, email, address/street, city, state, zip, notes, timeline, ...). Include a hidden `website` field as a honeypot.
- **CRM:** `BV_CRM_PROVIDER=local|hubspot`. To add Salesforce, Follow Up Boss, etc., subclass `CRMAdapter`
  (`integrations/crm/base.py`) and register it in `integrations/registry.py`. The rest of the system does not change.
  CRM outages are logged and never block the pipeline.

### Tests

```bash
cd backend
pytest -q --cov=bravo                                  # SQLite, ~10s
BV_TEST_DATABASE_URL=postgresql+psycopg2://user@localhost/bravo_test pytest -q   # same suite on PostgreSQL
```

## What has and has not been verified

Verified in development: the full suite (157 tests) passes on SQLite **and** on PostgreSQL 16; the Alembic baseline
migrates up and down on PostgreSQL and `alembic check` is clean; the dashboard builds and was driven end to end in
Chromium (sign-in, approval queue, approve, leads, deal calculator, activity log).

**Not verified, so test before relying on them:**
- AI voice calling has only been exercised with a scripted model and simulated Twilio webhooks, never a real phone call. Test with your own number first.
- The Bravo Vision sync contract is a proposal (see above); it has not been run against the real Bravo Vision app.
- Twilio, Gmail, Calendar and HubSpot were tested against fakes and mock transports, never against live accounts.
  The HubSpot property names and association IDs should be checked against your portal.
- The Claude integration was tested with a scripted model. Tune prompts against real conversations and review
  early drafts closely before approving at volume.
- The Dockerfiles and compose file were written but not built (no Docker daemon was available); the compose file passes `docker compose config`.
- Rate limits are in-process, so run a single API worker (the Dockerfile does) or move them to Redis before scaling out.
- Time zones are per-state defaults; split-zone states are approximated.

## Compliance: please read

This software enforces the controls above but does not make a business compliant. Before you text or email sellers
or buyers, have a real estate attorney confirm: TCPA consent and Do-Not-Call scrubbing (federal, state, and your own
list; this system honors STOP but does not scrub the DNC registry), CAN-SPAM, A2P 10DLC, state rules on
wholesaling, licensing and disclosure of equitable interest, and any rules on AI-generated outreach. Contract
language must come from attorney-reviewed templates: Jarvis records an approval for a document you supply and
never writes contract terms. Payments are approval records only; Jarvis does not move money.
