from __future__ import annotations

from typing import Any, Mapping


def resolve_sent_reply_completion(
    *,
    state: Mapping[str, Any] | None,
    visible_text: str,
    delivery_partial: bool = False,
    delivery_unknown: bool = False,
) -> dict[str, Any]:
    """Resolve the final Trace outcome after a reply dispatch path finishes.

    Tool execution, evidence delivery and outbound confirmation are deliberately
    independent. A tool returning data cannot by itself make the whole turn
    successful.
    """

    values = state or {}
    evidence_required = bool(values.get("agent_evidence_delivery_required", False))
    evidence_delivery = str(
        values.get("agent_evidence_delivery_status", "not_required") or "not_required"
    )
    citation_mode = str(values.get("agent_citation_mode", "none") or "none")
    evidence_recovered = bool(values.get("agent_evidence_recovered", False))
    coverage_status = str(values.get("agent_social_coverage_status", "") or "")
    social_tool_execution = str(
        values.get("agent_social_tool_execution", "not_used") or "not_used"
    )
    general_tool_execution = str(
        values.get("agent_tool_execution", "not_used") or "not_used"
    )
    tool_execution = (
        social_tool_execution
        if social_tool_execution != "not_used"
        else general_tool_execution
    )
    evidence_unavailable = bool(
        values.get("agent_evidence_unavailable", False)
        or values.get("media_reference_unavailable", False)
    )
    if evidence_unavailable and evidence_delivery == "not_required":
        evidence_delivery = "incomplete"
    outbound_delivery = (
        "unconfirmed" if delivery_unknown else "partial" if delivery_partial else "confirmed"
    )
    visible_nonempty = bool(str(visible_text or "").strip())

    if delivery_unknown or not visible_nonempty:
        outcome = "failed"
        diagnosis_code = "outbound_send_failed"
    elif delivery_partial:
        outcome = "partial"
        diagnosis_code = "outbound_send_failed"
    elif evidence_required and evidence_delivery not in {"met", "recovered"}:
        outcome = "partial"
        diagnosis_code = "evidence_delivery_incomplete"
    elif evidence_unavailable:
        outcome = "partial"
        diagnosis_code = "evidence_delivery_incomplete"
    elif evidence_recovered or evidence_delivery == "recovered":
        outcome = "partial"
        diagnosis_code = "visible_output_recovered"
    elif coverage_status == "degraded" or tool_execution == "partial":
        outcome = "partial"
        diagnosis_code = "social_coverage_degraded"
    else:
        outcome = "ok"
        diagnosis_code = "ok"

    return {
        "outcome": outcome,
        "diagnosis_code": diagnosis_code,
        "tool_execution": tool_execution,
        "evidence_delivery": evidence_delivery,
        "outbound_delivery": outbound_delivery,
        "coverage_status": coverage_status,
        "evidence_required": evidence_required,
        "citation_mode": citation_mode,
        "evidence_recovered": evidence_recovered,
        "evidence_unavailable": evidence_unavailable,
    }


__all__ = ["resolve_sent_reply_completion"]
