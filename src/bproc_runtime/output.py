"""Exclusive output reservation and append-only run metadata."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from bproc_core.masks import occlusion_ratio


class OutputConflict(FileExistsError):
    pass


def reserve_dataset(root: Path, dataset_name: str) -> Path:
    if (not dataset_name or dataset_name in (".", "..") or
            Path(dataset_name).name != dataset_name or "/" in dataset_name or "\\" in dataset_name):
        raise ValueError("Dataset name must be one nonempty path component")
    root = Path(root).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True)
    dataset = root / dataset_name
    try:
        dataset.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise OutputConflict(f"Output already exists; choose a new dataset name: {dataset}") from exc
    atomic_json(dataset / "run_metadata" / "status.json", {"status": "running"})
    return dataset


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


class JsonRunRecorder:
    def __init__(self, metadata_dir: Path):
        self.root = Path(metadata_dir)
        self._config = None
        self._frames = []
        self._started = None
        self._world = None
        self.begun = False
        self.finished = False

    def begin(self, config) -> None:
        from bproc_core.config import world_metadata
        from bproc_core.project_io import save_project
        self._config = config
        self._world = world_metadata(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self._started = datetime.now(timezone.utc).isoformat()
        atomic_json(self.root / "status.json", {"status": "running", "started_at": self._started,
                    "requested": config.total_shots, "written_frames": 0, "saved": 0,
                    "world": self._world})
        save_project(config, self.root / "project.yaml")
        atomic_json(self.root / "objects.json", [
            {"key": t.asset.key, "obj_id": t.obj_id, "name": t.name,
             "source": str(t.asset.path), "unit": t.asset.unit}
            for t in config.targets if t.enabled])
        atomic_json(self.root / "manifest.json", self._manifest())
        self.begun = True

    def _manifest(self) -> dict:
        return {"seed": self._config.seed, "world": self._world, "frames": self._frames}

    def record_frame(self, ref, candidate, evidence) -> None:
        from bproc_core.scene import candidate_to_dict
        anchor = next(t.obj_id for t in self._config.targets if t.asset.key == candidate.anchor_key)
        row = {**asdict(ref), "candidate": candidate_to_dict(candidate),
               "occlusion_ratio": occlusion_ratio(evidence, anchor), "seed": self._config.seed}
        self._frames.append(row)
        atomic_json(self.root / "manifest.json", self._manifest())
        self.begun = True

    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view") -> None:
        with (self.root / "rejections.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"scene_index": scene_index, "view_index": view_index,
                                     "attempt_index": attempt_index, "reason": reason,
                                     "phase": phase}, ensure_ascii=False) + "\n")

    def finish(self, summary) -> None:
        """Write the final status even when begin() only got partway through.

        The dataset directory is already ours at this point, so leaving a stale "running"
        marker would be worse than writing a status with unknown start time or world.
        """
        payload = {**asdict(summary), "finished_at": datetime.now(timezone.utc).isoformat()}
        if self._started is not None:
            payload["started_at"] = self._started
        if self._world is not None:
            payload["world"] = self._world
        atomic_json(self.root / "status.json", payload)
        self.finished = True


def write_terminal_status(dataset: Path, status: str, failures=None, exit_code: int = 1,
                          detail: str = "") -> None:
    """Record a terminal status for a dataset whose run never reached run_project."""
    payload = {"status": status, "exit_code": int(exit_code), "failures": dict(failures or {}),
               "finished_at": datetime.now(timezone.utc).isoformat()}
    if detail:
        payload["detail"] = detail
    atomic_json(Path(dataset) / "run_metadata" / "status.json", payload)
