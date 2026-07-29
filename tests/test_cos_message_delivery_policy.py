from __future__ import annotations

import ast
import copy
import itertools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "message_delivery_shadow_v1.json"
LANGUAGE_FIXTURE_PATH = (
    ROOT / "tests" / "fixtures" / "message_delivery_language_comparison_v1.json"
)
sys.path.insert(0, str(SCRIPTS))

import cos_message_delivery_policy as policy  # noqa: E402


def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def project(value: dict, **overrides):
    events = copy.deepcopy(value["events"])
    verified_actor_sessions = value["verified_actor_sessions"]
    for event in events:
        if "actor_session_id" not in event:
            sessions = verified_actor_sessions.get(event.get("actor_id"), [])
            event["actor_session_id"] = sessions[0] if sessions else "unverified-session"
    arguments = {
        "messages": value["messages"],
        "policies": value["policies"],
        "events": events,
        "recipient_agent": value["recipient_agent"],
        "recipient_session_id": value["recipient_session_id"],
        "worker_state": value["worker_state"],
        "now": value["now"],
        "live_controller_epoch": value["live_controller_epoch"],
        "verified_actor_sessions": verified_actor_sessions,
    }
    arguments.update(overrides)
    return policy.project_delivery(**arguments)


def test_canonical_fixture_covers_ordering_obligations_and_failure_projection():
    value = fixture()
    result = project(value)

    assert len(value["messages"]) == 24
    assert result["violations"] == []
    assert result["proposed_action"] == {
        "action": "INTERRUPT",
        "message_id": 16,
        "reason": "Critical message while worker is running",
    }
    header_ids = [row["message_id"] for row in result["digest"]["headers"]]
    assert header_ids[:4] == [16, 3, 7, 11]
    assert 2 not in header_ids  # superseded by existing message 21
    assert 4 not in header_ids  # closed after recipient receipt
    assert 8 not in header_ids  # authorized cancellation after presentation
    assert 12 not in header_ids  # visible delivery failure, not silent loss
    assert 18 not in header_ids  # TTL expired under the virtual clock
    assert 20 not in header_ids  # exact-session traffic stays on the old session
    assert result["dlq_projection"] == [
        {"message_id": 12, "display_id": "M-00012", "reason": "bounded retry exhausted"}
    ]


def test_valid_correlated_response_closes_response_due_but_negative_responses_do_not():
    result = project(fixture())
    obligations = {row["message_id"]: row for row in result["response_due"]}

    assert obligations[3]["response_due"] is False
    assert obligations[3]["ack_due"] is True
    assert result["state"]["3"]["response_message_id"] == 24
    assert obligations[11]["response_due"] is True
    assert obligations[11]["response_message_id"] is None


def test_receipts_are_unnumbered_idempotent_events_not_semantic_messages():
    value = fixture()
    result = project(value)

    assert len(result["receipt_projection"]) == 6
    assert all("display_id" not in event and "id" not in event for event in value["events"])
    assert len(value["messages"]) == 24

    mutation = copy.deepcopy(value)
    mutation["events"].append(
        {
            "id": 25001,
            "display_id": "M-25001",
            "event_type": "received",
            "message_id": 6,
            "actor_id": "worker",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:6:received:numbered-mutation",
        }
    )
    kinds = {row["kind"] for row in project(mutation)["violations"]}
    assert "receipt_has_semantic_message_identity" in kinds


def test_reordered_input_events_reconstruct_identical_projection():
    value = fixture()
    forward = project(value)
    reverse = project(value, events=list(reversed(value["events"])))

    assert reverse["fixture_sha256"] == forward["fixture_sha256"]
    assert reverse["projection_sha256"] == forward["projection_sha256"]
    assert reverse["state"] == forward["state"]


def test_control_treatment_divergence_is_exact_and_policy_explained():
    result = project(fixture())
    comparison = result["control_comparison"]

    assert comparison["control_proposed_action"] == {
        "action": "PRESENT",
        "message_id": 1,
        "reason": "legacy immediate FIFO",
    }
    assert comparison["treatment_proposed_action"] == result["proposed_action"]
    assert comparison["diverged"] is True
    assert comparison["policy_reasons"] == [
        "urgency_ordering",
        "normalized_worker_state_gate",
    ]
    evidence = dict(comparison)
    digest = evidence.pop("evidence_sha256")
    assert digest == policy.content_digest(evidence)


def test_event_generator_has_the_same_canonical_evidence_as_a_list():
    value = fixture()
    listed = project(value)
    streamed = project(value, events=(event for event in value["events"]))

    assert streamed["fixture_sha256"] == listed["fixture_sha256"]
    assert streamed["projection_sha256"] == listed["projection_sha256"]


def test_principal_generators_are_materialized_once_for_policy_and_evidence():
    value = fixture()
    listed = project(value)
    streamed = project(
        value,
        edge_principals=(item for item in ["delivery-edge"]),
        hub_principals=(item for item in ["delivery-hub"]),
    )

    assert streamed["fixture_sha256"] == listed["fixture_sha256"]
    assert streamed["projection_sha256"] == listed["projection_sha256"]


@pytest.mark.parametrize(
    ("worker_state", "urgency", "expected"),
    [
        ("idle", "Normal", "PRESENT"),
        ("idle", "Elevated", "PRESENT"),
        ("idle", "Urgent", "PRESENT"),
        ("idle", "Critical", "PRESENT"),
        ("running", "Normal", "HOLD"),
        ("running", "Elevated", "HOLD"),
        ("running", "Urgent", "STEER"),
        ("running", "Critical", "INTERRUPT"),
        ("needs_input", "Critical", "HOLD"),
        ("stale", "Critical", "HOLD"),
        ("lost", "Critical", "HOLD"),
        ("unknown", "Critical", "HOLD"),
    ],
)
def test_bounded_worker_state_and_urgency_partition(worker_state, urgency, expected):
    message = {
        "id": 101,
        "display_id": "M-00101",
        "from_agent": "planner",
        "to_agent": "worker",
        "to_session_id": None,
        "subject": "partition",
        "content": "not in digest",
        "created_at": "2026-07-29T12:00:00Z",
        "ttl_seconds": 3600,
        "required_ack": False,
        "correlation_id": "partition",
        "reply_to": None,
    }
    result = policy.project_delivery(
        messages=[message],
        policies=[{"message_id": 101, "urgency": urgency}],
        events=[],
        recipient_agent="worker",
        recipient_session_id="session-current",
        worker_state=worker_state,
            now="2026-07-29T12:01:00Z",
            live_controller_epoch=7,
            verified_actor_sessions={"worker": ["session-current"]},
    )

    assert result["proposed_action"]["action"] == expected


def test_stale_epoch_and_terminal_regression_fail_closed():
    value = fixture()
    mutation = copy.deepcopy(value)
    mutation["events"].extend(
        [
            {
                "event_type": "presented",
                "message_id": 16,
                "actor_id": "delivery-edge",
                "session_id": "session-current",
                "controller_epoch": 6,
                "recorded_at": "2026-07-29T12:04:00Z",
                "idempotency_key": "receipt:16:stale",
            },
            {
                "event_type": "received",
                "message_id": 12,
                "actor_id": "worker",
                "session_id": "session-current",
                "controller_epoch": 7,
                "recorded_at": "2026-07-29T12:04:01Z",
                "idempotency_key": "receipt:12:regression",
            },
        ]
    )

    result = project(mutation)
    kinds = {row["kind"] for row in result["violations"]}
    assert {"stale_controller_epoch", "terminal_state_regression"} <= kinds
    assert result["state"]["16"]["presented"] is False
    assert result["state"]["12"]["status"] == "delivery_failed"


@pytest.mark.parametrize(
    ("event_type", "actor_id"),
    [
        ("presented", "worker"),
        ("received", "delivery-edge"),
        ("closed", "worker"),
        ("delivery_failed", "delivery-hub"),
    ],
)
def test_fenced_receipt_from_wrong_actor_cannot_mutate_state(event_type, actor_id):
    value = fixture()
    value["events"] = [
        {
            "event_type": event_type,
            "message_id": 16,
            "actor_id": actor_id,
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": f"receipt:16:{event_type}:wrong-actor",
        }
    ]

    result = project(value)
    assert any(row["kind"] == "receipt_actor_mismatch" for row in result["violations"])
    assert result["state"]["16"]["status"] == "queued"


def test_fenced_receipt_for_stale_session_cannot_mutate_current_delivery():
    value = fixture()
    value["events"] = [
        {
            "event_type": "received",
            "message_id": 16,
            "actor_id": "worker",
            "session_id": "session-old",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:16:received:stale-session",
        }
    ]

    result = project(value)
    assert any(row["kind"] == "receipt_session_mismatch" for row in result["violations"])
    assert result["state"]["16"]["received"] is False


def test_allowed_actor_with_unverified_producing_session_cannot_mutate_state():
    value = fixture()
    value["events"] = [
        {
            "event_type": "received",
            "message_id": 16,
            "actor_id": "worker",
            "actor_session_id": "session-old",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:16:received:foreign-actor-session",
        }
    ]

    result = project(value)
    assert any(
        row["kind"] == "receipt_actor_session_mismatch"
        for row in result["violations"]
    )
    assert result["state"]["16"]["received"] is False


def test_received_does_not_fabricate_terminal_presentation_evidence():
    value = fixture()
    value["events"] = [
        {
            "event_type": "received",
            "message_id": 16,
            "actor_id": "worker",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:16:received:without-presented",
        }
    ]

    result = project(value)
    assert result["state"]["16"]["received"] is True
    assert result["state"]["16"]["presented"] is False


def test_invalid_receipt_cannot_poison_later_valid_same_key():
    value = fixture()
    key = "receipt:16:received:poison-attempt"
    value["events"] = [
        {
            "event_type": "received",
            "message_id": 16,
            "actor_id": "worker",
            "session_id": "session-old",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": key,
        },
        {
            "event_type": "received",
            "message_id": 16,
            "actor_id": "worker",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:01Z",
            "idempotency_key": key,
        },
    ]

    result = project(value)
    assert result["state"]["16"]["received"] is True
    assert len(result["receipt_projection"]) == 1


def test_same_idempotency_key_with_different_payload_is_rejected():
    value = fixture()
    duplicate = copy.deepcopy(value["events"][0])
    duplicate["actor_id"] = "different-edge"
    value["events"].append(duplicate)

    result = project(value)
    assert any(
        row["kind"] == "receipt_idempotency_collision" for row in result["violations"]
    )


def test_same_idempotency_key_with_changed_time_conflicts_without_poisoning_replay():
    value = fixture()
    original = copy.deepcopy(value["events"][0])
    changed_time = copy.deepcopy(original)
    changed_time["recorded_at"] = "2026-07-29T12:01:30Z"
    exact_replay = copy.deepcopy(original)
    value["events"] = [original, changed_time, exact_replay]

    result = project(value)

    assert result["receipt_projection"] == [original]
    assert result["state"]["4"]["presented"] is True
    assert [row["kind"] for row in result["violations"]] == [
        "receipt_idempotency_collision"
    ]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("reply_to", 11),
        ("correlation_id", "wrong-correlation"),
        ("from_agent", "different-worker"),
        ("content", "   "),
    ],
)
def test_malformed_or_empty_response_does_not_discharge_obligation(field, replacement):
    value = fixture()
    value["messages"][23][field] = replacement

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)
    assert obligation["response_due"] is True
    assert obligation["response_message_id"] is None


def test_response_created_before_original_does_not_discharge_obligation():
    value = fixture()
    response = next(row for row in value["messages"] if row["id"] == 24)
    response["created_at"] = "2026-07-29T12:00:02Z"

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)

    assert obligation["response_due"] is True
    assert obligation["response_message_id"] is None


def test_agent_scoped_response_requires_verified_producing_session():
    value = fixture()
    response = next(row for row in value["messages"] if row["id"] == 24)
    response["from_session_id"] = "unverified-worker-session"

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)

    assert obligation["response_due"] is True
    assert obligation["response_message_id"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"response_disposition": None},
        {"response_disposition": "deferred"},
        {"response_references": {}},
        {"response_references": {"task_id": "T-wrong", "result_id": "R-003"}},
    ],
)
def test_explicit_response_disposition_and_references_fail_closed(mutation):
    value = fixture()
    response_policy = next(row for row in value["policies"] if row["message_id"] == 3)
    response_policy.update(
        {
            "allowed_response_dispositions": ["completed", "blocked"],
            "required_response_references": {
                "task_id": "T-003",
                "result_id": "R-003",
            },
        }
    )
    response = next(row for row in value["messages"] if row["id"] == 24)
    response.update(
        {
            "response_disposition": "completed",
            "response_references": {"task_id": "T-003", "result_id": "R-003"},
        }
    )
    response.update(mutation)

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)

    assert obligation["response_due"] is True
    assert obligation["response_message_id"] is None


def test_explicit_response_disposition_and_references_accept_exact_match():
    value = fixture()
    response_policy = next(row for row in value["policies"] if row["message_id"] == 3)
    response_policy.update(
        {
            "allowed_response_dispositions": ["completed", "blocked"],
            "required_response_references": {
                "task_id": "T-003",
                "result_id": "R-003",
            },
        }
    )
    response = next(row for row in value["messages"] if row["id"] == 24)
    response.update(
        {
            "response_disposition": "completed",
            "response_references": {"task_id": "T-003", "result_id": "R-003"},
        }
    )

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)

    assert obligation["response_due"] is False
    assert obligation["response_message_id"] == 24


@pytest.mark.parametrize(
    "policy_update",
    [
        {"allowed_response_dispositions": "completed"},
        {"allowed_response_dispositions": ["completed", "completed"]},
        {"required_response_references": []},
        {"required_response_references": {"": "R-003"}},
        {
            "requires_response": False,
            "allowed_response_dispositions": ["completed"],
        },
    ],
)
def test_invalid_explicit_response_policy_is_rejected(policy_update):
    value = fixture()
    response_policy = next(row for row in value["policies"] if row["message_id"] == 3)
    response_policy.update(policy_update)

    with pytest.raises(policy.DeliveryPolicyError):
        project(value)


def test_reply_to_superseded_predecessor_does_not_close_replacement():
    value = fixture()
    replacement = next(row for row in value["policies"] if row["message_id"] == 21)
    replacement["requires_response"] = True
    predecessor = next(row for row in value["messages"] if row["id"] == 2)
    response = next(row for row in value["messages"] if row["id"] == 24)
    response["reply_to"] = predecessor["id"]
    response["correlation_id"] = predecessor["correlation_id"]

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 21)
    assert obligation["response_due"] is True
    assert obligation["response_message_id"] is None


def test_exact_session_response_requires_authenticated_producing_session():
    value = fixture()
    exact = next(row for row in value["messages"] if row["id"] == 19)
    exact["required_ack"] = False
    exact_policy = next(row for row in value["policies"] if row["message_id"] == 19)
    exact_policy["requires_response"] = True
    response = next(row for row in value["messages"] if row["id"] == 24)
    response.update(
        {
            "reply_to": 19,
            "correlation_id": exact["correlation_id"],
            "from_session_id": "session-old",
        }
    )

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 19)
    assert obligation["response_due"] is True


def test_response_subject_is_metadata_not_an_authority_coordinate():
    value = fixture()
    response = next(row for row in value["messages"] if row["id"] == 24)
    response["subject"] = "unrelated presentation text"

    result = project(value)
    obligation = next(row for row in result["response_due"] if row["message_id"] == 3)
    assert obligation["response_due"] is False
    assert obligation["response_message_id"] == 24


@pytest.mark.parametrize("limit", [1, 5, 50])
def test_digest_limit_is_exact_and_does_not_change_queue_state(limit):
    value = fixture()
    result = project(value, digest_limit=limit)

    assert len(result["digest"]["headers"]) == min(
        limit, result["digest"]["queued_count"]
    )
    assert result["digest"]["queued_count"] > 5


def test_equal_timestamp_fifo_uses_existing_message_id_as_tie_breaker():
    value = fixture()
    for message in value["messages"]:
        if message["id"] in {1, 5}:
            message["created_at"] = "2026-07-29T12:00:00Z"
    for row in value["policies"]:
        if row["message_id"] in {1, 5}:
            row["urgency"] = "Normal"

    result = project(value, worker_state="idle")
    header_ids = [row["message_id"] for row in result["digest"]["headers"]]
    assert header_ids.index(1) < header_ids.index(5)


def test_false_closure_does_not_discharge_response_obligation():
    value = fixture()
    mutation = copy.deepcopy(value)
    mutation["events"].append(
        {
            "event_type": "closed",
            "message_id": 11,
            "actor_id": "delivery-hub",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:11:false-close",
        }
    )

    result = project(mutation)
    assert any(row["kind"] == "false_closure" for row in result["violations"])
    assert result["state"]["11"]["status"] == "queued"
    assert any(row["message_id"] == 11 and row["response_due"] for row in result["response_due"])


def test_presented_cancellation_stays_pending_until_distinct_recipient_ack():
    value = fixture()
    value["events"] = [
        {
            "event_type": "presented",
            "message_id": 8,
            "actor_id": "delivery-edge",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:02:00Z",
            "idempotency_key": "receipt:8:presented:cancel-test",
        },
        {
            "event_type": "cancelled",
            "message_id": 8,
            "actor_id": "planner",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:02:01Z",
            "idempotency_key": "receipt:8:cancelled:cancel-test",
        },
        {
            "event_type": "received",
            "message_id": 8,
            "actor_id": "worker",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:02:02Z",
            "idempotency_key": "receipt:8:received:ordinary",
        },
    ]

    pending = project(value)
    assert pending["state"]["8"]["status"] == "cancellation_pending"
    obligation = next(row for row in pending["response_due"] if row["message_id"] == 8)
    assert obligation["cancellation_ack_due"] is True

    value["events"].append(
        {
            "event_type": "received",
            "message_id": 8,
            "actor_id": "worker",
            "session_id": "session-current",
            "ack_type": "cancellation",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:02:03Z",
            "idempotency_key": "receipt:8:received:cancellation",
        }
    )
    cancelled = project(value)
    assert cancelled["state"]["8"]["status"] == "cancelled"
    assert cancelled["state"]["8"]["cancellation_acknowledged"] is True


def test_presented_message_cannot_be_auto_superseded_or_expired():
    value = fixture()
    value["events"] = [
        {
            "event_type": "presented",
            "message_id": 2,
            "actor_id": "delivery-edge",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:00:20Z",
            "idempotency_key": "receipt:2:presented:before-replacement",
        },
        {
            "event_type": "presented",
            "message_id": 18,
            "actor_id": "delivery-edge",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:01:00Z",
            "idempotency_key": "receipt:18:presented:before-expiry",
        },
    ]

    result = project(value)
    assert any(
        row["kind"] == "presented_supersession_requires_cancellation"
        for row in result["violations"]
    )
    assert result["state"]["2"]["status"] == "presented"
    assert result["state"]["18"]["status"] == "presented"
    assert result["state"]["18"]["expiry_disposition_due"] is True


def test_bounded_three_event_lifecycle_matches_independent_oracle():
    event_names = ("presented", "received", "cancel", "cancel_ack", "close", "fail")

    for sequence in itertools.product(event_names, repeat=3):
        value = fixture()
        value["messages"] = [copy.deepcopy(value["messages"][15])]
        value["policies"] = [
            {"message_id": 16, "urgency": "Critical", "requires_response": False}
        ]
        value["events"] = []
        status = "queued"
        presented = False
        received = False
        cancellation_acknowledged = False
        for index, name in enumerate(sequence):
            if name in {"presented", "fail"}:
                actor = "delivery-edge"
            elif name in {"received", "cancel_ack"}:
                actor = "worker"
            elif name == "cancel":
                actor = "planner"
            else:
                actor = "delivery-hub"
            event = {
                "event_type": {
                    "cancel": "cancelled",
                    "cancel_ack": "received",
                    "close": "closed",
                    "fail": "delivery_failed",
                }.get(name, name),
                "message_id": 16,
                "actor_id": actor,
                "session_id": "session-current",
                "controller_epoch": 7,
                "recorded_at": f"2026-07-29T12:04:0{index}Z",
                "idempotency_key": f"bounded:{index}:{name}",
            }
            if name == "cancel_ack":
                event["ack_type"] = "cancellation"
            value["events"].append(event)

            terminal = status in policy.TERMINAL_STATES
            if terminal:
                continue
            if name == "presented":
                presented = True
                if status != "cancellation_pending":
                    status = "received" if received else "presented"
            elif name == "received":
                received = True
                if status != "cancellation_pending":
                    status = "received"
            elif name == "cancel":
                status = "cancellation_pending" if presented or received else "cancelled"
            elif name == "cancel_ack":
                if status == "cancellation_pending":
                    status = "cancelled"
                    cancellation_acknowledged = True
            elif name == "close":
                if received and status != "cancellation_pending":
                    status = "closed"
            elif name == "fail":
                status = "delivery_failed"

        result = project(value, now="2026-07-29T12:04:10Z")
        projected = result["state"]["16"]
        assert projected["status"] == status, sequence
        assert projected["presented"] is presented, sequence
        assert projected["received"] is received, sequence
        assert projected["cancellation_acknowledged"] is cancellation_acknowledged, sequence


def test_late_response_cannot_retroactively_authorize_an_earlier_close():
    value = fixture()
    mutation = copy.deepcopy(value)
    mutation["messages"][2]["required_ack"] = False
    mutation["messages"][23]["created_at"] = "2026-07-29T12:05:00Z"
    mutation["events"].append(
        {
            "event_type": "closed",
            "message_id": 3,
            "actor_id": "delivery-hub",
            "session_id": "session-current",
            "controller_epoch": 7,
            "recorded_at": "2026-07-29T12:04:00Z",
            "idempotency_key": "receipt:3:premature-close",
        }
    )

    result = project(mutation)
    assert any(row["kind"] == "false_closure" for row in result["violations"])
    assert result["state"]["3"]["status"] == "queued"
    assert result["state"]["3"]["response_message_id"] == 24


def test_unauthorized_or_ambiguous_supersession_fails_closed():
    value = fixture()
    unauthorized = copy.deepcopy(value)
    for row in unauthorized["policies"]:
        if row["message_id"] == 22:
            row["supersedes_message_id"] = 1
    result = project(unauthorized)
    assert any(row["kind"] == "unauthorized_supersession" for row in result["violations"])
    assert result["state"]["1"]["status"] == "queued"

    ambiguous = copy.deepcopy(value)
    for row in ambiguous["policies"]:
        if row["message_id"] in {5, 9}:
            row["supersedes_message_id"] = 1
    result = project(ambiguous)
    assert any(row["kind"] == "ambiguous_supersession" for row in result["violations"])
    assert result["state"]["1"]["status"] == "queued"


def test_digest_body_leak_mutation_turns_red():
    value = fixture()
    mutation = copy.deepcopy(value)
    mutation["messages"][0]["subject"] = mutation["messages"][0]["content"]

    result = project(mutation)
    assert any(row["kind"] == "digest_body_leak" for row in result["violations"])


def test_outputs_reuse_existing_identity_coordinates_only():
    result = project(fixture())
    forbidden_keys = {
        "obligation_id",
        "delivery_generation",
        "binding_generation",
        "delivery_attempt_id",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert forbidden_keys.isdisjoint(set(keys(result)))
    assert all("message_id" in row for row in result["digest"]["headers"])
    assert all("message_id" in row for row in result["response_due"])


def test_shadow_module_has_no_authority_or_transport_dependencies():
    source = (SCRIPTS / "cos_message_delivery_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "c2_coord_client",
        "cos_iterm_edge_client",
        "cos_tab_dispatch",
        "iterm2",
        "socket",
        "subprocess",
        "sqlite3",
        "psycopg2",
        "requests",
        "urllib",
    }
    assert imported.isdisjoint(forbidden)
    assert "open(" not in source
    assert "write_text" not in source


def test_fixture_has_stable_cross_tier_identity():
    value = fixture()
    assert value["schema"] == "cos.message-delivery-shadow.fixture.v1"
    assert policy.content_digest(value) == (
        "639d66f9363e1211d876ab4862e104c06692309cceaed3985573aef5f7097a6f"
    )


def test_language_comparison_fixture_has_stable_identity():
    experiment = json.loads(LANGUAGE_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert policy.content_digest(experiment) == (
        "97b68352ce363ecd2ddce2484855627163919586f532e439911af06ce61518db"
    )


def test_language_comparison_scores_answers_but_does_not_choose_for_operator():
    experiment = json.loads(LANGUAGE_FIXTURE_PATH.read_text(encoding="utf-8"))
    responses = []
    for candidate in experiment["candidates"]:
        for question in experiment["questions"]:
            responses.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "question_id": question["question_id"],
                    "selected_label": candidate["labels"][question["expected_rank"]],
                }
            )

    score = policy.score_language_comprehension(experiment, responses)
    assert score["complete"] is True
    assert [row["accuracy"] for row in score["candidate_scores"]] == [1.0, 1.0]
    assert score["operator_judgment_required"] is True
    assert score["preferred_candidate"] is None


def test_language_comparison_exposes_confusion_and_incomplete_trials():
    experiment = json.loads(LANGUAGE_FIXTURE_PATH.read_text(encoding="utf-8"))
    score = policy.score_language_comprehension(
        experiment,
        [
            {
                "candidate_id": "B",
                "question_id": "idle-ordering",
                "selected_label": "Routine",
            }
        ],
    )

    assert score["complete"] is False
    candidate_b = next(
        row for row in score["candidate_scores"] if row["candidate_id"] == "B"
    )
    assert candidate_b["accuracy"] == 0.0
    assert candidate_b["confusions"] == [
        {
            "question_id": "idle-ordering",
            "expected_label": "Priority",
            "selected_label": "Routine",
        }
    ]
