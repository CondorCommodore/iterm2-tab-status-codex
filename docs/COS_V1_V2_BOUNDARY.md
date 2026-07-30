# COS capability boundary: V1 core and later work

This document is the scope boundary for the Codex COS skill. It prevents the
bootstrap supervisor, the message-delivery experiments, and the future Control
Room from being treated as one unfinished feature.

## V1: usable bootstrap supervisor

V1 is a Codex-only, Mac-local supervisor that can keep one bounded work lane
moving without Control Room. It uses existing coord-api tasks, attempts,
messages, leases, sessions, evidence, and results. It does not add a scheduler,
work-item database, or competing authority.

### V1 must do

1. Join coord-api as the canonical Codex principal and show a read-only fleet
   snapshot before any action. `cosctl status` is the deterministic snapshot
   surface: it reports registered worker states, actionable coord items, wake
   reasons, and a decision digest without mutating state.
   `cosctl preflight` is the separate read-only terminal gate; it must report
   a ready manifest, plan paths, service registration, and edge health before
   any authorized terminal experiment.
   `cosctl roster-proposal` compares that manifest with the live signal state;
   it is diagnostic only, requires explicit re-arm for adoption, and never
   rewrites identities.
2. Read one landed controlling plan and write a current-focus record containing
   the active objective, owner/session, expected report, known gate, and next
   reconciliation action.
3. Select one eligible registered Codex worker, reserve it before delivery, and
   issue one complete bounded assignment with objective, repository/worktree,
   acceptance tests, stopping condition, report destination, and authorization
   limits.
   Live delivery must use the validated `DispatchEnvelope` path; legacy goal
   dispatch is a dry-run/compatibility experiment only.
4. Bind the assignment to the verified logical agent, coord session, CLI
   session, terminal identity, worktree, task/attempt, and supervisor epoch.
5. Record delivery and acknowledgement evidence without creating a numbered
   message for transport ACKs. An injection/exit code is never treated as model
   receipt or completion.
6. Reconcile `idle`, `reserved`, `running`, `needs_input`, `blocked`, `stale`,
   `lost`, and `unknown` states. On uncertainty, stop delivery, preserve the
   reservation, and record a precise blocker or recover only after lease expiry.
7. For an authorized PR lane, verify exact head, run the configured authoritative
   CI, obtain an independent Codex review, invalidate evidence after head
   changes, repair/review again, and merge only at the authorized boundary.
8. Continue health and lease checks during coord/provider failure while issuing
   no new authoritative assignment, merge decision, or terminal action.
9. End each cycle with durable task/result/evidence state and a bounded next
   action. Repeated unchanged blockers are summarized, not endlessly replayed.

### V1 explicitly does not require

- Claude runtime support;
- Escape-based interruption or input-buffer sensing;
- automatic terminal actions from screenshots or an LLM classifier;
- Control Room processes, UI, takeover, or shadow comparison;
- unattended launchd/watchdog installation;
- cross-machine SSH delivery;
- automatic deployment, credential changes, trading, or destructive cleanup.

### V1 acceptance evidence

V1 is useful when the following are demonstrated with Codex only:

- one bounded assignment reaches a registered worker and produces durable result
  evidence;
- a stale or failed delivery is handled without duplicate assignment;
- one exact-head PR completes the CI/review/repair/merge cycle when explicitly
  authorized; and
- lease loss, identity drift, and coord-api loss fail closed.

The enrolled-runtime probe proves session hydration and coord readback. It is a
startup prerequisite, not proof of fleet supervision or terminal delivery.

## V1 shadow extensions: useful experiments, not activation gates

The message-delivery hub and session inbox digest are currently shadow/design
work. They may compare external queueing, precedence, digest summaries,
headless resume, and tab injection, but they must not become a second authority
or silently replace the existing message path.

The current experiment sequence is:

1. **Test 1:** pure queue/receipt/digest projection and durable producer-stopped
   readback; no terminal action.
2. **Test 2:** disposable enrolled-runtime delivery lab; synthetic messages only,
   with observation before any terminal action.
3. **Test 3:** one explicitly authorized low-risk real-work canary.

No test advances merely because a fixture or local harness passes. Each stage
needs its named acceptance evidence and explicit operator authorization.

## V2: later capabilities

These are deliberately deferred until V1 is stable and the shadow experiments
show measurable value:

- shared Claude/Codex delivery profiles and parity testing;
- verified prompt/input-buffer sensing and one-Escape Immediate/Flash delivery;
- durable inbox-digest and response-obligation API extensions in coord-api;
- mailman/clearinghouse retry, supersession, DLQ, and session succession;
- launchd watchdog with bounded poke and same-session headless resume;
- Control Room shadow, successor lease takeover, and rollback to bootstrap;
- multi-machine enrolled adapters and transport comparisons (tab, headless,
  and tmux where applicable);
- model-assisted visual classification, always subordinate to deterministic
  identity, lease, and postcondition checks;
- automatic canaries, merge projection, and broader fleet refill policy.

V2 work must reuse V1 identities and leases. It may not create a parallel COS
database, scheduler, task model, or authority epoch.

## Operating rule

When a request is not in the V1 list, record it as V2 or an experiment before
implementing it. A passing experiment can promote a capability into a future
V1/V2 revision; it does not expand the current authority boundary by itself.
