"""One immutable scientific configuration, separate from editor selection."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from bproc_core.config import ProjectConfig
from bproc_core.project_io import load_project, save_project


class ProjectState(QObject):
    project_changed = Signal()
    selection_changed = Signal(object)
    meshes_changed = Signal()
    candidate_changed = Signal(object)
    dirty_changed = Signal(bool)

    def __init__(self, config: ProjectConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.edited_target_key = config.targets[0].asset.key if config.targets else None
        self.meshes = {}
        self.candidate = None
        self.dirty = False
        self.path = None
        # Bumped whenever the project behind the editor changes; background work compares it to
        # decide whether a finished result still describes what the user is looking at.
        self.context_revision = 0
        # Geometry identity: loading, replacing or dropping meshes must invalidate results that
        # were computed against the previous meshes even when the config stayed the same.
        self.mesh_revision = 0

    def _touch_context(self):
        self.context_revision += 1

    def _set_dirty(self, dirty):
        if self.dirty != dirty:
            self.dirty = dirty
            self.dirty_changed.emit(dirty)

    def set_project(self, config: ProjectConfig):
        old_assets = {t.asset.key: t.asset for t in self.config.targets}
        old_assets.update({a.key: a for a in self.config.occluders})
        new_assets = {t.asset.key: t.asset for t in config.targets}
        new_assets.update({a.key: a for a in config.occluders})
        self.config = config
        keys = {t.asset.key for t in config.targets}
        keys.update(a.key for a in config.occluders)
        self.meshes = {key: mesh for key, mesh in self.meshes.items()
                       if key in keys and old_assets.get(key) == new_assets.get(key)}
        if self.edited_target_key not in {t.asset.key for t in config.targets}:
            self.edited_target_key = config.targets[0].asset.key if config.targets else None
        self.candidate = None
        self._touch_context()
        self.mesh_revision += 1
        self._set_dirty(False)
        self.project_changed.emit()
        self.selection_changed.emit(self.edited_target_key)
        self.meshes_changed.emit()
        self.candidate_changed.emit(None)

    def update(self, **changes):
        self.replace_config(replace(self.config, **changes))

    def replace_config(self, config):
        if config == self.config:
            return
        # Appearance is not geometry: recolouring the target must not throw away the candidate
        # the viewport is showing (the old behaviour blanked both viewports for a whole search).
        # Viewport LOD is display-only too: switching auto/full must keep the same candidate.
        appearance_only = replace(config, material=self.config.material,
                                  viewport=self.config.viewport) == self.config
        old_assets = {t.asset.key: t.asset for t in self.config.targets}
        old_assets.update({a.key: a for a in self.config.occluders})
        new_assets = {t.asset.key: t.asset for t in config.targets}
        new_assets.update({a.key: a for a in config.occluders})
        stale = {key for key in self.meshes if old_assets.get(key) != new_assets.get(key)}
        for key in stale:
            self.meshes.pop(key, None)
        self.config = config
        target_keys = {t.asset.key for t in config.targets}
        if self.edited_target_key not in target_keys:
            self.edited_target_key = config.targets[0].asset.key if config.targets else None
            self.selection_changed.emit(self.edited_target_key)
        keep_candidate = appearance_only and not stale and self.candidate is not None
        if not keep_candidate:
            self.candidate = None
        self._touch_context()
        self._set_dirty(True)
        self.project_changed.emit()
        if stale:
            self.mesh_revision += 1
            self.meshes_changed.emit()
        if not keep_candidate:
            self.candidate_changed.emit(None)

    def select_target(self, key):
        if key is not None and key not in {t.asset.key for t in self.config.targets}:
            raise ValueError("编辑目标不存在")
        if key != self.edited_target_key:
            self.edited_target_key = key
            self.selection_changed.emit(key)

    def set_anchor(self, key):
        if not any(t.asset.key == key and t.enabled for t in self.config.targets):
            raise ValueError("渲染中心必须是已启用的目标")
        self.update(anchor_key=key)

    def set_meshes(self, meshes):
        self.meshes = dict(meshes)
        if self.candidate is not None:
            self.candidate = None
            self.candidate_changed.emit(None)
        self.mesh_revision += 1
        self.meshes_changed.emit()

    def set_candidate(self, candidate):
        self.candidate = candidate
        self.candidate_changed.emit(candidate)

    def load(self, path):
        config = load_project(Path(path))
        self.meshes = {}
        self.path = Path(path).resolve()   # set before the change signal, so background work sees the new file
        self.set_project(config)

    def save(self, path=None):
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("请先选择项目文件")
        save_project(self.config, target)
        resolved = target.resolve()
        if resolved != self.path:
            self.path = resolved
            self._touch_context()          # save-as invalidates work started for the previous file
        self._set_dirty(False)
