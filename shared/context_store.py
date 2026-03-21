import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)

CONTEXT_ROOT = Path(os.getenv("CONTEXT_ROOT", "/workspace/.ai_factory/contexts"))
CONTEXT_ROOT.mkdir(parents=True, exist_ok=True)

MAX_CONTEXT_SIZE_KB = 512
MAX_CONTEXT_TOTAL_MB = 50
CONTEXT_TTL_DAYS = int(os.getenv("CONTEXT_TTL_DAYS", "7"))

CONTEXT_PRIORITY = {
    "always_keep": [
        "task_id",
        "description",
        "status",
        "error",
        "project_name",
        "stage",
    ],
    "truncate": [
        "code",
        "logs",
        "full_description",
        "artifacts",
        "delivery_summary",
        "execution_plan",
    ],
    "drop": ["raw_debug", "internal_trace", "_context_file", "_workflow_id"],
}


def get_workflow_dir(workflow_id: str) -> Path:
    workflow_dir = CONTEXT_ROOT / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)
    return workflow_dir


def _get_size_kb(data: Dict[str, Any]) -> float:
    serialized = json.dumps(data, ensure_ascii=True)
    return len(serialized.encode("utf-8")) / 1024


def save_context(
    workflow_id: str,
    stage: str,
    data: Dict[str, Any],
    subdir: str = "",
) -> str:
    workflow_dir = get_workflow_dir(workflow_id)
    if subdir:
        workflow_dir = workflow_dir / subdir
        workflow_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{stage}.json"
    filepath = workflow_dir / filename

    size_kb = _get_size_kb(data)

    context_data = {
        "_meta": {
            "workflow_id": workflow_id,
            "stage": stage,
            "saved_at": datetime.now().isoformat(),
            "size_kb": round(size_kb, 2),
            "version": "1.0",
        },
        **{k: v for k, v in data.items() if k not in CONTEXT_PRIORITY["drop"]},
    }

    try:
        filepath.write_text(
            json.dumps(context_data, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        LOGGER.info(
            "[context] Saved | workflow=%s | stage=%s | size=%.2fKB | path=%s",
            workflow_id,
            stage,
            size_kb,
            str(filepath),
        )
        return str(filepath)
    except Exception as e:
        LOGGER.error(
            "[context] Failed to save | workflow=%s | stage=%s | error=%s",
            workflow_id,
            stage,
            e,
        )
        raise


def load_context(filepath: str) -> Dict[str, Any]:
    path = Path(filepath)
    if not path.exists():
        LOGGER.warning("[context] File not found: %s", filepath)
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if "_meta" in data:
            LOGGER.info(
                "[context] Loaded | workflow=%s | stage=%s | size=%.2fKB",
                data["_meta"].get("workflow_id", "unknown"),
                data["_meta"].get("stage", "unknown"),
                data["_meta"].get("size_kb", 0),
            )

        for key in CONTEXT_PRIORITY["drop"]:
            data.pop(key, None)

        return data
    except Exception as e:
        LOGGER.error("[context] Failed to load | path=%s | error=%s", filepath, e)
        raise


def truncate_for_llm(
    data: Dict[str, Any],
    max_tokens: int = 8000,
    max_size_kb: float = MAX_CONTEXT_SIZE_KB,
) -> Dict[str, Any]:
    serialized = json.dumps(data, ensure_ascii=True)
    size_kb = len(serialized.encode("utf-8")) / 1024

    if size_kb <= max_size_kb:
        return data

    LOGGER.warning(
        "[context] Truncating large context | current=%.2fKB | max=%.2fKB",
        size_kb,
        max_size_kb,
    )

    result = {}

    for key in CONTEXT_PRIORITY["always_keep"]:
        if key in data:
            result[key] = data[key]

    for key in CONTEXT_PRIORITY["truncate"]:
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value[:5000] + "\n... [TRUNCATED]"
            elif isinstance(value, list):
                value = value[:50] if len(value) > 50 else value
                if any(isinstance(item, dict) for item in value):
                    value = [
                        {
                            k: v
                            for k, v in item.items()
                            if k in CONTEXT_PRIORITY["always_keep"]
                        }
                        if isinstance(item, dict)
                        else item
                        for item in value[:20]
                    ]
            result[key] = value

    result["_truncated"] = True
    result["_original_size_kb"] = round(size_kb, 2)

    return result


def append_audit_log(workflow_id: str, entry: Dict[str, Any]) -> str:
    workflow_dir = get_workflow_dir(workflow_id)
    audit_file = workflow_dir / "audit.jsonl"
    entry["_audit_ts"] = datetime.now().isoformat()
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    return str(audit_file)


def log_activity_event(
    workflow_id: str,
    activity: str,
    event: str,
    duration_ms: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    entry = {
        "type": "activity_event",
        "workflow_id": workflow_id,
        "activity": activity,
        "event": event,
        "duration_ms": duration_ms,
        **(metadata or {}),
    }
    append_audit_log(workflow_id, entry)


def generate_markdown_log(workflow_id: str) -> str:
    workflow_dir = get_workflow_dir(workflow_id)
    audit_file = workflow_dir / "audit.jsonl"
    md_file = workflow_dir / "audit.md"

    if not audit_file.exists():
        return str(md_file)

    entries = []
    try:
        for line in audit_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except Exception as e:
        LOGGER.warning("[context] Failed to parse audit log: %s", e)
        return str(md_file)

    if not entries:
        return str(md_file)

    lines = [
        f"# Workflow Audit: {workflow_id}",
        "",
        "## Timeline",
        "",
        "| Time | Activity | Event | Duration | Details |",
        "|------|----------|-------|----------|---------|",
    ]

    for entry in entries:
        ts = entry.get("ts", entry.get("_audit_ts", ""))
        activity = entry.get("activity", entry.get("type", ""))
        event = entry.get("event", "")
        duration = (
            f"{entry.get('duration_ms', 0)}ms" if entry.get("duration_ms") else "-"
        )

        details_parts = []
        metadata = {
            k: v
            for k, v in entry.items()
            if k
            not in {
                "type",
                "workflow_id",
                "activity",
                "event",
                "duration_ms",
                "ts",
                "_audit_ts",
            }
        }
        for k, v in list(metadata.items())[:3]:
            if isinstance(v, (str, int, float, bool)):
                details_parts.append(f"{k}={v}")

        details = "; ".join(details_parts) if details_parts else "-"
        if len(details) > 50:
            details = details[:47] + "..."

        lines.append(f"| {ts[:19]} | {activity} | {event} | {duration} | {details} |")

    try:
        md_file.write_text("\n".join(lines), encoding="utf-8")
        LOGGER.info("[context] Generated markdown audit log: %s", md_file)
    except Exception as e:
        LOGGER.error("[context] Failed to write markdown log: %s", e)

    return str(md_file)


def cleanup_old_contexts(days: int = CONTEXT_TTL_DAYS) -> int:
    if not CONTEXT_ROOT.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=days)
    removed = 0

    for workflow_dir in CONTEXT_ROOT.iterdir():
        if not workflow_dir.is_dir():
            continue

        try:
            dir_mtime = datetime.fromtimestamp(workflow_dir.stat().st_mtime)
            if dir_mtime < cutoff:
                import shutil

                shutil.rmtree(workflow_dir)
                removed += 1
                LOGGER.info("[context] Removed old context: %s", workflow_dir.name)
        except Exception as e:
            LOGGER.warning("[context] Failed to remove %s: %s", workflow_dir, e)

    if removed > 0:
        LOGGER.info("[context] Cleanup complete | removed=%d", removed)

    return removed


def get_workflow_stats(workflow_id: str) -> Dict[str, Any]:
    workflow_dir = get_workflow_dir(workflow_id)
    if not workflow_dir.exists():
        return {"exists": False}

    stats = {
        "exists": True,
        "workflow_id": workflow_id,
        "files": [],
        "total_size_kb": 0,
    }

    try:
        for f in workflow_dir.rglob("*.json"):
            if f.name == "audit.jsonl":
                continue
            size_kb = f.stat().st_size / 1024
            stats["files"].append(
                {
                    "path": str(f.relative_to(workflow_dir)),
                    "size_kb": round(size_kb, 2),
                }
            )
            stats["total_size_kb"] += size_kb

        stats["total_size_kb"] = round(stats["total_size_kb"], 2)

        audit_file = workflow_dir / "audit.jsonl"
        if audit_file.exists():
            lines = audit_file.read_text(encoding="utf-8").splitlines()
            stats["audit_entries"] = len([l for l in lines if l.strip()])

    except Exception as e:
        LOGGER.warning("[context] Failed to get stats for %s: %s", workflow_id, e)

    return stats
