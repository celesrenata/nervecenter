"""Issue groups and their latest diagnosis for the kube agent (NS-ADR-120).

Many issues often share one cause (every ``stonks-oracle`` pod stuck in ImagePullBackOff, three
``svclb`` pods Pending on a port conflict). ``group_issues`` folds open issues into groups, ranked
by severity and blast radius; the monitor starts one focused kube_doctor run per group and
``DiagnosisStore`` keeps each group's newest diagnosis (summary, evidence, confidence, run link)
so /kube shows one line per group.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from backend.kube.issues import ReportStore

IMAGE_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull",
                 "ImageInspectError", "RegistryUnavailable"}
CLUSTER = "_cluster"
MAX_PODS_SHOWN = 8
MAX_PODS_PER_GROUP = 40
_SEVERITY = {"critical": 2, "warning": 1}
_MODE_RANK = {"normal": 0, "override_active": 0, "diagnose_only": 1, "known_down": 2}
_SLUG = re.compile(r"[^a-z0-9]+")
DIAG_MARK = "DIAGNOSIS:"
#: Tools that count as "looked at the evidence" for a focus run.
EVIDENCE_TOOLS = {"kube_describe", "kube_logs", "kube_events"}
EVIDENCE_KUBECTL_VERBS = {"describe", "logs", "events"}


def _slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:60] or "x"


def cause_of(issue: dict[str, Any]) -> str:
    """Cause bucket inside one namespace: related issues share it."""
    kind, reason = issue.get("kind", ""), str(issue.get("reason", ""))
    if kind == "pod":
        if reason in IMAGE_REASONS:
            return "imagepull"
        if reason == "Pending":
            return "pending"
        return f"{_slug(reason)}-{_slug(str(issue.get('name', '')))}"
    return f"{kind}-{_slug(reason)}" + (f"-{_slug(str(issue.get('name', '')))}"
                                        if kind in ("node", "volume", "pv") else "")


def group_id(issue: dict[str, Any]) -> str:
    return f"{_slug(issue.get('namespace') or CLUSTER)}:{cause_of(issue)}"


def _issue_pods(issue: dict[str, Any]) -> list[str]:
    ev = issue.get("evidence") or {}
    pods = ev.get("pods") or ([ev["pod"]] if ev.get("pod") else [])
    return [str(p) for p in pods]


def group_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open issues (ledger dicts with ``mode`` already set) -> ranked groups."""
    groups: dict[str, dict[str, Any]] = {}
    for issue in issues:
        gid = group_id(issue)
        g = groups.setdefault(gid, {
            "id": gid, "namespace": issue.get("namespace", ""), "cause": cause_of(issue),
            "kinds": [], "reasons": [], "issue_keys": [], "workloads": [], "pods": [],
            "severity": "warning", "mode": issue.get("mode", "normal"), "first_seen": None,
            "last_seen": None})
        for field, value in (("kinds", issue.get("kind")), ("reasons", issue.get("reason")),
                             ("issue_keys", issue.get("key")), ("workloads", issue.get("name"))):
            if value and value not in g[field]:
                g[field].append(value)
        for pod in _issue_pods(issue):
            if pod not in g["pods"] and len(g["pods"]) < MAX_PODS_PER_GROUP:
                g["pods"].append(pod)
        if _SEVERITY.get(issue.get("severity"), 0) > _SEVERITY.get(g["severity"], 0):
            g["severity"] = issue["severity"]
        if _MODE_RANK.get(issue.get("mode", "normal"), 0) < _MODE_RANK.get(g["mode"], 0):
            g["mode"] = issue["mode"]
        for field, pick in (("first_seen", min), ("last_seen", max)):
            value = issue.get(field)
            if value is not None:
                g[field] = value if g[field] is None else pick(g[field], value)
    out = []
    for g in groups.values():
        g["issue_count"] = len(g["issue_keys"])
        g["pod_count"] = len(g["pods"])
        g["blast_radius"] = max(g["pod_count"], g["issue_count"], len(g["workloads"]))
        out.append(g)
    out.sort(key=lambda g: (-_SEVERITY.get(g["severity"], 0), _MODE_RANK.get(g["mode"], 0),
                            -g["blast_radius"], g["id"]))
    return out


def group_context(group: dict[str, Any], coverage_line: str | None = None) -> str:
    """The data block that starts a focus run: only this group, with exact pod names."""
    lines = [
        f"Kube issue group (focus run): id={group['id']}",
        f"namespace={group['namespace'] or CLUSTER} cause={group['cause']} "
        f"severity={group['severity']} mode={group['mode']}",
        f"reasons={', '.join(group['reasons'])}; workloads={', '.join(group['workloads'][:10])}",
        f"issues={group['issue_count']}; pods ({group['pod_count']}): "
        + (", ".join(group["pods"]) if group["pods"] else "none listed"),
        f"issue keys: {'; '.join(group['issue_keys'][:10])}",
    ]
    if coverage_line:
        lines.append(f"coverage: {coverage_line}")
    lines.append("Diagnose only this group. kube_health {group: \"" + group["id"]
                 + "\"} lists it again.")
    return "\n".join(lines)


def parse_diagnosis(output: str) -> dict[str, Any]:
    """``DIAGNOSIS: {json}`` line at the end of a report, else a clipped first paragraph."""
    for line in reversed((output or "").splitlines()):
        line = line.strip().lstrip("*#> ").strip()
        if line.upper().startswith(DIAG_MARK):
            try:
                data = json.loads(line[len(DIAG_MARK):].strip())
            except ValueError:
                break
            if isinstance(data, dict):
                ev = data.get("evidence")
                ev_list = [str(e)[:300] for e in ev][:6] if isinstance(ev, list) else (
                    [str(ev)[:300]] if ev else [])
                conf = str(data.get("confidence", "")).lower()
                return {"summary": str(data.get("summary", ""))[:500], "evidence": ev_list,
                        "cause": str(data.get("cause", ""))[:300],
                        "confidence": conf if conf in ("high", "medium", "low") else "low"}
    paragraph = next((p.strip() for p in (output or "").split("\n\n") if p.strip()), "")
    return {"summary": paragraph[:500], "evidence": [], "cause": "", "confidence": "low"}


def used_evidence_tool(events: list[dict[str, Any]]) -> bool:
    """Did the run call a describe, logs or events tool? (``tool_call`` live events)"""
    for ev in events:
        if ev.get("kind") != "tool_call":
            continue
        data = ev.get("data") or {}
        tool = str(data.get("tool", ""))
        if tool in EVIDENCE_TOOLS:
            return True
        if tool == "kubectl":
            args = (data.get("args") or {}).get("args")
            first = args[0] if isinstance(args, list) and args else str(args or "").split(" ")[0]
            if str(first) in EVIDENCE_KUBECTL_VERBS:
                return True
    return False


class DiagnosisStore:
    """Latest diagnosis per group id, kept as one ``diagnoses`` report row (newest wins)."""

    def __init__(self, reports: ReportStore) -> None:
        self._reports = reports

    async def all(self) -> dict[str, dict[str, Any]]:
        row = await self._reports.latest("diagnoses")
        data = (row or {}).get("data") or {}
        return dict(data.get("groups") or {})

    async def put(self, gid: str, diagnosis: dict[str, Any], now: datetime,
                  keep: set[str] | None = None) -> None:
        latest = await self._reports.latest("diagnoses")
        data = (latest or {}).get("data") or {}
        groups = dict(data.get("groups") or {})
        prev_at = (latest or {}).get("created_at")
        if isinstance(prev_at, datetime) and prev_at >= now:
            now = prev_at + timedelta(microseconds=1)  # newest row must win on a clock tie
        groups[gid] = {**diagnosis, "at": now.isoformat()}
        if keep is not None:  # forget groups that no longer exist, except this one
            groups = {k: v for k, v in groups.items() if k in keep or k == gid}
        await self._reports.add("diagnoses", {"groups": groups}, now)
