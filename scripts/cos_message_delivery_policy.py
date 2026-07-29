#!/usr/bin/env python3
"""Pure shadow policy for COS message ordering and delivery obligations.

This module is deliberately below the C2 authority and transport boundary.  It
accepts snapshots shaped like existing coord-api messages plus unnumbered
delivery events, and returns a deterministic proposal.  It owns no durable
state and has no terminal, network, database, or process side effects.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

URGENCY_ORDER = {
    "Normal": 0,
    "Elevated": 1,
    "Urgent": 2,
    "Critical": 3,
}
TERMINAL_STATES = {"cancelled", "superseded", "expired", "closed", "delivery_failed"}
DELIVERY_EVENTS = {"accepted", "routed", "presented", "received", "closed", "delivery_failed"}
FENCED_EVENTS = {"presented", "received", "closed", "delivery_failed", "cancelled"}


class DeliveryPolicyError(ValueError):
    """Raised when a shadow input is structurally unsafe or ambiguous."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def score_language_comprehension(
    experiment: Mapping[str, Any], responses: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Score an anonymized label-comprehension trial without choosing policy.

    The score is deterministic evidence for an operator.  It deliberately does
    not select a winning vocabulary because participant comprehension and
    preference are human judgments.
    """

    if experiment.get("schema") != "cos.message-language-comparison.v1":
        raise DeliveryPolicyError("unsupported language comparison schema")
    candidates = experiment.get("candidates")
    questions = experiment.get("questions")
    if not isinstance(candidates, list) or not candidates:
        raise DeliveryPolicyError("language candidates must be a non-empty list")
    if not isinstance(questions, list) or not questions:
        raise DeliveryPolicyError("language questions must be a non-empty list")
    candidate_labels: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise DeliveryPolicyError("language candidate must be an object")
        candidate_id = str(candidate.get("candidate_id") or "")
        labels = candidate.get("labels")
        if not candidate_id or candidate_id in candidate_labels:
            raise DeliveryPolicyError("language candidate IDs must be unique and non-empty")
        if (
            not isinstance(labels, list)
            or len(labels) != 4
            or len({str(label) for label in labels}) != 4
        ):
            raise DeliveryPolicyError("language candidates require four unique ordered labels")
        candidate_labels[candidate_id] = tuple(str(label) for label in labels)
    expected_ranks: dict[str, int] = {}
    for question in questions:
        if not isinstance(question, Mapping):
            raise DeliveryPolicyError("language question must be an object")
        question_id = str(question.get("question_id") or "")
        expected_rank = question.get("expected_rank")
        if (
            not question_id
            or question_id in expected_ranks
            or isinstance(expected_rank, bool)
            or not isinstance(expected_rank, int)
            or expected_rank not in range(4)
        ):
            raise DeliveryPolicyError("language questions require unique IDs and ranks 0-3")
        expected_ranks[question_id] = expected_rank

    selections: dict[tuple[str, str], str] = {}
    for response in responses:
        if not isinstance(response, Mapping):
            raise DeliveryPolicyError("language response must be an object")
        candidate_id = str(response.get("candidate_id") or "")
        question_id = str(response.get("question_id") or "")
        selected_label = str(response.get("selected_label") or "")
        coordinate = (candidate_id, question_id)
        if candidate_id not in candidate_labels or question_id not in expected_ranks:
            raise DeliveryPolicyError("language response references an unknown coordinate")
        if selected_label not in candidate_labels[candidate_id]:
            raise DeliveryPolicyError("language response selects an unknown label")
        if coordinate in selections:
            raise DeliveryPolicyError("duplicate language response coordinate")
        selections[coordinate] = selected_label

    candidate_scores: list[dict[str, Any]] = []
    for candidate_id, labels in candidate_labels.items():
        correct = 0
        answered = 0
        confusions: list[dict[str, str]] = []
        for question_id, expected_rank in expected_ranks.items():
            selected = selections.get((candidate_id, question_id))
            if selected is None:
                continue
            answered += 1
            expected = labels[expected_rank]
            if selected == expected:
                correct += 1
            else:
                confusions.append(
                    {
                        "question_id": question_id,
                        "expected_label": expected,
                        "selected_label": selected,
                    }
                )
        candidate_scores.append(
            {
                "candidate_id": candidate_id,
                "answered": answered,
                "total": len(expected_ranks),
                "correct": correct,
                "accuracy": correct / len(expected_ranks),
                "confusions": confusions,
            }
        )
    result = {
        "schema": "cos.message-language-comparison-score.v1",
        "experiment_sha256": content_digest(experiment),
        "complete": all(row["answered"] == row["total"] for row in candidate_scores),
        "candidate_scores": candidate_scores,
        "operator_judgment_required": True,
        "preferred_candidate": None,
    }
    result["score_sha256"] = content_digest(result)
    return result


def _timestamp(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise DeliveryPolicyError(f"{field} must be a timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DeliveryPolicyError(f"{field} must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise DeliveryPolicyError(f"{field} is required")


def _message_id(message: dict[str, Any]) -> int:
    value = message.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeliveryPolicyError("message.id must be a positive existing coord-api ID")
    return value


def _policy_map(
    messages: dict[int, dict[str, Any]], policies: Iterable[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for raw in policies:
        if not isinstance(raw, dict):
            raise DeliveryPolicyError("delivery policy rows must be objects")
        message_id = raw.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise DeliveryPolicyError("delivery policy message_id must be an existing integer ID")
        if message_id not in messages:
            raise DeliveryPolicyError(f"delivery policy references unknown message {message_id}")
        if message_id in result:
            raise DeliveryPolicyError(f"duplicate delivery policy for message {message_id}")
        urgency = str(raw.get("urgency") or "Normal")
        if urgency not in URGENCY_ORDER:
            raise DeliveryPolicyError(f"unsupported neutral urgency: {urgency}")
        allowed_dispositions = raw.get("allowed_response_dispositions", [])
        if (
            not isinstance(allowed_dispositions, list)
            or any(
                not isinstance(value, str) or not value.strip() for value in allowed_dispositions
            )
            or len(set(allowed_dispositions)) != len(allowed_dispositions)
        ):
            raise DeliveryPolicyError(
                "allowed_response_dispositions must be a list of unique non-empty strings"
            )
        required_references = raw.get("required_response_references", {})
        if not isinstance(required_references, Mapping) or any(
            not isinstance(name, str)
            or not name.strip()
            or value is None
            or (isinstance(value, str) and not value.strip())
            for name, value in required_references.items()
        ):
            raise DeliveryPolicyError(
                "required_response_references must map non-empty names to expected values"
            )
        requires_response = bool(raw.get("requires_response", False))
        if (allowed_dispositions or required_references) and not requires_response:
            raise DeliveryPolicyError("response constraints require requires_response=true")
        result[message_id] = {
            "message_id": message_id,
            "urgency": urgency,
            "requires_response": requires_response,
            "allowed_response_dispositions": list(allowed_dispositions),
            "required_response_references": dict(required_references),
            "supersedes_message_id": raw.get("supersedes_message_id"),
            "authorized_by": raw.get("authorized_by"),
        }
    for message_id in messages:
        result.setdefault(
            message_id,
            {
                "message_id": message_id,
                "urgency": "Normal",
                "requires_response": False,
                "allowed_response_dispositions": [],
                "required_response_references": {},
                "supersedes_message_id": None,
                "authorized_by": None,
            },
        )
    return result


def _matches_recipient(
    message: dict[str, Any], *, recipient_agent: str, recipient_session_id: str
) -> bool:
    to_agent = str(message.get("to_agent") or "")
    to_session = str(message.get("to_session_id") or "")
    if to_agent and to_agent != recipient_agent:
        return False
    if to_session and to_session != recipient_session_id:
        return False
    return bool(to_agent or to_session)


def _event_sort_key(event: dict[str, Any]) -> tuple[float, str]:
    return (
        _timestamp(event.get("recorded_at"), "event.recorded_at"),
        str(event.get("idempotency_key") or ""),
    )


def _resolve_event_idempotency(
    events: Iterable[dict[str, Any]], violations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop conflicting coordinates and collapse exact replays before mutation."""

    ordered_events = sorted(events, key=_event_sort_key)
    digests_by_key: dict[str, set[str]] = {}
    for event in ordered_events:
        key = str(event.get("idempotency_key") or "")
        if key:
            digests_by_key.setdefault(key, set()).add(content_digest(event))
    conflicting_keys = {
        key for key, event_digests in digests_by_key.items() if len(event_digests) > 1
    }
    violations.extend(
        {"kind": "receipt_idempotency_collision", "key": key} for key in sorted(conflicting_keys)
    )

    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for event in ordered_events:
        key = str(event.get("idempotency_key") or "")
        if key and (key in conflicting_keys or key in seen):
            continue
        if key:
            seen.add(key)
        resolved.append(event)
    return resolved


def _initial_state(message: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": message,
        "policy": policy,
        "status": "queued",
        "presented": False,
        "received": False,
        "response_message_id": None,
        "response_at": None,
        "closed": False,
        "failure_reason": None,
        "terminal_at": None,
        "cancellation_requested_at": None,
        "cancellation_acknowledged": False,
        "expiry_disposition_due": False,
    }


def _validate_supersession(
    states: dict[int, dict[str, Any]],
    events: Iterable[dict[str, Any]],
    violations: list[dict[str, Any]],
) -> None:
    replacements: dict[int, list[int]] = {}
    for new_id, state in states.items():
        old_id = state["policy"].get("supersedes_message_id")
        if old_id is None:
            continue
        if isinstance(old_id, bool) or not isinstance(old_id, int) or old_id not in states:
            violations.append(
                {"kind": "unknown_supersession_target", "message_id": new_id, "target": old_id}
            )
            continue
        replacements.setdefault(old_id, []).append(new_id)
    for old_id, new_ids in replacements.items():
        if len(new_ids) != 1:
            violations.append(
                {"kind": "ambiguous_supersession", "message_id": old_id, "candidates": new_ids}
            )
            continue
        new_id = new_ids[0]
        old = states[old_id]["message"]
        new = states[new_id]["message"]
        authorized_by = states[new_id]["policy"].get("authorized_by")
        if new.get("from_agent") != old.get("from_agent") and authorized_by != "operator":
            violations.append(
                {"kind": "unauthorized_supersession", "message_id": old_id, "candidate": new_id}
            )
            continue
        replacement_at = _timestamp(new.get("created_at"), "message.created_at")
        already_presented = any(
            event.get("message_id") == old_id
            and event.get("event_type") in {"presented", "received"}
            and _timestamp(event.get("recorded_at"), "event.recorded_at") <= replacement_at
            for event in events
        )
        if already_presented:
            violations.append(
                {
                    "kind": "presented_supersession_requires_cancellation",
                    "message_id": old_id,
                    "candidate": new_id,
                }
            )
            continue
        states[old_id]["status"] = "superseded"
        states[old_id]["terminal_at"] = replacement_at


def _valid_response(
    original: dict[str, Any],
    response: dict[str, Any],
    policy: dict[str, Any],
    recipient_agent: str,
    verified_actor_sessions: Mapping[str, set[str]],
    now: float,
) -> bool:
    exact_session = str(original.get("to_session_id") or "")
    producing_session = str(response.get("from_session_id") or "")
    response_references = response.get("response_references")
    required_references = policy["required_response_references"]
    references_match = isinstance(response_references, Mapping) and all(
        response_references.get(name) == expected for name, expected in required_references.items()
    )
    if not required_references:
        references_match = True
    allowed_dispositions = policy["allowed_response_dispositions"]
    return bool(
        response.get("reply_to") == original.get("id")
        and response.get("correlation_id")
        and response.get("correlation_id") == original.get("correlation_id")
        and response.get("from_agent") == recipient_agent
        and str(response.get("content") or "").strip()
        and _timestamp(original.get("created_at"), "message.created_at")
        <= _timestamp(response.get("created_at"), "message.created_at")
        <= now
        and producing_session in verified_actor_sessions.get(recipient_agent, set())
        and (not exact_session or producing_session == exact_session)
        and (
            not allowed_dispositions or response.get("response_disposition") in allowed_dispositions
        )
        and references_match
    )


def _apply_responses(
    states: dict[int, dict[str, Any]],
    recipient_agent: str,
    verified_actor_sessions: Mapping[str, set[str]],
    now: float,
) -> None:
    responses = [state["message"] for state in states.values() if state["message"].get("reply_to")]
    responses.sort(
        key=lambda item: (
            _timestamp(item["created_at"], "message.created_at"),
            item["id"],
        )
    )
    for original_id, state in states.items():
        if not state["policy"]["requires_response"]:
            continue
        for response in responses:
            if _valid_response(
                state["message"],
                response,
                state["policy"],
                recipient_agent,
                verified_actor_sessions,
                now,
            ):
                state["response_message_id"] = response["id"]
                state["response_at"] = _timestamp(response["created_at"], "message.created_at")
                break


def _apply_events(
    states: dict[int, dict[str, Any]],
    events: Iterable[dict[str, Any]],
    *,
    recipient_agent: str,
    recipient_session_id: str,
    live_controller_epoch: int,
    edge_principals: set[str],
    hub_principals: set[str],
    operator_principals: set[str],
    verified_actor_sessions: Mapping[str, set[str]],
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accepted_receipts: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("idempotency_key") or "")
        if not key:
            violations.append({"kind": "receipt_missing_idempotency_key"})
            continue
        if event.get("display_id") or event.get("id"):
            violations.append({"kind": "receipt_has_semantic_message_identity", "key": key})
            continue
        message_id = event.get("message_id")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id not in states
        ):
            violations.append(
                {"kind": "receipt_unknown_message", "key": key, "message_id": message_id}
            )
            continue
        event_type = str(event.get("event_type") or "")
        if event_type not in DELIVERY_EVENTS | {"cancelled"}:
            violations.append(
                {"kind": "unknown_receipt_type", "key": key, "event_type": event_type}
            )
            continue
        epoch = event.get("controller_epoch")
        if event_type in FENCED_EVENTS and epoch != live_controller_epoch:
            violations.append(
                {"kind": "stale_controller_epoch", "key": key, "message_id": message_id}
            )
            continue
        state = states[message_id]
        if state["status"] in TERMINAL_STATES and not (
            state["status"] == "closed" and event_type == "closed"
        ):
            violations.append(
                {
                    "kind": "terminal_state_regression",
                    "key": key,
                    "message_id": message_id,
                    "terminal_state": state["status"],
                }
            )
            continue
        actor = str(event.get("actor_id") or "")
        allowed_actors = {
            "accepted": hub_principals,
            "routed": hub_principals,
            "presented": edge_principals,
            "received": {recipient_agent},
            "closed": hub_principals,
            "delivery_failed": edge_principals,
            "cancelled": {
                str(state["message"].get("from_agent") or ""),
                *operator_principals,
            },
        }[event_type]
        if actor not in allowed_actors:
            violations.append(
                {"kind": "receipt_actor_mismatch", "key": key, "message_id": message_id}
            )
            continue
        actor_session_id = str(event.get("actor_session_id") or "")
        if actor_session_id not in verified_actor_sessions.get(actor, set()):
            violations.append(
                {
                    "kind": "receipt_actor_session_mismatch",
                    "key": key,
                    "message_id": message_id,
                }
            )
            continue
        if event_type in FENCED_EVENTS:
            expected_session = str(state["message"].get("to_session_id") or recipient_session_id)
            if str(event.get("session_id") or "") != expected_session:
                violations.append(
                    {
                        "kind": "receipt_session_mismatch",
                        "key": key,
                        "message_id": message_id,
                    }
                )
                continue
        recorded_at = _timestamp(event.get("recorded_at"), "event.recorded_at")
        if event_type == "presented":
            state["presented"] = True
            if state["status"] != "cancellation_pending":
                state["status"] = "received" if state["received"] else "presented"
        elif event_type == "received":
            if str(event.get("ack_type") or "") == "cancellation":
                requested_at = state["cancellation_requested_at"]
                if requested_at is None or recorded_at < requested_at:
                    violations.append(
                        {
                            "kind": "cancellation_ack_without_pending_request",
                            "key": key,
                            "message_id": message_id,
                        }
                    )
                    continue
                state["cancellation_acknowledged"] = True
                state["status"] = "cancelled"
                state["terminal_at"] = recorded_at
            else:
                state["received"] = True
                if state["status"] != "cancellation_pending":
                    state["status"] = "received"
        elif event_type == "closed":
            ack_due = bool(state["message"].get("required_ack")) and not state["received"]
            response_due = state["policy"]["requires_response"] and (
                not state["response_message_id"] or state["response_at"] > recorded_at
            )
            cancellation_ack_due = state["status"] == "cancellation_pending"
            if ack_due or response_due or cancellation_ack_due:
                violations.append({"kind": "false_closure", "key": key, "message_id": message_id})
                continue
            state["closed"] = True
            state["status"] = "closed"
            state["terminal_at"] = recorded_at
        elif event_type == "cancelled":
            if state["presented"] or state["received"]:
                state["status"] = "cancellation_pending"
                state["cancellation_requested_at"] = recorded_at
            else:
                state["status"] = "cancelled"
                state["terminal_at"] = recorded_at
        elif event_type == "delivery_failed":
            state["status"] = "delivery_failed"
            state["failure_reason"] = str(event.get("reason") or "unspecified")
            state["terminal_at"] = recorded_at
        accepted_receipts.append(dict(event))
    return accepted_receipts


def _expire(states: dict[int, dict[str, Any]], now: float) -> None:
    for state in states.values():
        if state["status"] in TERMINAL_STATES:
            continue
        message = state["message"]
        ttl = message.get("ttl_seconds", 24 * 60 * 60)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise DeliveryPolicyError("message.ttl_seconds must be a positive integer")
        if _timestamp(message["created_at"], "message.created_at") + ttl <= now:
            if state["presented"] or state["received"] or state["status"] == "cancellation_pending":
                state["expiry_disposition_due"] = True
            else:
                state["status"] = "expired"
                state["terminal_at"] = now


def _action(urgency: str, worker_state: str) -> str:
    if worker_state == "idle":
        return "PRESENT"
    if worker_state == "running" and urgency == "Critical":
        return "INTERRUPT"
    if worker_state == "running" and urgency == "Urgent":
        return "STEER"
    return "HOLD"


def project_delivery(
    *,
    messages: Iterable[dict[str, Any]],
    policies: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    recipient_agent: str,
    recipient_session_id: str,
    worker_state: str,
    now: object,
    live_controller_epoch: int,
    edge_principals: Iterable[str] = ("delivery-edge",),
    hub_principals: Iterable[str] = ("delivery-hub",),
    operator_principals: Iterable[str] = ("operator",),
    verified_actor_sessions: Mapping[str, Iterable[str]] | None = None,
    digest_limit: int = 20,
) -> dict[str, Any]:
    """Return a deterministic shadow proposal without performing delivery."""

    if worker_state not in {
        "idle",
        "reserved",
        "running",
        "needs_input",
        "blocked",
        "stale",
        "lost",
        "unknown",
    }:
        raise DeliveryPolicyError(f"unsupported normalized worker state: {worker_state}")
    if isinstance(live_controller_epoch, bool) or not isinstance(live_controller_epoch, int):
        raise DeliveryPolicyError("live_controller_epoch must be an integer")
    if digest_limit < 1:
        raise DeliveryPolicyError("digest_limit must be positive")
    edge_principal_set = set(edge_principals)
    hub_principal_set = set(hub_principals)
    operator_principal_set = set(operator_principals)
    if not edge_principal_set or not hub_principal_set:
        raise DeliveryPolicyError("edge_principals and hub_principals must be non-empty")
    if not isinstance(verified_actor_sessions, Mapping) or not verified_actor_sessions:
        raise DeliveryPolicyError("verified_actor_sessions must be a non-empty mapping")
    verified_actor_session_sets: dict[str, set[str]] = {}
    for actor, sessions in verified_actor_sessions.items():
        actor_id = str(actor or "")
        session_set = {str(session or "") for session in sessions}
        if not actor_id or not session_set or "" in session_set:
            raise DeliveryPolicyError(
                "verified_actor_sessions requires non-empty actor and session identities"
            )
        verified_actor_session_sets[actor_id] = session_set

    message_rows: dict[int, dict[str, Any]] = {}
    for raw in messages:
        if not isinstance(raw, dict):
            raise DeliveryPolicyError("message snapshots must be objects")
        row = dict(raw)
        message_id = _message_id(row)
        if message_id in message_rows:
            raise DeliveryPolicyError(f"duplicate coord-api message ID: {message_id}")
        _timestamp(row.get("created_at"), "message.created_at")
        if not row.get("display_id"):
            raise DeliveryPolicyError("message.display_id is required")
        message_rows[message_id] = row

    policy_rows = _policy_map(message_rows, policies)
    event_rows = [dict(event) for event in events]
    states = {
        message_id: _initial_state(message, policy_rows[message_id])
        for message_id, message in message_rows.items()
    }
    violations: list[dict[str, Any]] = []
    resolved_event_rows = _resolve_event_idempotency(event_rows, violations)
    now_ts = _timestamp(now, "now")
    _validate_supersession(states, resolved_event_rows, violations)
    _apply_responses(states, recipient_agent, verified_actor_session_sets, now_ts)
    accepted_receipts = _apply_events(
        states,
        resolved_event_rows,
        recipient_agent=recipient_agent,
        recipient_session_id=recipient_session_id,
        live_controller_epoch=live_controller_epoch,
        edge_principals=edge_principal_set,
        hub_principals=hub_principal_set,
        operator_principals=operator_principal_set,
        verified_actor_sessions=verified_actor_session_sets,
        violations=violations,
    )
    _expire(states, now_ts)

    matching = [
        state
        for state in states.values()
        if _matches_recipient(
            state["message"],
            recipient_agent=recipient_agent,
            recipient_session_id=recipient_session_id,
        )
    ]
    queued = [
        state
        for state in matching
        if state["status"] not in TERMINAL_STATES and not state["presented"]
    ]
    queued.sort(
        key=lambda state: (
            -URGENCY_ORDER[state["policy"]["urgency"]],
            _timestamp(state["message"]["created_at"], "message.created_at"),
            state["message"]["id"],
        )
    )
    headers = [
        {
            "message_id": state["message"]["id"],
            "display_id": state["message"]["display_id"],
            "subject": state["message"].get("subject") or "",
            "urgency": state["policy"]["urgency"],
            "task_id": state["message"].get("task_id"),
            "required_ack": bool(state["message"].get("required_ack")),
            "requires_response": state["policy"]["requires_response"],
        }
        for state in queued[:digest_limit]
    ]
    obligations = [
        {
            "message_id": state["message"]["id"],
            "display_id": state["message"]["display_id"],
            "ack_due": bool(state["message"].get("required_ack")) and not state["received"],
            "response_due": state["policy"]["requires_response"]
            and not state["response_message_id"],
            "response_message_id": state["response_message_id"],
            "cancellation_ack_due": state["status"] == "cancellation_pending",
            "expiry_disposition_due": state["expiry_disposition_due"],
        }
        for state in matching
        if state["status"] not in TERMINAL_STATES
        and (
            (bool(state["message"].get("required_ack")) and not state["received"])
            or (state["policy"]["requires_response"] and not state["response_message_id"])
            or state["status"] == "cancellation_pending"
            or state["expiry_disposition_due"]
        )
    ]
    dlq = [
        {
            "message_id": state["message"]["id"],
            "display_id": state["message"]["display_id"],
            "reason": state["failure_reason"],
        }
        for state in matching
        if state["status"] == "delivery_failed"
    ]
    top = queued[0] if queued else None
    proposed_action = {
        "action": "HOLD" if top is None else _action(top["policy"]["urgency"], worker_state),
        "message_id": None if top is None else top["message"]["id"],
        "reason": "queue empty"
        if top is None
        else f"{top['policy']['urgency']} message while worker is {worker_state}",
    }
    digest = {
        "recipient_agent": recipient_agent,
        "recipient_session_id": recipient_session_id,
        "worker_state": worker_state,
        "queued_count": len(queued),
        "headers": headers,
        "obligations": obligations,
        "assigned_task_ids": sorted(
            {header["task_id"] for header in headers if header.get("task_id")}
        ),
    }
    serialized_digest = canonical_json(digest)
    leaked = [
        state["message"]["id"]
        for state in matching
        if state["message"].get("content") and str(state["message"]["content"]) in serialized_digest
    ]
    if leaked:
        violations.append({"kind": "digest_body_leak", "message_ids": leaked})

    control_queue = [
        state
        for state in matching
        if state["status"] not in TERMINAL_STATES and not state["presented"]
    ]
    control_queue.sort(
        key=lambda state: (
            _timestamp(state["message"]["created_at"], "message.created_at"),
            state["message"]["id"],
        )
    )
    control_ids = [state["message"]["id"] for state in control_queue]
    control_id = control_ids[0] if control_ids else None
    control_action = {
        "action": "HOLD" if control_id is None else "PRESENT",
        "message_id": control_id,
        "reason": "queue empty" if control_id is None else "legacy immediate FIFO",
    }
    treatment_id = proposed_action["message_id"] if proposed_action["action"] != "HOLD" else None
    policy_reasons: list[str] = []
    if control_id != treatment_id:
        policy_reasons.append("urgency_ordering")
    if control_action["action"] != proposed_action["action"]:
        policy_reasons.append("normalized_worker_state_gate")
    divergence = {
        "control_would_present": control_ids,
        "control_proposed_action": control_action,
        "treatment_proposed_action": proposed_action,
        "diverged": control_action != proposed_action,
        "policy_reasons": policy_reasons,
    }
    divergence["evidence_sha256"] = content_digest(divergence)
    projection_state = {
        str(message_id): {
            "status": state["status"],
            "presented": state["presented"],
            "received": state["received"],
            "response_message_id": state["response_message_id"],
            "failure_reason": state["failure_reason"],
            "cancellation_acknowledged": state["cancellation_acknowledged"],
            "expiry_disposition_due": state["expiry_disposition_due"],
        }
        for message_id, state in sorted(states.items())
    }
    input_bundle = {
        "messages": sorted(message_rows.values(), key=lambda row: row["id"]),
        "policies": sorted(policy_rows.values(), key=lambda row: row["message_id"]),
        "events": sorted(event_rows, key=_event_sort_key),
        "recipient_agent": recipient_agent,
        "recipient_session_id": recipient_session_id,
        "worker_state": worker_state,
        "now": now_ts,
        "live_controller_epoch": live_controller_epoch,
        "edge_principals": sorted(edge_principal_set),
        "hub_principals": sorted(hub_principal_set),
        "operator_principals": sorted(operator_principal_set),
        "verified_actor_sessions": {
            actor: sorted(sessions)
            for actor, sessions in sorted(verified_actor_session_sets.items())
        },
    }
    result = {
        "schema": "cos.message-delivery-shadow.v1",
        "fixture_sha256": content_digest(input_bundle),
        "digest": digest,
        "digest_sha256": content_digest(digest),
        "proposed_action": proposed_action,
        "receipt_projection": accepted_receipts,
        "response_due": obligations,
        "dlq_projection": dlq,
        "control_comparison": divergence,
        "state": projection_state,
        "violations": violations,
    }
    result["projection_sha256"] = content_digest(result)
    return result
