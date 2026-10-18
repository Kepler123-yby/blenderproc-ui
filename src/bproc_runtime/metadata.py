"""Frame records: one appended line per committed frame, one atomic manifest at the end.

Rewriting the whole manifest for every frame made metadata writing quadratic (400 frames took
about 226 MB of cumulative writes). The journal is append-only, so the committed bytes grow
linearly, and the final manifest is produced once in ``finish``.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

SCHEMA_VERSION = 2
JOURNAL_NAME = "frames.jsonl"
MANIFEST_NAME = "manifest.json"


class MetadataError(ValueError):
    """A dataset whose frame records cannot be trusted."""


def manifest_payload(seed, world, frames, *, final: bool = False) -> dict:
    """The manifest written at begin() (empty) or at finish() (final, cross-checked against the journal)."""
    return {"schema_version": SCHEMA_VERSION, "seed": seed, "world": world,
            "frames": list(frames), "final": bool(final)}


def encode_row(row: dict) -> str:
    return json.dumps({"schema_version": SCHEMA_VERSION, **row}, ensure_ascii=False, allow_nan=False)


def append_frame(metadata_dir: Path, row: dict) -> None:
    """Append exactly one journal line and force it to disk before it counts as committed."""
    metadata_dir = Path(metadata_dir)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    with (metadata_dir / JOURNAL_NAME).open("a", encoding="utf-8") as stream:
        stream.write(encode_row(row) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def reset_journal(metadata_dir: Path) -> None:
    (Path(metadata_dir) / JOURNAL_NAME).write_text("", encoding="utf-8")


def read_frame_records(metadata_dir: Path, *, allow_incomplete_tail: bool = False) -> list[dict]:
    """Read committed frame rows, preferring the journal and verifying it against the manifest."""
    folder = Path(metadata_dir)
    journal = folder / JOURNAL_NAME
    manifest = _load_manifest(folder / MANIFEST_NAME)
    if journal.exists():
        frames = _read_journal(journal, allow_incomplete_tail=allow_incomplete_tail)
        # Only the final manifest must agree row by row; while a run is going the manifest is
        # still the empty placeholder written by begin(). Version and field types are validated
        # for every snapshot so a tampered running manifest cannot slip through.
        if manifest is not None:
            version = manifest.get("schema_version", 0)
            if version != SCHEMA_VERSION:
                raise MetadataError(
                    f"manifest.json 与 frames.jsonl 版本不一致：manifest={version}, journal={SCHEMA_VERSION}")
            rows = manifest.get("frames")
            if not isinstance(rows, list):
                raise MetadataError("manifest.json 的 frames 不是列表")
            final = manifest.get("final", False)
            if not isinstance(final, bool):
                raise MetadataError("manifest.json 的 final 不是布尔值")
            if final:
                _require_consistent(frames, rows)
        return frames
    if manifest is None:
        raise MetadataError("缺少 frames.jsonl 与 manifest.json，无法读取帧记录")
    frames = manifest.get("frames")
    if not isinstance(frames, list):
        raise MetadataError("manifest.json 的 frames 不是列表")
    version = manifest.get("schema_version", 0)
    if version not in (0, 1):
        raise MetadataError(f"未知的 manifest 版本：{version}")
    if "final" in manifest and not isinstance(manifest["final"], bool):
        raise MetadataError("manifest.json 的 final 不是布尔值")
    return frames


def _load_manifest(path: Path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise MetadataError(f"manifest.json 不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise MetadataError("manifest.json 顶层必须是对象")
    return data


def _read_journal(path: Path, *, allow_incomplete_tail: bool) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        if not allow_incomplete_tail:
            raise MetadataError("frames.jsonl 末尾有一条未写完的记录")
        warnings.warn("frames.jsonl 末尾有一条未写完的记录，已忽略", RuntimeWarning)
        lines = lines[:-1]
    frames = []
    for number, line in enumerate(lines, start=1):
        body = line.rstrip("\n")
        if not body.strip():
            raise MetadataError(f"frames.jsonl 第 {number} 行是空行")
        try:
            row = json.loads(body)
        except ValueError as error:
            raise MetadataError(f"frames.jsonl 第 {number} 行不是合法 JSON：{error}") from error
        if not isinstance(row, dict):
            raise MetadataError(f"frames.jsonl 第 {number} 行不是对象")
        version = row.pop("schema_version", None)
        if version != SCHEMA_VERSION:
            raise MetadataError(f"frames.jsonl 第 {number} 行的版本未知：{version}")
        if row.get("frame_index") != len(frames):
            raise MetadataError(f"frames.jsonl 第 {number} 行的帧号不连续：{row.get('frame_index')}")
        frames.append(row)
    return frames


def _require_consistent(journal: list[dict], manifest: list) -> None:
    if not isinstance(manifest, list):
        raise MetadataError("manifest.json 的 frames 不是列表")
    if len(journal) != len(manifest):
        raise MetadataError(
            f"manifest.json 与 frames.jsonl 帧数不一致（manifest={len(manifest)}, journal={len(journal)}）")
    for position, (journal_row, manifest_row) in enumerate(zip(journal, manifest)):
        if not isinstance(manifest_row, dict):
            raise MetadataError(f"manifest.json 第 {position} 帧不是对象")
        if _canonical(journal_row) != _canonical(manifest_row):
            index = journal_row.get("frame_index", position)
            fields = sorted(set(journal_row) | set(manifest_row))
            different = [name for name in fields if journal_row.get(name) != manifest_row.get(name)]
            raise MetadataError(
                f"manifest.json 与 frames.jsonl 第 {index} 帧内容不一致"
                f"（字段：{'、'.join(different) or '未知'}）")


def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False)
