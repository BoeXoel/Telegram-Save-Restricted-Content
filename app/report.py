from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.db import Database


REPORT_COLUMNS = (
    "job_id",
    "source_link",
    "source_chat_id",
    "source_topic_id",
    "source_message_id",
    "size_bytes",
    "media_type",
    "caption_summary",
    "dest_chat_id",
    "dest_topic_id",
    "writer_identity",
    "upload_limit_bytes",
    "status",
    "reason_code",
    "transfer_route",
    "remote_uri",
    "last_error",
)


@dataclass(frozen=True)
class OversizedReport:
    rows: list[dict[str, str]]

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows)

    def print(self) -> None:
        if not self.rows:
            print("No oversized records found")
            return
        for row in self.rows:
            location = row["source_link"] or f"chat {row['source_chat_id']} message {row['source_message_id']}"
            print(
                " | ".join(
                    (
                        f"job {row['job_id']}",
                        location,
                        f"{row['media_type']} {row['size_bytes']} bytes",
                        f"state={row['status']}",
                        f"route={row['transfer_route'] or 'record'}",
                        f"remote={row['remote_uri'] or '-'}",
                    )
                )
            )


def build_oversized_report(database: Database) -> OversizedReport:
    report_rows: list[dict[str, str]] = []
    for job in database.oversized_jobs():
        items = _manifest_items(job["media_manifest"], job)
        for item in items:
            report_rows.append(
                {
                    "job_id": str(job["id"]),
                    "source_link": source_link(
                        str(job["source_chat_id"]),
                        int(item["message_id"]),
                        job["source_topic_id"],
                    ),
                    "source_chat_id": str(job["source_chat_id"]),
                    "source_topic_id": _text(job["source_topic_id"]),
                    "source_message_id": _text(item["message_id"]),
                    "size_bytes": _text(item["size"]),
                    "media_type": _text(item["type"]) or _text(job["media_type"]),
                    "caption_summary": caption_summary(job["caption"]),
                    "dest_chat_id": str(job["dest_chat_id"]),
                    "dest_topic_id": _text(job["dest_topic_id"]),
                    "writer_identity": _text(job["writer_identity"]),
                    "upload_limit_bytes": _text(job["upload_limit_bytes"]),
                    "status": str(job["status"]),
                    "reason_code": _text(job["reason_code"]),
                    "transfer_route": _text(job["transfer_route"]),
                    "remote_uri": redact_uri(_text(job["remote_uri"])),
                    "last_error": _text(job["last_error"]),
                }
            )
    return OversizedReport(rows=report_rows)


def source_link(chat_id: str, message_id: int, topic_id: Any) -> str:
    if chat_id.startswith("-100") and chat_id[4:].isdigit():
        base = f"https://t.me/c/{chat_id[4:]}"
        if topic_id:
            return f"{base}/{int(topic_id)}/{message_id}"
        return f"{base}/{message_id}"
    if chat_id.startswith("@") and len(chat_id) > 1:
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    return ""


def caption_summary(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact_uri(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return value
    host = parsed.hostname
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))


def _manifest_items(value: Any, job: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, json.JSONDecodeError):
        parsed = []
    if isinstance(parsed, list) and parsed:
        items = [item for item in parsed if isinstance(item, dict)]
        if items:
            return items
    return [
        {
            "message_id": job["source_message_id"],
            "size": job["file_size"],
            "type": job["media_type"],
        }
    ]


def _text(value: Any) -> str:
    return "" if value is None else str(value)
