"""运行目录与幂等键。

目录布局（docs/v0-spec.md §3）：
    data/<run_id>/
        paper.pdf         (M1)
        repo/             (M2)
        events.jsonl
        analysis.json
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

from .config import settings


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def run_dir(run_id: str) -> Path:
    path = settings.data_dir / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_path(run_id: str) -> Path:
    return run_dir(run_id) / "events.jsonl"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def meta_path(run_id: str) -> Path:
    return run_dir(run_id) / "meta.json"


def write_meta(run_id: str, **fields: Any) -> dict[str, Any]:
    path = meta_path(run_id)
    current = read_json(path) or {}
    current.update(fields)
    write_json(path, current)
    return current


def read_meta(run_id: str) -> dict[str, Any]:
    return read_json(meta_path(run_id)) or {}


def paper_path(run_id: str) -> Path:
    return run_dir(run_id) / "paper.pdf"


def run_key(
    *,
    pdf_sha256: str,
    repo_url: str,
    commit_sha: str,
    prompt_version: str,
    model: str,
) -> str:
    """§8 幂等键：同论文 + 同 commit + 同 prompt + 同模型 → 直接回放，不花钱。"""
    material = "|".join([pdf_sha256, repo_url, commit_sha, prompt_version, model])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
