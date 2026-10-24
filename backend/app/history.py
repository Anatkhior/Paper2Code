"""Local history: lightweight index, explicit recovery, and a consistent read snapshot.

Analysis files remain the source of truth. history.json is a derived index plus the
current operation journal; it never contains credentials or a resumable LLM request.
"""
from __future__ import annotations

import json
import time
import uuid
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

from . import store
from .config import settings
from .verify import summarize

INDEX_VERSION = 1
TRACE_PAGE_SIZE = 100
_index_lock = RLock()


def serialized_index(function):
    """List migration runs in a worker thread; it must not overwrite a live update."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _index_lock:
            return function(*args, **kwargs)
    return wrapped


def _object(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{name} 应为对象")


def _items(value: Any, name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} 应为对象列表")
    for item in value:
        if not isinstance(item.get("id"), str):
            raise ValueError(f"{name} 条目缺少 id")
        _object(item.get("explanation"), "explanation")
        for field in ("paper_evidence", "code_evidence"):
            if field in item and (not isinstance(item[field], list) or any(not isinstance(row, dict) for row in item[field])):
                raise ValueError(f"{field} 应为对象列表")


def _validate_document(name: str, value: dict) -> None:
    if name == "meta.json":
        _object(value.get("paper"), "paper")
    if name == "plan.json":
        _object(value.get("plan"), "plan")
        if value.get("plan"):
            _items(value["plan"].get("innovations", []), "plan.innovations")
    if name == "artifact.json":
        _object(value.get("run"), "run")
        for field in ("paper", "repo", "provider"):
            _object((value.get("run") or {}).get(field), f"run.{field}")
        _object(value.get("verification"), "verification")
        _items(value.get("innovations", []), "innovations")
    if name == "history.json":
        for field in ("run_id", "title", "analysis_status"):
            if not isinstance(value.get(field), str):
                raise ValueError(f"索引缺少 {field}")
        for field in ("created_at", "updated_at"):
            if not isinstance(value.get(field), (int, float)):
                raise ValueError(f"索引缺少 {field}")
        _object(value.get("latest_operation"), "latest_operation")
        _object(value.get("original_provider"), "original_provider")


def read_document(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        value = store.read_json(path)
        if value is not None and not isinstance(value, dict):
            raise ValueError("预期 JSON 对象")
        if value is not None:
            _validate_document(path.name, value)
        return value
    except (OSError, ValueError) as exc:
        warnings.append(f"{path.name} 无法读取：{exc}")
        return None


def read_journal(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """A damaged journal is viewable up to the error, never silently repaired."""
    if not path.exists():
        return []
    rows = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("预期 JSON 对象")
            if path.name == "events.jsonl":
                if not isinstance(value.get("id"), int) or not isinstance(value.get("ts"), (int, float)) or not isinstance(value.get("type"), str) or not isinstance(value.get("data"), dict):
                    raise ValueError("事件缺少 id/type/ts/data")
                if value["type"] in {"plan_ready", "plan_updated"}:
                    _validate_document("plan.json", value["data"])
                if value["type"] == "finding":
                    _items([value["data"].get("finding")], "finding")
                if value["type"] == "repo_ready":
                    _object(value["data"].get("repo"), "repo")
            if path.name == "chat.jsonl" and (value.get("role") not in {"user", "assistant"} or not isinstance(value.get("text"), str)):
                raise ValueError("对话缺少 role/text")
            rows.append(value)
    except (OSError, ValueError) as exc:
        warnings.append(f"{path.name} 读取中断（第 {len(rows) + 1} 条附近）：{exc}；原文件保留，需修复后才能续问。")
    return rows


def _repo(artifact: dict | None, events: list[dict]) -> dict | None:
    pinned = ((artifact or {}).get("run") or {}).get("repo")
    if pinned:
        return pinned
    for event in reversed(events):
        if event.get("type") == "repo_ready":
            return event["data"]["repo"]
        if event.get("type") == "repo_cloning":
            return None
    return None


def _apply_event(entry: dict, event: dict) -> dict:
    entry = {**entry, "updated_at": event["ts"]}
    kind, data = event["type"], event["data"]
    operation = dict(entry.get("latest_operation") or {})
    if kind in {"repo_cloning", "chat_user"}:
        operation = {"id": event.get("operation_id"), "phase": "locate" if kind == "repo_cloning" else "chat",
                     "status": "running", "started_at": event["ts"]}
    if kind == "run_start":
        phase = (data.get("meta") or {}).get("phase", operation.get("phase"))
        operation.update(id=event.get("operation_id"), phase=phase, status="running", started_at=event["ts"])
        if not entry.get("original_provider") and phase != "chat":
            entry["original_provider"] = {"model": data.get("model"), "protocol": data.get("provider")}
    elif kind in {"plan_ready", "plan_updated"}:
        entry["innovation_count"] = len((data.get("plan") or {}).get("innovations") or [])
        if not operation:
            entry["analysis_status"] = "partial"
    elif kind == "finding":
        ids = set(entry.get("finding_ids") or [])
        ids.add(data["finding"]["id"])
        entry["finding_ids"] = sorted(ids)
        entry["result_count"] = len(ids)
    elif kind == "run_end":
        status = "failed" if data.get("finalize_error") else data.get("status", "failed")
        operation.update(status=status, ended_at=event["ts"], reason=data.get("stopped_reason"))
        if operation.get("phase") != "chat":
            if status == "ok":
                status = "recon_complete" if operation.get("phase") == "recon" else "complete"
                if data.get("missing_ids") or data.get("partial"):
                    status = "partial"
            entry["analysis_status"] = status
    if operation:
        entry["latest_operation"] = operation
    return entry


@serialized_index
def index_entry(run_id: str, *, reconcile: bool = True) -> dict:
    directory = store.run_dir(run_id)
    index_path = directory / "history.json"
    warnings: list[str] = []
    entry = read_document(index_path, warnings)
    journal = directory / "events.jsonl"
    journal_size = journal.stat().st_size if journal.exists() else 0
    if entry and entry.get("schema_version") == INDEX_VERSION and (not reconcile or (not entry.get("warnings") and entry.get("journal_size") == journal_size)):
        return entry
    previous = entry or {}
    # A corrupt derived index can be rebuilt. Source documents are never rewritten.
    warnings = []
    meta = read_document(directory / "meta.json", warnings) or {}
    events = read_journal(directory / "events.jsonl", warnings)
    plan = (read_document(directory / "plan.json", warnings) or {}).get("plan") or {}
    artifact = read_document(directory / "artifact.json", warnings) or {}
    paper = meta.get("paper") or {}
    created = events[0]["ts"] if events else directory.stat().st_mtime
    entry = {
        "schema_version": INDEX_VERSION, "run_id": run_id, "journal_size": journal_size,
        "kind": "m0" if meta.get("phase") == "m0" and not paper else "paper",
        "title": paper.get("title_guess") or paper.get("filename") or run_id,
        "filename": paper.get("filename"), "created_at": created, "updated_at": created,
        "analysis_status": "uploaded", "latest_operation": None,
        "original_provider": meta.get("original_provider"),
        "innovation_count": len(plan.get("innovations") or []),
        "result_count": 0, "finding_ids": [], "warnings": warnings,
    }
    for event in events:
        entry = _apply_event(entry, event)
    pending = previous.get("latest_operation") or {}
    if pending.get("started_at", 0) > (events[-1]["ts"] if events else 0):
        entry["latest_operation"] = pending
        if pending.get("phase") != "chat":
            entry["analysis_status"] = "running"
    if artifact:
        entry["result_count"] = len(artifact.get("innovations") or [])
        entry["original_provider"] = entry["original_provider"] or (artifact.get("run") or {}).get("provider")
    if not entry["original_provider"] and meta.get("model"):
        entry["original_provider"] = {"model": meta["model"]}
    # Legacy conversion is additive; never rewrite original analysis or journals.
    store.write_json(index_path, entry)
    return entry


def recovered(entry: dict, *, active: bool) -> dict:
    operation = entry.get("latest_operation") or {}
    if operation.get("status") != "running" or active:
        return entry
    result = {**entry, "latest_operation": {**operation, "status": "interrupted", "reason": "服务已重启，任务中断；已保存的内容保留。"}}
    if operation.get("phase") != "chat":
        result["analysis_status"] = "interrupted"
    return result


@serialized_index
def recover_run(run_id: str) -> dict:
    previous = index_entry(run_id)
    result = recovered(previous, active=False)
    if result != previous:
        store.write_json(store.run_dir(run_id) / "history.json", result)
    return result


@serialized_index
def begin_operation(run_id: str, phase: str, provider: dict) -> str:
    entry = index_entry(run_id)
    operation_id = uuid.uuid4().hex
    now = time.time()
    entry = {**entry, "updated_at": now, "latest_operation": {
        "id": operation_id, "phase": phase, "status": "running", "started_at": now, "provider": provider,
    }}
    if phase != "chat":
        entry["analysis_status"] = "running"
        entry["original_provider"] = entry.get("original_provider") or provider
    if phase == "locate":
        entry.update(finding_ids=[], result_count=0)
    store.write_json(store.run_dir(run_id) / "history.json", entry)
    return operation_id


@serialized_index
def record_event(run_id: str, event: dict) -> None:
    if event["type"] not in {"paper_ready", "repo_cloning", "chat_user", "run_start", "plan_ready", "plan_updated", "finding", "run_end", "chat_reply"}:
        return
    entry = _apply_event(index_entry(run_id, reconcile=False), event)
    entry["journal_size"] = store.events_path(run_id).stat().st_size
    store.write_json(store.run_dir(run_id) / "history.json", entry)


def list_history(*, query: str, offset: int, limit: int, active_ids: set[str]) -> dict:
    entries = []
    root = settings.data_dir
    if root.exists():
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not (directory / "meta.json").exists():
                continue
            try:
                entry = recovered(index_entry(directory.name, reconcile=directory.name not in active_ids), active=directory.name in active_ids)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                entry = {"run_id": directory.name, "title": directory.name, "updated_at": directory.stat().st_mtime,
                         "analysis_status": "unreadable", "warnings": [f"历史索引无法读取：{exc}"]}
            if entry.get("kind") == "m0":
                continue
            if query.casefold() not in f"{entry['title']} {entry.get('filename') or ''}".casefold():
                continue
            entries.append({key: value for key, value in entry.items() if key != "finding_ids"})
    entries.sort(key=lambda item: (item["updated_at"], item["run_id"]), reverse=True)
    return {"items": entries[offset:offset + limit], "total": len(entries), "offset": offset, "limit": limit}


def snapshot(run_id: str, *, active: bool, events: list[dict] | None = None) -> dict:
    directory = store.run_dir(run_id)
    warnings: list[str] = []
    meta = read_document(directory / "meta.json", warnings) or {}
    plan = (read_document(directory / "plan.json", warnings) or {}).get("plan")
    artifact = read_document(directory / "artifact.json", warnings)
    transcript = read_journal(directory / "chat.jsonl", warnings)
    if events is None:
        events = read_journal(directory / "events.jsonl", warnings)
    entry = recovered(index_entry(run_id, reconcile=not active), active=active)
    repo = _repo(artifact, events)
    findings = (artifact or {}).get("innovations") or []
    if not artifact:
        # Interrupted initial locate may have persisted findings but no final artifact.
        current: dict[str, dict] = {}
        for event in events:
            if event["type"] == "repo_cloning":
                current.clear()
            if event["type"] == "finding":
                item = event["data"]["finding"]
                current[item["id"]] = item
        findings = list(current.values())
    resources = {"paper": (directory / "paper.pdf").is_file(),
                 "repo": bool(repo and (directory / "repo" / ".git").exists())}
    if not resources["paper"]:
        warnings.append("论文 PDF 不在本机，已保存的解读和对话仍可阅读。")
    if repo and not resources["repo"]:
        warnings.append("历史代码仓库不在本机，无法读取或核对代码；解读和对话仍可阅读。")
    first_event = max(0, len(events) - TRACE_PAGE_SIZE)
    if active and (entry.get("latest_operation") or {}).get("phase") == "chat":
        # The current reply needs its chat_user/step_start boundary to reconstruct
        # streaming text. Older trace pages stay lazy, even on long conversations.
        starts = [index for index, event in enumerate(events) if event["type"] == "chat_user"]
        if starts:
            first_event = min(first_event, starts[-1])
    return {"run_id": run_id, "meta": meta, "history": entry, "paper": meta.get("paper"),
            "plan": plan, "artifact": artifact, "findings": findings, "turns": transcript,
            "verification": summarize(artifact, artifact["verification"]) if artifact and artifact.get("verification") else None,
            "repo": repo, "resources": resources, "warnings": warnings,
            "can_chat": not any("无法读取" in warning or "读取中断" in warning for warning in warnings),
            "active": active, "event_cursor": events[-1]["id"] if events else 0,
            "events": events[first_event:], "has_older_events": first_event > 0}
