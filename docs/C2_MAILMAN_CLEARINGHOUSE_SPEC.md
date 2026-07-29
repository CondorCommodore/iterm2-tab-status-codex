# COS Message Delivery Hub and Session Delivery Agent

Status: first-pass design specification; no authority activation implied.

## 1. Purpose and non-goals

The **message delivery hub** is a coord-api TRANSPORT projection/service role
for semantic messages, their delivery obligations, and required responses. It
is not a fifth authority plane or a separately durable service. A machine-local
**session delivery agent** is the last-mile adapter for one registered Claude or
Codex CLI session. Together they separate immediate durable acceptance from
state-aware presentation to a live session. `clearinghouse` and `mailman` may
remain internal implementation aliases during migration, but they are not
user-facing product terms.

This design does not create another task or message authority. Coord-api remains
the durable truth for messages, tasks, sessions, leases, receipts, and audit
events. The terminal is not a queue. A successful keystroke is not proof that an
agent received, understood, answered, or completed a message.

The delivery hub must not:

- reinterpret a summary as an authoritative instruction;
- acknowledge receipt or response on behalf of a recipient model;
- generate `M-####` messages for transport acknowledgements;
- infer session identity from a reusable TTY alone;
- permit an LLM classifier to grant itself interrupt or restricted-override authority;
- dispatch during coord-api loss or after a relevant lease has expired; or
- turn informational broadcasts into an alternate work-item model.

## 2. Authority and identity model

### 2.1 Queue ownership and delivery binding

The authoritative queue is a coord-api TRANSPORT projection over existing
messages and belongs to a logical coordination principal, such as
`mikebook_codex`, not to an ephemeral CLI UUID or a new queue identity. Each
queued item may have either:

- an **agent scope**, deliverable to a verified successor session; or
- an **exact-session scope**, deliverable only to the named `coord_session_id`
  and `cli_session_id` tuple.

At presentation time the delivery hub creates a delivery binding containing:

```text
logical agent
  + coord session UUID
  + CLI session UUID
  + iTerm session UUID
  + TTY
  + host
  + runtime
  + session-capability digest/version
  + controller lease epoch
  + worker/session lease epoch
```

Every field is checked immediately before terminal action. The iTerm UUID is the
terminal edge identity; the TTY is a corroborating property and must match. The
coord session capability proves that the logical recipient owns the active
session. The CLI UUID prevents a newly launched runtime in the same tab from
receiving stale traffic.

The coord-api derives authoritative actor and session coordinates from the
authenticated principal and its exact-session capability. A client-supplied
`actor_id`, `session_id`, TTY, or allowlist is only an untrusted assertion. The
pure Test 1 reducer accepts those fields solely as a policy-model input; it does
not prove authentication. Authoritative receipt coordinates are the recipient
agent, coord session UUID, CLI session UUID, iTerm session UUID, controller
epoch, worker reservation epoch, and authenticated actor principal. TTY remains
corroborating evidence rather than identity.

For actionable traffic, the existing task and attempt must be reserved to this
exact identity tuple before presentation. A recipient receipt cannot substitute
for task ownership, and the hub must not present a message as actionable when a
different session holds the task lease.

### 2.2 Single authority and fencing

Only the holder of the live supervisor lease may authorize terminal presentation.
The machine-local adapter verifies the expected supervisor epoch and the target
worker/session reservation epoch immediately before every state-changing byte,
including Escape, prompt text, CR, LF, or recovery keystroke. Lease loss between
steps aborts the sequence. A successor controller uses a higher epoch; receipts
and late completions from an older epoch cannot advance delivery-hub state.

The session delivery agent is an adapter, not a controller. It cannot claim
tasks, change urgency, synthesize instructions, acquire supervisor authority, or
choose a new recipient outside the delivery hub's fenced delivery instruction.

## 3. Message urgency

The working neutral vocabulary is deliberately provisional:

```text
Critical > Urgent > Elevated > Normal
```

| Public working label | Operational treatment |
| --- | --- |
| Normal | Hold externally and present at the next natural idle/ready boundary. |
| Elevated | Place ahead of Normal and present at the next verified model boundary; do not interrupt active work by default. |
| Urgent | Place ahead of Elevated and Normal; steer by interrupting current processing through the shared verified Escape sequence. Always requires recipient acknowledgement and a disposition. |
| Critical | Place at the front; interrupt immediately under pre-authorized policy. Always requires prompt recipient acknowledgement and a disposition. Failure escalates immediately. |

FIFO applies inside a class using existing coord-api message/event ordering,
except when a message explicitly supersedes another item, expires, or is
cancelled by an authorized message. Any future explicit acceptance cursor must
be defined by TRANSPORT rather than invented by COS. A higher urgency postpones
lower traffic; it does not silently delete it.

An internal **restricted override** is a separately authorized attribute, not a
fifth normal urgency and not available to ordinary agents or classifiers. It
requires an operator-approved sender role, authenticated and signed envelope,
explicit scope, short TTL, response obligation, and immutable audit event. Its
transport policy may authorize stronger interruption or recovery, but safety,
credential, destructive-action, and remote-host boundaries still apply. Any
historical compatibility name for this attribute remains hidden from general
users.

If urgency is missing, the message is Normal. An optional LLM assistant may
propose an urgency or digest, but only deterministic policy or an authorized
sender can change it. An LLM can never promote traffic to Critical or restricted
override. The neutral labels, their number, and their descriptions are
hypotheses to test with operators. An implementation-only compatibility mapping
is isolated in the note at the end of the experiment section and must not leak
into general-user APIs or UI.

## 4. External authoritative queue

Coord-api accepts a semantic message immediately, assigns its `M-####` identity,
and places one delivery obligation per logical recipient in the delivery hub.
Acceptance is not terminal presentation.

The queue is stored outside Claude, Codex, iTerm, and local watcher files. Only
the currently selected item is injected. This permits later Elevated, Urgent, or
Critical traffic to move forward without editing or clearing a TUI input buffer.

An item includes:

```json
{
  "message_id": 28650,
  "urgency": "urgent",
  "recipient_agent": "mikebook_codex",
  "target_scope": "agent",
  "target_coord_session_id": null,
  "subject": "Suspend merge activity for repository X",
  "created_at": "2026-07-29T20:00:00Z",
  "expires_at": null,
  "ack_required": true,
  "response_required": true,
  "response_deadline": "2026-07-29T20:15:00Z",
  "supersedes": [],
  "correlation_id": "incident-123",
  "state": "queued"
}
```

The authoritative body remains in the numbered message and is fetched by ID. Queue rows
may copy only routing metadata and a non-authoritative subject/header.

## 5. Session inbox digest

Each active session receives a replaceable **session inbox digest**. It is an
unnumbered control-plane projection, identified by recipient, monotonic sequence,
and content digest. It is not an `M-####` message and does not itself require an
acknowledgement message. Any legacy projection key is an internal migration
detail; user-visible UX and new public APIs use neutral session/digest language.

The manifest may contain:

- inline urgent intelligence and heads-up summaries, clearly labelled as
  informational and linked to their sources;
- headers for queued but not presented messages;
- headers for presented but unacknowledged messages;
- outstanding acknowledgement, response, or disposition obligations; and
- tasks currently assigned to the logical agent and/or exact UUID, sourced from
  existing coord-api task/attempt/lease records.

It must never inline authoritative instructions, handoffs, or any text whose exact
wording carries authority. Those entries contain only `message_id`, urgency,
sender, subject, timestamps, integrity digest, and obligation flags. The session
downloads the authoritative body from `/messages/{id}` with exact-session
credentials where applicable.

```json
{
  "kind": "session_inbox_digest",
  "recipient_agent": "mikebook_codex",
  "recipient_coord_session_id": "coord-uuid",
  "sequence": 482,
  "generated_at": "2026-07-29T20:00:00Z",
  "previous_digest": "sha256:...",
  "digest": "sha256:...",
  "urgent_intelligence": [
    {
      "kind": "heads-up",
      "summary": "PR 17 changed head and is now mergeable",
      "source_ref": "github:repo#17",
      "observed_at": "2026-07-29T19:59:00Z"
    }
  ],
  "queued_message_headers": [
    {
      "message_id": "M-28650",
      "urgency": "urgent",
      "subject": "Suspend merge activity for repository X",
      "sender": "operator",
      "body_digest": "sha256:...",
      "ack_required": true,
      "response_required": true,
      "expires_at": null
    }
  ],
  "presented_unacknowledged": [],
  "response_obligations": [],
  "assigned_tasks": [
    {
      "task_id": "T-9482",
      "attempt_id": "...",
      "summary": "Trusted-report production integration",
      "status": "in_progress",
      "lease_epoch": 7,
      "lease_expires_at": "..."
    }
  ]
}
```

A session checkpoints `last_digest_sequence_seen` and content digest through an
unnumbered upsert. A skipped sequence or broken digest chain causes a full
digest refresh, not blind acknowledgement of its contents. Urgent and Critical
traffic still triggers active delivery; the next digest reconciles missed or
duplicated urgent presentation.

## 6. Shared Claude/Codex delivery contract

The delivery hub emits runtime-neutral actions:

| Action | Meaning |
| --- | --- |
| `HOLD` | Retain in the external queue; do not touch the terminal. |
| `PRESENT` | At verified idle/ready state, type the selected message reference and submit it. |
| `STEER` | Deliver at the next safe model boundary without abandoning current work. |
| `INTERRUPT` | Interrupt current processing, verify a safe prompt, then present the selected message. |

Default mapping:

| Worker state | Normal | Elevated | Urgent | Critical |
| --- | --- | --- | --- | --- |
| `idle` / ready | PRESENT | PRESENT first | PRESENT now | PRESENT now |
| `running` | HOLD | STEER at boundary | INTERRUPT | INTERRUPT now |
| `needs_input` | HOLD | HOLD or present after current prompt | INTERRUPT only if the message resolves or overrides the prompt | INTERRUPT |
| `blocked` | HOLD/digest | PRESENT | INTERRUPT | INTERRUPT |
| `stale`, `lost`, `unknown` | HOLD | HOLD and escalate | no injection; recover/reroute | no blind injection; recover/escalate |

This table is the contract to test, not permission to inject. The Phase 1 pure
reducer deliberately proposes active delivery only for verified `idle` or
`running` observations. It returns HOLD for `reserved`, `needs_input`,
`blocked`, `stale`, `lost`, and `unknown` until Test 2 establishes a safe,
versioned runtime-profile transition for that state.

Claude and Codex implement identical logical semantics. Runtime profiles may use
different observations, but neither receives weaker identity, fencing, or
postcondition checks. There is no Claude-only shortcut that equates hard Enter
with receipt, and no Codex-only durable soft queue inside the TUI.

### 6.1 Urgent/Critical Escape protocol

For Urgent and Critical traffic, the shared initial physical action is Escape.
It is a
state-transition protocol, not a raw keystroke macro:

1. Lock the exact target iTerm UUID against concurrent deliveries.
2. Capture a fresh observation: TTY, foreground runtime, CLI and coord UUIDs,
   worker state/readiness, processing marker, prompt/screen fingerprint, and
   any existing input-buffer condition.
3. Verify supervisor and worker/session lease epochs.
4. Record an `interrupt_requested` event and send one Escape.
5. Observe a bounded transition. Success requires evidence that the same
   runtime reached an allowed prompt/input-ready state. A shell prompt,
   permission chooser, closed runtime, changed UUID, or unchanged running state
   is not success.
6. Re-verify identities and both epochs immediately before text.
7. Preserve or clear existing input only according to an explicit, audited
   policy. Never erase unknown queued operator text automatically.
8. Type a compact urgency envelope that references the authoritative
   `M-####` body, then send CR and LF using the proven character path.
9. Verify the prompt was accepted through a new state transition and write a
   presentation receipt. This still does not constitute recipient receipt.
10. Require the Claude/Codex model to fetch the message and write its own
    recipient receipt.

Urgent permits a bounded graceful transition before escalation. Critical has a shorter
deadline and immediately raises a recovery/operator alert on failure. Repeated
Escape or process termination is not automatic; it requires an explicit policy
and, for stronger action, restricted-override authority.

## 7. Receipt and response lifecycle

### 7.1 Producers and proof

| Stage | Producer | What it proves |
| --- | --- | --- |
| `accepted` | coord-api | Semantic message is durable and has an `M-####` ID. |
| `routed` | delivery hub | A logical recipient obligation exists and is ordered. |
| `delivery_bound` | delivery hub | A verified exact session was selected under a live epoch. |
| `presented` | terminal edge/session delivery agent | The exact CLI accepted the delivery prompt/reference. |
| `received` | Claude/Codex session | The recipient fetched and recognized the authoritative message. |
| `responded` | Claude/Codex session plus delivery-hub correlation check | A demanded substantive reply or disposition was recorded. |
| `closed` | delivery hub | Every required obligation has reached an allowed terminal state. |

The delivery hub records all stages but cannot manufacture `received` or
`responded`. A session delivery agent can write `presented` only with observed postcondition
evidence. An iTerm `async_send_text` return, CR/LF submission, process exit code,
or ambient `running` state alone is insufficient.

`presented` and `received` remain independent evidence. A recipient may prove
that it received a message when the edge lacks a trustworthy presentation ACK,
but that does not retroactively create an observed edge receipt. Any inferred
presentation used for UX is labeled separately and is never authoritative.

### 7.2 Unnumbered control records

ACKs are receipt upserts and append-only message events; they never consume the
semantic `coord_messages.id` sequence and never appear as `M-####` traffic.

Candidate coord-api-owned extensions/read models are shown below. The names are
illustrative and must reconcile with existing message reads, handoff ACKs,
execution state, bus events, and idempotency coordinates before any migration.
They must never become local COS tables or a separately authoritative database:

```text
message_delivery_obligations
  PK (message_id, recipient_agent, delivery_generation)

message_delivery_bindings
  PK (obligation_id, binding_generation)
  UNIQUE (obligation_id) WHERE active

message_receipts
  PK (obligation_id)
  accepted_at, routed_at, bound_at, presented_at, received_at,
  responded_at, closed_at, state, response_message_id, last_error

message_delivery_events
  PK event_id (UUID/ULID or internal sequence, never M-numbered)
  obligation_id, transition, actor, epochs, evidence_digest, occurred_at

session_inbox_digest_checkpoints
  PK (recipient_agent, coord_session_id)
  last_sequence_seen, last_digest, updated_at
```

Receipt upserts use compare-and-set state versions. Audit events are append-only.
Neither receipt mutation nor manifest checkpoint emits a message or wake event.

### 7.3 Response-required enforcement

`ack_required` and `response_required` are separate fields. `received` may close
an informational message only when no response is required. A required response
closes only when the delivery hub validates a substantive numbered reply with:

- `reply_to` equal to the original message ID;
- matching `correlation_id` (or a server-derived one);
- an authorized sender equal to the recipient obligation;
- `created_at` equal to or later than the original message creation time;
- an authenticated producing coord session enrolled to the recipient for
  agent-scoped traffic;
- for exact-session traffic, an authenticated producing coord session equal to
  the active delivery binding;
- a disposition allowed by the message's explicit shadow response policy; and
- exact required task/result reference values where that policy declares them.

The Phase 1 metadata fields are `allowed_response_dispositions` and
`required_response_references`. They only constrain an existing
`requires_response=true` obligation; defaults are empty, and the reducer does
not infer them from subject text, message content, task state, or an LLM.

`received` or `acting` are unnumbered receipt states, not substantive replies.
A precise blocker or requested answer is a numbered semantic response. Deadlines
are evaluated by the delivery hub. Overdue Normal/Elevated traffic raises status;
overdue Urgent/Critical traffic triggers urgency-specific escalation without
fabricating a reply.

### 7.4 State machines

Delivery obligation:

```text
accepted -> queued -> routed -> delivery_bound -> presenting -> presented
          -> received -> [response_due -> responded] -> closed
```

These are auxiliary message-delivery projection states; they do not redefine
task-attempt or message-execution terminality. Allowed terminal alternatives are `no_response_required`, `declined`,
`superseded`, `expired`, `session_ended`, `delivery_failed`, and
`operator_blocked`. `delivery_failed` and `operator_blocked` remain visible and
may still require operator resolution; "terminal" here means no automatic
forward transition under the current generation.

Presentation attempt:

```text
planned -> fenced -> interrupting? -> prompt_ready -> injecting -> presented
        \-> aborted_epoch_loss | identity_mismatch | unsafe_screen
         | no_prompt_transition | transport_error | timed_out
```

State transitions are monotonic per delivery generation. A retry creates a new
attempt, not a regression from `presented` to `queued`. Terminal response states
cannot be overwritten by late/stale sessions.

## 8. Supersession, expiry, retry, and succession

### 8.1 Supersession and cancellation

`supersedes` names exact message IDs and requires sender authority over the
superseded instruction or explicit operator cancellation authority. Supersession
closes only unpresented obligations automatically. If the old instruction was
presented or received, the new message must explicitly cancel it and itself be
presented; the delivery hub tracks both obligations until cancellation is
acknowledged. Summaries and LLM classifiers cannot supersede authoritative
messages.

### 8.2 Expiry

Expiry prevents new presentation after `expires_at`. It does not erase audit or
silently cancel work already started. Expired unpresented instructions close as
`expired`; expired presented instructions require an explicit disposition or operator
policy. Clock decisions use coord-api/server time. Short-TTL Urgent/Critical traffic that
expires undelivered escalates.

### 8.3 Retry and idempotency

Delivery is at-least-once with idempotent handling. The durable idempotency
coordinate is conceptually:

```text
(message_id, recipient_agent, delivery_generation, logical_action)
```

Any delivery generation or physical-attempt coordinate must derive from the
existing message, binding, controller epoch, and dispatch attempt/idempotency
coordinates; it is not a new semantic task or message identity. A lost
presentation ACK can therefore be reconciled from post-state without injecting
the body twice. Every field that affects reconstruction, including the
server-owned recorded timestamp, is covered by the immutable event digest. The
same idempotency key with a changed timestamp is a collision, not a replay.
Retries use bounded exponential backoff with jitter, urgency
deadlines, and fresh fencing/identity/state checks. They never repeat Escape or
Enter blindly.

### 8.4 Session succession

When a CLI/coord UUID ends, exact-session messages close `session_ended` and do
not leak to a successor. Agent-scoped, unpresented obligations remain queued.
The delivery hub may bind them to a successor only after that session is
active, capability-backed, registered in the machine manifest, and confirmed as
the same logical principal. A new binding generation records old and new UUIDs.
Presented-but-unacknowledged traffic is not silently rebound: reconcile its
post-state first, then either close the old attempt or explicitly redeliver.

## 9. Proposed coord-api TRANSPORT API evolution

All write endpoints require principal-bound authentication and idempotency keys.
Exact-session reads additionally require matching session ID and capability,
consistent with current coord-api exact-session message access.

```text
POST /messages
  Add urgency, ack_required, response_required, response_deadline,
  supersedes, target_scope. Returns semantic M-numbered message.

GET /messages/{id}
  Fetch authoritative body; enforce exact-session scope when set.

GET /agents/{agent}/message-queue
  Delivery-hub headers and obligation states; no authoritative bodies.

POST /messages/{id}/receipts
  Upsert recipient-owned received/acting state. Never creates a message.

GET /messages/{id}/delivery
  Delivery bindings, receipt projection, response obligation, attempts.

POST /messages/{id}/delivery-attempts
  Supervisor-only fenced presentation intent; returns attempt and immutable action token.

POST /messages/{id}/delivery-attempts/{attempt}/presented
  Edge-owned postcondition evidence and receipt transition.

POST /messages/{id}/disposition
  Recipient-owned terminal disposition; requires response_message_id when a
  substantive response is required.

GET /message-obligations?state=response_due&overdue=true
  Supervisor delivery-hub view.

GET /sessions/{coord_session_id}/inbox-digest
  Sequence/content digest for exact owned active session.

PUT /sessions/{coord_session_id}/inbox-digest-checkpoint
  Unnumbered idempotent checkpoint.
```

The session-delivery-to-edge local protocol should add an operation such as
`present_message` containing only message/attempt IDs, logical action, exact
target binding, expected epochs, payload digest, and one-use action token. The
edge should fetch or receive a digest-bound compact prompt from the trusted
delivery hub; arbitrary caller text must not ride this authority path.

## 10. Security and failure boundaries

- **Authentication:** verify sender principal, recipient principal, exact
  session capability, and machine registration. Message signatures cover body,
  urgency, scope, TTL, obligation flags, correlation, and supersession.
- **Authorization:** urgency affects scheduling, not underlying action
  authority. An urgent message cannot expand the run manifest's repositories,
  actions, deployment, credential, remote-host, destructive, or trading bounds.
- **Integrity:** canonical payload digests bind message body, session inbox digest
  sequence, delivery action, terminal observation, and receipts. Store observed
  evidence digests rather than trusting client booleans.
- **Least authority:** the session delivery agent holds only a scoped delivery credential. The
  terminal edge accepts only registered local sessions over a same-user
  mode-0600 socket and verifies epochs itself.
- **Coord-api outage:** active work may continue, but no new authoritative
  routing, presentation, receipt closure, or response disposition occurs.
- **Terminal uncertainty:** shell fallback, permission dialogs, stale screen
  signals, changed UUID, queued operator input, or ambiguous prompt state fail
  closed and escalate. Screenshot/LLM assistance may propose a bounded action;
  it does not replace deterministic identity and epoch checks.
- **Duplicate/reordered events:** compare-and-set state version and attempt IDs
  reject regressions. Late predecessor events remain audit-only.
- **Lost delivery agent:** queue remains durable. A replacement resumes from
  delivery-hub state and exact-session registration, not local cache.
- **Compromised summary:** a session inbox digest cannot execute an authoritative
  instruction because authoritative bodies are absent. The recipient fetches and
  verifies the numbered message.
- **Audit:** record actor, principal, host, all UUIDs, epochs, urgency,
  decision/action, before/after evidence digests, attempt, idempotency key,
  timestamps, errors, and resulting state. Do not place credentials or full
  sensitive message bodies in edge receipts.

## 11. Compatibility and migration

Current integration evidence already provides useful primitives: registered
worker identities, exact iTerm session resolution, principal-bound coord
configuration, live supervisor and worker lease verification, idempotency keys,
append-only local receipts, CR/LF submission, bounded headless resume, session
deliverability checks, `to_session_id`, message reads, handoff ACKs, execution
states, task records, and worker-state normalization.

### 11.1 Existing-field compatibility matrix

This matrix is a source inspection of current Home Lab coord-api contracts. It
does not claim that the pure reducer has durably exercised those routes.

| Test 1 meaning | Current coord-api coordinate | Safe reuse | Missing or different meaning |
| --- | --- | --- | --- |
| semantic message identity | `coord_messages.id`, `display_id`, `external_id` | Reuse `id` as the only semantic message coordinate; receipts remain unnumbered. | No delivery-obligation or attempt identity exists. |
| sender and recipient | `from_agent`, `to_agent`, `to_session_id` | Reuse logical-agent and optional exact-session addressing. | Agent-scoped fan-out does not select one responsibility-bearing session. |
| correlation and response | `reply_to`, `correlation_id`, numbered answer message | Reuse both fields and validate non-empty response plus authorized sender. | Subject text, read state, and ACK text are not response authority. |
| acceptance | `accepted`, `accepted_at`, `acknowledged_by = coord-api` | Interpret strictly as coord-api durability acceptance. | It is not presentation or recipient acknowledgement. |
| reads | `coord_message_reads`: `read_count`, `read_by`, `last_read_at` | Useful comparison telemetry. Exact-session message routes require matching session capability before access. | Stored read rows identify an agent, not a delivery generation or producing session. |
| required handoff ACK | `required_ack`, `handoff_acked_at`, `handoff_acked_by` | Reuse the requirement and agent-level acknowledgement for historical compatibility. Exact-session handoff routes authenticate the target session. | The stored ACK projection does not retain the producing coord-session UUID or lifecycle stages. |
| execution | `execution_state`, `execution_result`, `execution_updated_at`, `execution_updated_by` | Reuse as a separate message-execution projection; terminal `done`/`failed` cannot regress. | `updated_by` is an actor principal, not an exact producing session, delivery receipt, or task-attempt identity. |
| bus ACK | `coord_bus_events` `message_ack`: `message_id`, `agent_id`, `session_id`, `ack_type`, `idempotency_key`, actor metadata | Reuse its unnumbered append/idempotency shape. `/bus/messages/{id}/ack` authenticates the agent recipient. | The supplied `session_id` is not by itself proof that the authenticated actor owns that session; a verified actor-session binding remains required. |
| exact-session inbox delivery | `/sessions/{session}/inbox/lease` and `/lease/ack` require exact active session capability; server receipt records `acked_session_id` | Reuse this as the strongest current recipient-session receipt primitive. | It is Redis inbox consumption, not yet the complete accepted/routed/presented/responded/closed durable lifecycle. |
| handoff/session receipt destination | lease ACK validates the original sender session is still active before publishing the receipt | Reuse sender-session validation and exact destination. | The receipt is an inbox record, not a producer-stopped SQL delivery manifest. |
| delivery failure and DLQ | bus `queued_dead_letter`, session dead-letter drain, existing dispatch receipts | Reuse as failure inputs and comparison telemetry. | No single message-delivery obligation currently binds retry exhaustion, target session, controller epoch, and final disposition. |
| proposed urgency policy | Test 1 `urgency`, `requires_response`, `supersedes_message_id` | Shadow-only policy metadata keyed by the existing message ID. | These are not current `MessageCreate` authority fields and must not be written back before reviewed API evolution. |
| proposed receipt fencing | `actor_id`, verified `actor_session_id`, target `session_id`, `controller_epoch`, idempotency key | Derive from current authenticated principal/session registry, exact target, supervisor epoch, and existing idempotency coordinates. | The target-session UUID must never be reused as proof of the receipt producer's session. |

The compatibility rule is therefore additive: reuse current message IDs,
correlation, exact-session authentication, bus idempotency, execution guards,
and server-created inbox receipts, but do not collapse them into one synthetic
`received` fact. Any future durable delivery projection must retain the source
coordinate and proof strength of each input.

It also exposes gaps this design must not preserve:

- `CoordClient.post_receipt()` currently serializes a dispatch receipt into a
  new self-addressed `/messages` activity, consuming an `M-####` ID. Replace
  this usage with receipt/event endpoints; keep the local append-only cache only
  as recovery evidence.
- Existing message presentation calls coord-api acceptance an acknowledgement
  (`acknowledged_by = coord-api`). Rename or interpret that strictly as
  acceptance, not recipient ACK.
- Message reads, handoff ACKs, execution states, CR consumer ACK/read calls, and
  terminal `observed_ack` each prove different things. Migration must map them
  explicitly; it must not collapse them into `received`.
- Current dispatch recognition of a start-state transition is presentation
  evidence at best. It does not prove model receipt or response.
- Existing session routing can fan out to active sessions. The delivery hub
  needs one obligation per logical recipient plus explicit exact-session
  bindings to avoid duplicate semantic responsibility.

Staged migration without rewriting history:

1. Introduce receipt/event and manifest tables/endpoints in shadow mode.
2. Dual-write new dispatch audits to legacy numbered activity messages and new
   unnumbered receipts for a bounded comparison period.
3. Classify historical/new self-addressed ACK-like activity messages for
   metrics only. Do not delete, renumber, or silently transform old messages.
4. Change producers to write receipts only; retain reading compatibility for
   legacy ACK messages.
5. Stop emitting numbered ACK/status chatter after parity and rollback proof.
6. Optionally hide recognized legacy transport noise from default inbox views,
   while preserving it in immutable history and exports.

Migration metrics should report numbered messages avoided, obligations open,
time accepted-to-presented, presented-to-received, received-to-response,
duplicate attempts prevented, expired traffic, and unresolved Urgent/Critical delivery.

## 12. Experiment-first validation

This document defines hypotheses, not settled product behavior. Experiments begin
with synthetic messages, fake sessions, recorded observations, and shadow
decisions. No experiment injects real work until its shadow results meet its
stopping conditions and the operator explicitly enables the next stage. Each
experiment is reversible through a feature flag and leaves the current delivery
path available as the control.

The validation program has exactly three escalating executable protocols. A
protocol advances only after its predecessor passes and the operator authorizes
the next scope. Detailed cases such as soft versus hard submission, digest
usefulness, duplicate delivery, and session succession are subcases rather than
independent experiments.

### Test 1: delivery-hub shadow simulation

**Hypothesis.** An external delivery hub can order messages, apply supersession,
produce a useful neutral session inbox digest, track unnumbered receipts, and
enforce response-required obligations without terminal input or `M-####`
inflation.

**Prerequisites.** A pure in-memory state reducer; deterministic clock; fake
logical agents, session UUIDs, lease epochs, and tasks; the legacy
instantaneous-routing decision available as a read-only control; and a recorder
that cannot call a terminal adapter or live coord-api write route.

**Fixture/envelope set.** At minimum:

- 24 synthetic messages spanning Normal, Elevated, Urgent, and Critical, with
  FIFO ties, equal timestamps, expiry boundaries, and mixed agent/exact-session
  scope;
- one authorized and one unauthorized supersession, one cancellation after
  presentation, and one expired message;
- required-ACK only, required-response, and no-response-required obligations;
- valid, empty, wrong-sender, wrong-`reply_to`, wrong-correlation, late, and
  stale-session responses;
- digest sizes of 1, 5, 20, and 50 headers, with informational summaries and
  assigned task headers but no authoritative message bodies; and
- duplicate, delayed, and reordered acceptance/receipt/response events across a
  controller epoch change and a synthetic session succession; and
- a stale manifest binding, runtime-profile drift in the same visible tab, and
  multiple nominally active sessions for one logical agent.

**Exact procedure.**

1. Freeze the clock, seed agents/sessions/tasks, and record a baseline empty
   queue and message sequence.
2. Reconcile the registered roster against fresh exact-session observations.
   Treat `active` registry status as a candidate signal, not liveness proof;
   reject stale manifest/runtime bindings and never fall back to broad
   same-agent fan-out.
3. Submit the fixtures to both the control decision recorder and the treatment
   simulator; the treatment must not invoke any terminal method.
4. Advance the clock through running, idle, expiry, and session-ended states.
5. At every step record the ordered queue, selected action, obligation state,
   session inbox digest sequence/content digest, and semantic message counter.
6. Apply supersession/cancellation fixtures and prove unauthorized or ambiguous
   changes fail closed.
7. Apply receipt and response fixtures, including retries and reordered events;
   reconstruct the pure-model projection solely from immutable input snapshots
   and unnumbered events. This is not durable producer-stopped readback proof.
8. Compare projected treatment actions with the control's instantaneous actions
   and produce a divergence report. No control action is executed.
9. Run the neutral-language comprehension check on the resulting queue/digest.
   Compare Normal/Elevated/Urgent/Critical against the historical four-level
   Routine/Priority/Immediate/Flash meanings as an anonymized experimental
   reference only. The reference vocabulary is not proposed product terminology
   and must not escape the research fixture or results.

**Control and treatment.** Control is the current instantaneous routing decision
and current read/handoff/execution signals, observed only. Treatment is the
external ordered queue, session inbox digest, unnumbered receipt/event lifecycle,
and correlated response enforcement.

**Observables and metrics.** Queue-order mismatches; unauthorized supersession;
false closure; missing response obligations; duplicate/stale transition
acceptance; expiry accuracy; digest body leakage; digest selection time and
operator comprehension; receipt state reconstruction; ACK-only numbered
messages avoided; and projected accepted-to-presented delay by urgency/state.

**Pass criteria.** Zero lost or misordered obligations; zero unauthorized
supersession; zero false closures; every required response remains open until a
valid correlated response/disposition; zero authoritative body text in digests;
zero receipt writes increment the message sequence; deterministic reconstruction
matches live treatment state byte-for-byte; and every divergence from the
control is explained by documented state/urgency policy. Every proposed
presentation is bound to one freshly observed exact session whose runtime and
identity tuple match the registered manifest.

**Rejection or simplification.** Reject external scheduling if it loses or
misorders any message. Reduce the digest to counts, urgent IDs, obligations, and
tasks if headers cause meaning errors. Release only `received` plus
`response_message_id` before the full lifecycle if historical states cannot be
mapped reliably. Collapse urgency levels if operators cannot distinguish them.

**Safety and rollback.** Shadow only, synthetic data only, no terminal socket,
no live service writes, and no historical rewrite. Disable the treatment feature
flag and discard the in-memory projection to roll back.

**Evidence artifact.** One canonical JSON bundle containing fixture digests,
control/treatment decisions, queue snapshots, state-transition events, final
receipt rows, session inbox digests, reconstruction digest, message-sequence
before/after, language results, and pass/fail assertions.

**Current Phase 1b technical result (not full Test 1 acceptance).** The pure
reducer is implemented in `scripts/cos_message_delivery_policy.py`; its canonical
24-message fixture is `tests/fixtures/message_delivery_shadow_v1.json`, pinned as
`639d66f9363e1211d876ab4862e104c06692309cceaed3985573aef5f7097a6f`.
The focused suite contains 66 passing cases, including an exhaustive 216-case
three-event lifecycle check, deterministic control/treatment divergence
evidence, and cancellation-after-presentation acknowledgement semantics. The
full repository suite passes 385 tests. An AST/import guard proves the reducer
has no coord client, edge, terminal, process, socket, SQL, or HTTP dependency.
The compatibility matrix in Section 11.1 records which current coord-api fields
can be reused and which proposed meanings require new durable state. The
anonymized language fixture is pinned as
`97b68352ce363ecd2ddce2484855627163919586f532e439911af06ce61518db`;
its deterministic scorer reports comprehension and confusion without selecting
policy for the human operator. This proves a bounded pure-model projection only.
It does not prove coord-api persistence, producer-stopped readback, real
delivery, completed human language comprehension, or operational latency.

Named Phase 1a mutations are permanent tests:

| Invariant | Mutate-to-red witness |
| --- | --- |
| receipts do not consume semantic message identity | add `id`/`display_id` to a receipt event |
| required responses close only with valid correlation and sender | wrong sender, wrong correlation, or response after attempted close |
| controller fencing is monotonic | present under a stale controller epoch |
| receipts are bound to the intended principal and exact session | use the wrong edge/hub/recipient actor or a stale session UUID |
| receipt stages remain independent | receive without fabricating `presented` |
| invalid replays cannot poison idempotency | submit wrong-session then valid same-key receipts, a same-key/different-payload collision, and a changed timestamp under the same key |
| terminal delivery state cannot regress | receive after `delivery_failed` |
| supersession is singular and authorized | foreign-sender or two-replacement supersession |
| digests contain headers, not authoritative bodies | copy private body text into a digest subject |
| exact-session traffic does not follow a successor | address a message to the old session UUID |
| failures remain visible | exhaust bounded delivery and require a DLQ projection |
| a substantive response is causal, non-empty, and exactly constrained | use a before-original timestamp, wrong sender, `reply_to`, correlation, disposition, required reference, blank content, or a superseded predecessor |
| responses come from a verified producing session | use an unverified agent-scoped session or another exact session under the same logical agent |
| digest bounds and FIFO are deterministic | replay limits 1/5/50 and equal timestamps |

Remaining work before Test 1 can pass is the human language/digest comparison
and the real integration tier: persist the artifact through coord-api, stop its
producer, reconstruct it through the supported read route, and compare live
control/treatment behavior. Those claims cannot be inferred from this pure
model.

**Pre-protocol diagnostics only, 2026-07-29.** In one operator-authorized
bounded review, the local edge returned `observed_ack=false`; the prompt was
nevertheless visibly queued and work began, proving byte submission is not a
presentation ACK. Another session sharing the same logical agent claimed the
task first. The intended tab correctly refused the conflicting lease and no
duplicate dispatch was issued.

In a later queue-only probe, delivery to the manifest's exact session was
rejected because that session was inactive. Fresh terminal observation showed
that visible tab/runtime assignments had changed, while the coord registry
still returned many nominally active sessions for the same logical agent,
including short-lived sessions. No broadcast fallback was used. Together these
diagnostics establish that registry `active` status alone is weak liveness and
that the roster must be refreshed and exact identity reverified before every
proposal. An inactive exact target, manifest/runtime mismatch, ambiguous
same-agent candidate set, or attempted fan-out is an Experiment 1 abort—not a
reason to reroute. Evidence artifacts retain digests and aggregate counts, not
private session capabilities or raw identifiers. These diagnostics count
toward neither Test 1 nor Test 2 acceptance.

**Unanswered decisions.** Final neutral urgency labels and count; default
ACK/response deadlines; whether presented cancellation needs a new recipient
ACK; minimum useful digest fields; and which legacy signals map to `received`
rather than merely `presented` or `read`.

### Test 2: safe dual-runtime delivery lab

**Hypothesis.** Enrolled disposable Claude and Codex sessions can implement the
same HOLD/PRESENT/STEER/INTERRUPT contract: idle-gated Normal/Elevated delivery
and verified Escape-plus-submit for Urgent/Critical delivery, without depending
on a runtime's internal prompt queue or losing pre-existing input.

**Prerequisites.** Test 1 passes; one disposable, explicitly enrolled Claude
session and one Codex session on the local host; exact iTerm, CLI, and coord UUID
bindings; synthetic worker/session leases; versioned runtime observation
profiles; mode-0600 local edge; disposable working directories with no repository
write access; and a kill switch that disables terminal actions.

**Fixture/envelope set.** For each runtime, use synthetic messages in idle,
running, needs-input, blocked, stale, lost, chooser, completion, and shell-fallback
states. Include Normal/Elevated idle delivery, Urgent/Critical preemption, a
non-empty pre-existing input buffer, one intentionally lost presentation reply,
duplicate delivery requests, reordered receipts, stale epochs, reused TTY with a
new UUID, changed CLI/coord UUID, missed first CR, and one safe soft-queue versus
external-queue comparison where the runtime supports it.

**Exact procedure.**

1. Enroll each disposable runtime and prove the exact identity tuple and lease
   epochs without sending input.
2. Replay all fixtures in observation-only mode and record predicted logical
   actions and expected postconditions.
3. For Normal/Elevated, queue externally, transition the session from running to
   verified idle, present only the selected synthetic message reference, submit
   through separate CR/LF characters, and require terminal presentation plus
   recipient receipts.
4. For Urgent/Critical, lock the target, capture before-state evidence, reverify
   both epochs, send one Escape, and require a verified same-runtime prompt-ready
   transition before any text. Reverify immediately before text and submit the
   synthetic reference through CR/LF.
5. When input is non-empty or screen state is ambiguous, verify that treatment
   aborts without clearing, Escape repetition, or submission.
6. Fault-inject duplicate requests, lost replies, reordered events, stale epochs,
   changed identities, and missed first submit. Reconcile durable attempt and
   post-state before any retry.
7. Repeat the same scenario matrix for both runtimes and compare contract-level
   results; record runtime-profile differences separately.

**Control and treatment.** Control is current immediate CR/LF presentation (or
observation-only prediction where immediate input would be unsafe). Treatment is
external HOLD, idle-gated PRESENT, and fenced/verified Escape-plus-submit for
urgent delivery. The soft-queue subcase compares TUI-preloaded text with keeping
all text external until selection.

**Observables and metrics.** False-idle and false-prompt rates; identity/epoch
rejections; accepted-to-presented and presented-to-received latency; unintended
interruptions; chooser dismissals; shell fallback; work/input lost; duplicate
semantic turns; duplicate or stale receipts accepted; retries; CR/LF recovery;
and Claude/Codex parity at the logical-action level.

**Pass criteria.** Both runtimes pass the same logical state matrix; zero bytes
reach a stale, mismatched, lost, or ambiguous target; zero pre-existing input is
cleared or submitted; every Escape has verified before/after evidence; every
presentation has a distinct recipient receipt; no fault produces a duplicate
semantic turn; and lease loss prevents the next terminal byte at every tested
boundary.

**Rejection or simplification.** Disable automatic interruption for a runtime or
profile if prompt-ready cannot be proven. Use explicit recipient pull plus a
visible inbox indicator if idle detection is unreliable. Standardize on external
HOLD and hard submit if soft queuing is runtime-specific or lossy. Reserve
interruption for Critical only if Urgent latency gains do not outweigh continuity
loss. Remove automatic retry if lost ACK cannot be distinguished from lost
delivery.

**Safety and rollback.** Local disposable sessions and synthetic content only;
no repository mutation, shell commands, process termination, restricted override,
remote access, or live work assignments. One Escape maximum per attempt. The
kill switch closes the edge operation and existing delivery remains available.

**Evidence artifact.** A signed local lab bundle containing enrolled identity
records, runtime/profile versions, fixture envelopes, before/after observation
digests, physical-action receipts, recipient receipts, fault schedule, per-runtime
matrix, latency/error metrics, and a redacted transcript or screenshot hash for
every Escape transition.

**Unanswered decisions.** Exact prompt-ready signals by runtime version; whether
Elevated ever uses safe STEER while running; policy for known versus unknown
pre-existing input; per-urgency transition deadlines; and whether any supported
runtime justifies a soft-queue optimization.

### Test 3: bounded real-work canary

**Hypothesis.** After Tests 1 and 2 pass, one low-risk coding slice can traverse
queue acceptance, exact-UUID presentation, recipient receipt, required response,
and completion while surviving one forced urgency change and one session restart
without duplicate work, lost context, or expanded authority.

**Prerequisites.** Tests 1 and 2 pass with reviewed evidence; explicit operator
authorization for this canary and selected runtime; a clean isolated worktree
from current main; one low-risk, reversible task with narrow file scope and fast
tests; exact registered session identity; live supervisor and worker/session
leases; response template; rollback commit/worktree plan; and no overlapping
owner or worktree.

**Fixture/envelope set.** One real task message with objective, authorized file
scope, acceptance test, stop condition, report destination, no-deploy/no-merge
boundary, required receipt, and required final response. Add one synthetic
Normal informational header, one authorized urgency update that moves ahead of
it without changing task scope, one duplicate delivery attempt, and one planned
session restart producing a verified successor UUID. The task message is
agent-scoped; a separate synthetic exact-session message proves that old-session
traffic does not follow the successor.

**Exact procedure.**

1. Snapshot repository/worktree, task, message, lease, and session state; prove
   the authorized write set is isolated and clean.
2. Run a shadow selection cycle and compare it with the current immediate path;
   require a reviewed match or documented policy difference before presentation.
3. Accept the task message, queue it behind the informational header as defined
   by initial urgency, bind the selected exact UUID, present it under live epochs,
   and require the recipient to fetch and write an unnumbered receipt.
4. Start the bounded slice and record task/attempt heartbeat. Inject the authorized
   urgency update; verify queue reorder/presentation without scope expansion or
   duplicate assignment.
5. Stop the disposable session at a safe checkpoint, mark the old binding ended,
   launch/enroll the successor, and prove its identity and new binding generation.
   Rebind only eligible agent-scoped, unpresented obligations. Prove the synthetic
   exact-session message remains on the old session.
6. Replay the duplicate delivery request and one delayed old-session receipt;
   prove neither creates a second turn nor advances successor state.
7. Complete the code change and narrow acceptance test. Post the correlated
   substantive numbered response with exact files, test result, and disposition;
   close the receipt obligation only after validation.
8. Stop at the authorized boundary. Do not deploy or merge; commit, push, PR, or
   merge only if separately and explicitly authorized for this canary.

**Control and treatment.** Control is a parallel shadow record of what current
instantaneous delivery would have selected and when. Treatment is the delivery
hub queue, exact binding, state-aware presentation, unnumbered receipts,
correlated response enforcement, urgency update, and successor generation.

**Observables and metrics.** Accepted-to-bound/presented/received/start/completed
latencies; queue-order changes; duplicate attempts prevented; old-session events
rejected; context recovered after restart; files touched versus scope; test
result; receipt/response completeness; numbered ACK messages avoided; operator
keystrokes; and any difference from the control decision.

**Pass criteria.** Exactly one task attempt and one semantic execution; correct
exact-UUID presentation; recipient receipt and valid correlated final response;
urgency changes order but not authority; restart preserves eligible work and
does not leak exact-session traffic; stale receipts cannot advance state; only
authorized files change; acceptance test passes; zero numbered ACKs; zero
operator keystrokes after start; and no deploy or merge without separate
authorization.

**Rejection or simplification.** Stop real-work rollout on duplicate execution,
scope expansion, wrong-session delivery, lost context, false closure, stale-event
acceptance, unexplained control divergence, or repository mutation outside scope.
If succession is unreliable, require explicit operator readdressing. If urgency
preemption harms the task, retain queue ordering but present only at idle. If
receipts add no decision value, reduce them to received/response-due/closed.

**Safety and rollback.** One isolated worktree and low-risk slice; no deployment,
activation, credential change, remote-host action, destructive cleanup, or merge
without separate authorization. On failure, stop presentation, preserve evidence,
leave the task with a precise blocker, and discard or revert only the isolated
canary work under the pre-approved rollback plan.

**Evidence artifact.** One exact-head canary bundle containing initial/final
repository status, task/message/envelope digests, queue and digest snapshots,
identity/lease generations, all presentation and recipient receipts, restart and
stale-event proof, changed-file list, test output, correlated final response,
control/treatment comparison, and explicit authorization-boundary attestation.

**Unanswered decisions.** Which repository and task qualify as low risk; which
runtime runs first; whether a canary commit or draft PR is useful; how long to
wait between receipt and escalation; whether restart is graceful or forced; and
what soak evidence is required before more than one real task is admitted.

**Experimental reference note.** Test 1 alone compares the neutral labels with
the standard four precedence meanings historically named Routine, Priority,
Immediate, and Flash. The names and shorthand codes are research-fixture data,
not product language, API enums, defaults, or activation policy. They must not
appear in general-user UX. The comparison may be removed or collapsed based on
operator comprehension results.

## 13. Staged acceptance plan

### Stage A: pure contract and queue tests

- Validate `Critical > Urgent > Elevated > Normal`, FIFO within class,
  deterministic defaults, and
  explicit authorized supersession.
- Prove restricted override is rejected from ordinary principals and classifiers.
- Prove ACK/receipt/event writes never increment the message sequence or publish
  inbox wakes.
- Prove response-required messages cannot close on accepted, presented,
  received, or uncorrelated reply states.
- Test expiry before and after presentation, cancellation, duplicate and
  reordered events, compare-and-set regressions, deadlines, and retry backoff.
- Property-test inbox-digest canonicalization, sequence/content-digest chaining,
  header-only authoritative messages, and omission of authoritative body text.

### Stage B: identity, binding, and succession

- Test logical-agent queue binding to exact Claude and Codex UUID tuples.
- Reject unregistered tabs, reused TTYs, changed CLI/coord UUIDs, wrong host,
  wrong runtime, missing session capability, self-delivery, and stale identities.
- Prove exact-session traffic becomes `session_ended`; prove agent-scoped queued
  traffic safely binds to a verified successor.
- Prove presented-but-unacknowledged traffic cannot be rebound without explicit
  reconciliation/new generation.

### Stage C: shared transport conformance

- Run the same HOLD/PRESENT/STEER/INTERRUPT scenario suite against Claude and
  Codex runtime profiles.
- Exercise idle, running, needs-input, blocked, stale, lost, unknown, queued
  operator text, permission chooser, shell fallback, closed tab, and stopped
  runtime states.
- For Urgent/Critical traffic, prove Escape is preceded and followed by exact identity and epoch
  checks, reaches a verified prompt, and never treats byte acceptance as success.
- Exercise CR and LF as separate characters, missed first submit, duplicate
  delivery, delayed state signals, and lost presentation response.
- Verify lease loss at every boundary prevents the next terminal byte.
- Bind every accepted, routed, presented, received, closed, failed, or cancelled
  receipt to both an allowed principal and a separately verified producing
  session. The target-session coordinate is not proof of the actor's session;
  a stale or foreign producing session fails closed even when the actor name,
  target session, and controller epoch otherwise look valid.

### Stage D: delivery-hub and response integration

- Create semantic messages with all four urgency levels; verify immediate coord-api
  acceptance and state-aware delayed presentation.
- Verify individual recipient ACKs for Urgent/Critical and configured Elevated traffic, without numbered
  ACK messages.
- Require and correlate substantive responses; test wrong sender, wrong
  `reply_to`, wrong correlation, empty result, and stale predecessor reply.
  Subject text is presentation metadata, not a response-binding coordinate, so
  a subject mutation must not override the existing message ID, `reply_to`, and
  correlation checks.
- Verify session inbox digests contain urgent intelligence, queued headers, presented
  unacknowledged items, response obligations, and existing assigned tasks, but
  no inline authoritative body.
- Stop the producer and reconstruct every obligation and manifest from durable
  storage.

### Stage E: failure, recovery, and canary

- Crash delivery hub, session delivery agent, edge, controller, and recipient at each lifecycle
  state; resume without duplicate responsibility or false closure.
- Simulate coord-api, provider/API, and terminal observation outages with bounded
  backoff while health and lease checks continue.
- Prove Urgent/Critical missed-ACK escalation and operator-visible precise blockers.
- Run shadow decisions alongside current instantaneous delivery and compare
  urgency ordering, chosen action, presentation result, response correlation, and
  message-volume reduction.
- Prove authority takeover and rollback across successive supervisor epochs.
- Complete an unattended canary with mixed Claude/Codex recipients, all four
  urgency levels, one supersession, one session succession, one forced stall, one
  controller interruption, required replies, zero numbered ACKs, and zero
  operator keystrokes.

## 14. Open design questions

1. Which coord-api principal roles may originate Critical traffic, and which
   separate role may set restricted override?
2. Is Elevated traffic while running always deferred to idle, or may
   runtime-specific model boundaries expose a deterministic safe STEER without
   Escape?
3. What exact post-Escape observations constitute prompt-ready for each
   supported Claude and Codex release, and how are profile versions enrolled?
4. May an Urgent/Critical message clear pre-existing unsubmitted input, or must any
   non-empty/unknown input always block for operator reconciliation?
5. What are the default ACK and response deadlines by urgency, and which
   senders may override them?
6. Should one logical-agent message create one shared obligation or one
   obligation per explicitly addressed recipient when an agent has multiple
   active sessions?
7. What constitutes a substantive response for each message intent, beyond the
   general correlation and non-empty-result rules?
8. Which urgent intelligence sources may be summarized inline, and what
   provenance/signature is required for those non-instruction summaries?
9. How long are delivery events retained, and which sensitive evidence fields
   require redaction or separate access control?
10. During dual-write migration, what parity window and rollback threshold are
    required before numbered ACK emission is disabled?
