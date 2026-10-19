#!/usr/bin/env python3
"""Capture actual Qt/VisPy windows on a real desktop, with reproducible evidence.

Run with the UI venv, not Blender Python. An offscreen Qt platform is unsuitable
for this integration check: it must exercise real OpenGL canvas contexts.
"""

import argparse
from dataclasses import replace
import importlib.metadata
import numpy as np
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
        parser.error("GUI smoke needs a real xcb/Wayland desktop OpenGL context")
    from PySide6.QtCore import QTimer
    from vispy.io import write_png
    from bproc_core.project_io import load_project
    from bproc_core.scene import candidate_to_dict
    from bproc_ui.app import create_app
    from bproc_ui.state import ProjectState
    from bproc_ui.main_window import MainWindow

    app = create_app([])
    state = ProjectState(load_project(args.project))
    state.path = args.project.resolve()
    window = MainWindow(state, restore_settings=False)
    window.resize(1320, 880)
    window.show()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    evidence = {"project": str(args.project.resolve()), "versions": {name: importlib.metadata.version(name)
                 for name in ("PySide6", "vispy", "numpy", "trimesh")}, "screenshots": []}
    result = [1]

    def stop():
        window._closing = True
        window.camera_preview.hide()
        window.close()
        app.quit()

    def wait_for_candidate(anchor=None, timeout=20.0):
        """The preview searches on a worker thread, so wait for the result instead of its return."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            candidate = state.candidate
            if candidate is not None and (anchor is None or candidate.anchor_key == anchor):
                return candidate
            app.processEvents()
            time.sleep(0.02)
        return None

    def finish():
        try:
            window.camera_preview.move(window.x() + 90, window.y() + 125)
            window.camera_preview.show_preview()
            app.processEvents()
            window.grab().save(str(destination / "workspace-scene.png"))
            window.camera_preview.grab().save(str(destination / "camera-preview.png"))
            scene_image = window.scene_view.canvas.render()
            camera_image = window.camera_preview.canvas.render()
            write_png(str(destination / "scene-canvas.png"), scene_image)
            write_png(str(destination / "camera-canvas.png"), camera_image)
            evidence["screenshots"].extend(["workspace-scene.png", "camera-preview.png", "scene-canvas.png", "camera-canvas.png"])
            evidence["candidate"] = candidate_to_dict(state.candidate)
            evidence["mesh_count"] = len(state.meshes)
            evidence["object_ids"] = [t.obj_id for t in state.config.targets if t.enabled]
            evidence["camera_pixels_range"] = int(camera_image[..., :3].max()) - int(camera_image[..., :3].min())
            if evidence["camera_pixels_range"] < 10:
                raise RuntimeError("Camera framebuffer has no visible geometry contrast")
            current_config = state.config
            observer = window.scene_view.view.camera
            original_azimuth = observer.azimuth
            observer.azimuth += 12
            app.processEvents()
            if state.config is not current_config:
                raise RuntimeError("Observer camera navigation changed scientific settings")
            observer.azimuth = original_azimuth
            evidence["observer_navigation_preserves_config"] = True
            from bproc_ui.views.scene_view import placed_content_bounds
            bounds = placed_content_bounds(state.candidate, state.meshes)
            observer.center = (9., 9., 9.)
            window.scene_view.fit_scene()
            app.processEvents()
            expected_center = [float(v) for v in (bounds[0] + bounds[1]) / 2]
            if not np.allclose(observer.center, expected_center, atol=1e-6):
                raise RuntimeError(f"Home did not frame the placed content: {observer.center} != {expected_center}")
            evidence["home_frames_union_center"] = expected_center
            original_material = state.config.material
            state.update(material=replace(original_material, color="solid", base_color="#ff0000"))
            app.processEvents()
            scene_image = window.scene_view.canvas.render()
            camera_image = window.camera_preview.canvas.render()
            def red_pixels(image):
                return int(np.count_nonzero((image[..., 0] > 140) & (image[..., 1] < 90) & (image[..., 2] < 90)))
            scene_red, camera_red = red_pixels(scene_image), red_pixels(camera_image)
            if min(scene_red, camera_red) < 50:
                raise RuntimeError(f"solid base colour did not reach both viewports: scene={scene_red} camera={camera_red}")
            evidence["solid_colour_pixels"] = {"scene": scene_red, "camera": camera_red}
            write_png(str(destination / "scene-solid-red.png"), scene_image)
            write_png(str(destination / "camera-solid-red.png"), camera_image)
            evidence["screenshots"].extend(["scene-solid-red.png", "camera-solid-red.png"])
            state.update(material=original_material)
            app.processEvents()

            # Viewport LOD control: full/auto must be display-only and keep canonical meshes.
            canonical = {key: (len(mesh.faces), id(mesh)) for key, mesh in state.meshes.items()}
            auto_status = window.lod_status.text()
            window.lod_mode.setCurrentIndex(window.lod_mode.findData("full"))
            app.processEvents()
            full_status = window.lod_status.text()
            if state.config.viewport.lod_mode != "full" or "原始网格" not in full_status:
                raise RuntimeError(f"LOD full control did not reach the config/status: {full_status}")
            if {key: (len(mesh.faces), id(mesh)) for key, mesh in state.meshes.items()} != canonical:
                raise RuntimeError("LOD mode changed the canonical meshes")
            window._set_workspace(0)
            window.grab().save(str(destination / "workspace-lod-full.png"))
            evidence["screenshots"].append("workspace-lod-full.png")
            window.lod_mode.setCurrentIndex(window.lod_mode.findData("auto"))
            app.processEvents()
            evidence["lod"] = {"auto_status": auto_status, "full_status": full_status,
                               "canonical_unchanged": True}

            # Cycles image tiling and grouping controls must reach the project and the snapshot.
            panel = window.export_panel
            panel.use_tiling.setChecked(False)
            if state.config.render.use_tiling is not False:
                raise RuntimeError("tiling checkbox did not reach the render config")
            panel.use_tiling.setChecked(True)
            panel.tile_size.setValue(512)
            panel.split_scales.setChecked(True)
            panel.reframe_groups.setChecked(True)
            panel.framing_margin.setValue(0.15)
            if (state.config.render.use_tiling is not True or state.config.render.tile_size != 512
                    or state.config.scale_grouping.reframe is not True
                    or abs(state.config.scale_grouping.margin_fraction - .15) > 1e-9):
                raise RuntimeError("tiling/grouping controls did not reach the project config")
            window._set_workspace(2)
            app.processEvents()
            properties_scroll = window.properties.widget(2)
            bar = properties_scroll.verticalScrollBar()
            for fraction in (0.0, 0.33, 0.66, 1.0):
                bar.setValue(int(bar.maximum() * fraction))
                app.processEvents()
                name = f"workspace-export-scroll-{int(round(fraction * 100)):03d}.png"
                window.grab().save(str(destination / name))
                evidence["screenshots"].append(name)
            window.grab().save(str(destination / "workspace-render-tiling.png"))
            evidence["screenshots"].append("workspace-render-tiling.png")
            evidence["tiling_controls"] = {"use_tiling": state.config.render.use_tiling,
                                           "tile_size": state.config.render.tile_size}
            evidence["grouping_controls"] = {"reframe": state.config.scale_grouping.reframe,
                                             "margin": state.config.scale_grouping.margin_fraction,
                                             "note": panel.grouping_note.text()}
            canvas = window.camera_preview.canvas
            window.camera_preview.close()
            window.camera_preview.show_preview()
            if window.camera_preview.canvas is not canvas:
                raise RuntimeError("Reopening the camera replaced its OpenGL canvas")
            evidence["camera_reopen_reuses_canvas"] = True
            alternatives = [target.asset.key for target in state.config.targets
                            if target.enabled and target.asset.key != state.config.anchor_key]
            if alternatives:
                original_anchor = state.config.anchor_key
                state.set_anchor(alternatives[0])
                window.preview.refresh()
                if wait_for_candidate(alternatives[0]) is None:
                    raise RuntimeError("Center switch could not produce a candidate: " + window.preview_state.text())
                app.processEvents()
                expected_name = next(target.name for target in state.config.targets if target.asset.key == alternatives[0])
                evidence["alternate_center_caption"] = window.camera_preview.caption.text()
                if expected_name not in window.camera_preview.caption.text():
                    raise RuntimeError("Camera caption did not follow the selected center")
                window.camera_preview.grab().save(str(destination / "camera-alternate-center.png"))
                evidence["alternate_center"] = state.candidate.anchor_key
                evidence["screenshots"].append("camera-alternate-center.png")
                state.set_anchor(original_anchor)
                window.preview.refresh()
            window._set_workspace(1)
            app.processEvents()
            window.grab().save(str(destination / "workspace-sampling.png"))
            window._set_workspace(2)
            app.processEvents()
            window.grab().save(str(destination / "workspace-export.png"))
            jobs_root = destination / ("jobs-" + str(time.time_ns()))
            evidence["exported_jobs"] = {}
            for mode in ("rgb_masks", "bop"):
                state.update(output=replace(state.config.output, root=destination / "render-output", dataset_name="gui-" + mode))
                window.export_panel.choose_format(mode)
                receipt = window.export_panel.export_to(jobs_root / mode)
                exported = load_project(receipt.directory / "scene.yaml")
                if exported.output.format != mode:
                    raise RuntimeError("UI output selection did not reach exported YAML")
                if (exported.render.use_tiling is not True or exported.render.tile_size != 512
                        or exported.viewport.lod_mode != "auto"
                        or exported.scale_grouping.reframe is not True
                        or abs(exported.scale_grouping.margin_fraction - .15) > 1e-9):
                    raise RuntimeError("new viewport/tiling/grouping fields did not reach the export snapshot")
                evidence["exported_jobs"][mode] = str(receipt.directory)
                window.preview.refresh()
                properties_scroll = window.properties.widget(2)
                properties_scroll.verticalScrollBar().setValue(properties_scroll.verticalScrollBar().maximum())
                app.processEvents()
                screenshot = f"workspace-export-{mode}.png"
                window.grab().save(str(destination / screenshot))
                evidence["screenshots"].append(screenshot)
            heartbeat_ticks = []
            heartbeat = QTimer()
            heartbeat.setInterval(5)
            heartbeat.timeout.connect(lambda: heartbeat_ticks.append(1))
            heartbeat.start()
            window.export_panel.job_path.setText(str(destination / "gui-async-job"))
            started_export = time.monotonic()
            window.export_panel.export_button.click()
            if window.export_panel.export_button.text() != "正在导出…":
                raise RuntimeError("the export button did not enter its busy state")
            deadline = time.monotonic() + 180
            while window.export_panel._export_task is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            heartbeat.stop()
            elapsed_export = time.monotonic() - started_export
            if window.export_panel.last_result is None:
                raise RuntimeError("async export produced no result: " + window.export_panel.validation.text())
            if not heartbeat_ticks:
                raise RuntimeError(f"export blocked the Qt event loop for {elapsed_export:.2f}s")
            evidence["async_export"] = {"heartbeat_ticks": len(heartbeat_ticks),
                                        "seconds": round(elapsed_export, 2),
                                        "directory": str(window.export_panel.last_result.directory)}
            saved = destination / "gui-saved.yaml"
            state.save(saved)
            if load_project(saved) != state.config:
                raise RuntimeError("GUI project save did not round-trip")
            evidence["project_save_roundtrip"] = True
            window.resize(1024, 768)
            window._set_workspace(0)
            app.processEvents()
            window.grab().save(str(destination / "workspace-1024.png"))
            evidence["screenshots"].extend(["workspace-sampling.png", "workspace-export.png", "workspace-1024.png"])
            evidence["status"] = "passed"
            result[0] = 0
        except Exception as error:
            evidence["status"] = "failed"
            evidence["error"] = repr(error)
            print(f"GUI smoke failed: {error}", file=sys.stderr)
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (destination / "gui-manifest.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        stop()

    def poll():
        if state.candidate is not None:
            QTimer.singleShot(400, finish)
        elif time.monotonic() - started > args.timeout:
            evidence.update(status="failed", error=window.preview_state.text())
            (destination / "gui-manifest.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("GUI smoke timed out: " + window.preview_state.text(), file=sys.stderr)
            stop()
        else:
            QTimer.singleShot(100, poll)

    QTimer.singleShot(300, poll)
    app.exec()
    return result[0]


if __name__ == "__main__":
    raise SystemExit(main())
