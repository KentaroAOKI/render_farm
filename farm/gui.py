from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import threading

from PySide6 import QtCore, QtGui, QtWidgets as W

from .assets import enqueue, refresh_scene_list as refresh_project, validate_settings, stored_job_metadata
from .common import FileLock, atomic_json, command_flags, installations, read_json, runtime_env, worker_dir, machine_id, python_command, inside
from .store import Store, ACTIVE
from .worker import diagnose, worker_config
from .metrics import display_metrics, worker_unresponsive
from .late_post import published_groups, run_late_postprocess

LABELS = {"unrendered": "未レンダリング", "updated": "更新あり・未レンダリング", "queued": "キュー投入済み",
    "pending": "待機", "held": "保留", "paused": "停止中", "claimed": "取得済み", "opening": "読み込み中",
    "rendering": "レンダリング中", "validating": "検証中", "postprocessing": "後処理中", "publishing": "公開中",
    "restarting": "Maya再起動中", "waiting_io": "接続待機", "complete": "完了", "failed": "失敗",
    "deleted": "削除済み", "missing": "元ファイルなし", "error": "エラー", "idle": "待機", "working": "処理中",
    "stopped": "停止", "diagnosing": "環境診断中"}


class Task(QtCore.QThread):
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(object)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.succeeded.emit(self.fn())
        except Exception:
            self.failed.emit(traceback.format_exc())


def selected(table):
    return [table.item(row, 0).data(QtCore.Qt.UserRole) for row in sorted({index.row() for index in table.selectedIndexes()})]


def table(headers):
    t = W.QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(W.QAbstractItemView.SelectRows)
    t.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
    t.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().hide()
    t.setAlternatingRowColors(True)
    return t


def populate(t, rows, row_colors=None):
    chosen = set(selected(t))
    t.setRowCount(len(rows))
    for row, (identity, values) in enumerate(rows):
        for col, value in enumerate(values):
            item = W.QTableWidgetItem(str(value if value is not None else ""))
            item.setToolTip(item.text())
            item.setData(QtCore.Qt.UserRole, identity)
            color = (row_colors or {}).get(identity)
            if color:
                item.setBackground(QtGui.QColor(color))
                item.setForeground(QtGui.QColor("#202124"))
            t.setItem(row, col, item)
        if identity in chosen:
            for col in range(t.columnCount()):
                t.item(row, col).setSelected(True)
    t.resizeColumnsToContents()
    for col in range(t.columnCount()):
        t.setColumnWidth(col, min(t.columnWidth(col), 300))


class JobDialog(W.QDialog):
    def __init__(self, scene, defaults, parent):
        super().__init__(parent)
        self.setWindowTitle("レンダリング設定 — " + scene["name"])
        self.resize(680, 720)
        self.metadata = stored_job_metadata(scene)
        settings = {**self.metadata["settings"], **defaults}
        layout = W.QVBoxLayout(self)
        layout.addWidget(W.QLabel("シーン調査時: Maya " + scene["maya_version"] + " / 元シーンは変更しません"))
        form = W.QFormLayout()
        self.fields = {}
        self.maya_version = W.QComboBox()
        self.maya_version.addItem("指定しない（利用可能なMayaを使用）", None)
        for version in ("2025", "2026", "2027"):
            self.maya_version.addItem("Maya " + version + " を指定", version)
        form.addRow("実行するMayaバージョン", self.maya_version)
        ranges = self.metadata.get("ranges", {})
        self.range_source = W.QComboBox()
        for key, label in [("playback", "再生範囲"), ("animation", "アニメーション全体範囲"), ("render", "Render Settings範囲")]:
            if key in ranges:
                self.range_source.addItem(label, key)
        self.range_source.addItem("手動指定", "manual")
        form.addRow("レンダリング範囲", self.range_source)
        summary = W.QLabel("\n".join(
            f"{label}: {ranges[key]['start']:g} ～ {ranges[key]['end']:g} / 刻み {ranges[key]['step']:g}"
            for key, label in [("playback", "再生"), ("animation", "全体"), ("render", "Render Settings")] if key in ranges)
            + f"\n時間単位: {self.metadata.get('time_unit', '不明')} / {settings['fps']} FPS"
            + f" / 解像度: {settings['width']} × {settings['height']}")
        summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(summary)
        for key, label in [("start", "開始フレーム"), ("end", "終了フレーム"), ("step", "フレーム間隔"),
                           ("width", "幅"), ("height", "高さ"), ("aa", "Camera AA"), ("restart_limit", "同一フレームの再起動上限")]:
            spin = W.QDoubleSpinBox() if key in {"start", "end", "step"} else W.QSpinBox()
            if isinstance(spin, W.QDoubleSpinBox):
                spin.setDecimals(4)
            spin.setRange(-1000000 if key in {"start", "end"} else 0, 1000000)
            spin.setValue(settings.get(key, 3))
            self.fields[key] = spin
            form.addRow(label, spin)
        def change_range():
            source = self.range_source.currentData()
            if source in ranges:
                for key in ("start", "end", "step"):
                    self.fields[key].setValue(ranges[source][key])
        self.range_source.setCurrentIndex(max(0, self.range_source.findData(settings.get("range_source", "manual"))))
        for key in ("start", "end", "step"):
            self.fields[key].valueChanged.connect(lambda: self.range_source.setCurrentIndex(self.range_source.findData("manual"))
                                                  if not self.range_source.signalsBlocked() else None)
        # Programmatic range selection must preserve its source while updating fields.
        def select_range():
            self.range_source.blockSignals(True)
            try:
                change_range()
            finally:
                self.range_source.blockSignals(False)
        self.range_source.currentIndexChanged.connect(select_range)
        timeout = W.QDoubleSpinBox()
        timeout.setRange(.1, 100000)
        timeout.setValue(float(settings.get("timeout_minutes", 30)))
        self.fields["timeout_minutes"] = timeout
        form.addRow("画像生成タイムアウト（分）", timeout)
        fps = W.QLineEdit(str(settings["fps"]))
        self.fields["fps"] = fps
        form.addRow("FPS（24000/1001 等も可）", fps)
        for key, label, choices in [("device", "レンダーデバイス", ["CPU", "GPU"]),
                                    ("format", "画像形式", ["png", "exr"]),
                                    ("oidn", "OIDN（scene=シーン設定）", ["scene", "on", "off"])]:
            combo = W.QComboBox()
            combo.addItems(choices)
            combo.setCurrentText(str(settings.get(key, choices[0])))
            self.fields[key] = combo
            form.addRow(label, combo)
        adaptive = W.QCheckBox("アダプティブサンプリング")
        adaptive.setChecked(settings.get("adaptive", False))
        self.fields["adaptive"] = adaptive
        form.addRow(adaptive)
        layout.addLayout(form)
        tabs = W.QTabWidget()
        for key, label in [("cameras", "カメラ"), ("layers", "レイヤー"), ("aovs", "AOV")]:
            checklist = W.QListWidget()
            for item in self.metadata.get(key, []):
                label_text = item["name"]
                if key == "cameras":
                    label_text += " / renderable=" + str(item.get("renderable", item.get("enabled", False)))
                    if item.get("default"):
                        label_text += "（デフォルト）"
                entry = W.QListWidgetItem(label_text)
                entry.setData(QtCore.Qt.UserRole, item["name"])
                entry.setFlags(entry.flags() | QtCore.Qt.ItemIsUserCheckable)
                entry.setCheckState(QtCore.Qt.Checked if item["name"] in settings.get(key, []) else QtCore.Qt.Unchecked)
                checklist.addItem(entry)
            self.fields[key] = checklist
            tabs.addTab(checklist, label)
        layout.addWidget(tabs)
        layout.addWidget(W.QLabel("開始・終了・刻みは整数で指定してください（小数フレームのレンダーは未対応）。"))
        layout.addWidget(W.QLabel("Beautyは上記の画像形式、AOVはデータを保持するEXRで出力します。"))
        self.video = W.QCheckBox("Beauty連番をFFV1動画化（PNGのみ）")
        self.denoise = W.QCheckBox("FastDVDnet sigma 10（PNGのみ・追加環境が必要）")
        layout.addWidget(self.video)
        layout.addWidget(self.denoise)
        preset = W.QPushButton("参照プリセット：GPU / AA6 / OIDN / FastDVDnet / FFV1")
        preset.clicked.connect(self.preset)
        layout.addWidget(preset)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Ok).setText("キューに投入")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        try:
            validate_settings(self.values(), self.metadata)
        except (ValueError, TypeError) as exc:
            W.QMessageBox.warning(self, "レンダリング設定を確認してください", str(exc))
            return
        super().accept()

    def preset(self):
        self.fields["device"].setCurrentText("GPU")
        self.fields["format"].setCurrentText("png")
        self.fields["oidn"].setCurrentText("on")
        self.fields["aa"].setValue(6)
        self.fields["adaptive"].setChecked(False)
        self.video.setChecked(True)
        self.denoise.setChecked(True)

    def values(self):
        result = {"range_source": self.range_source.currentData()}
        for key, widget in self.fields.items():
            if isinstance(widget, W.QListWidget):
                result[key] = [widget.item(i).data(QtCore.Qt.UserRole) for i in range(widget.count()) if widget.item(i).checkState() == QtCore.Qt.Checked]
            elif isinstance(widget, W.QComboBox):
                result[key] = widget.currentText()
            elif isinstance(widget, W.QCheckBox):
                result[key] = widget.isChecked()
            elif isinstance(widget, W.QLineEdit):
                result[key] = widget.text()
            else:
                result[key] = widget.value()
        result["post"] = {"video": self.video.isChecked(), "fastdvdnet": self.denoise.isChecked()}
        return result


class Dashboard(W.QMainWindow):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root).resolve()
        self.store = Store(root)
        self.tasks = []
        self.scanning = False
        self.registering_project = False
        self.preparing_scenes = False
        self.scan_result_received = False
        self.scan_event = {}
        self.scan_started = None
        self.scan_phase_started = None
        self.scan_project_started = None
        self.worker_process = None
        self.setWindowTitle("Maya レンダーファーム — " + str(self.root))
        self.resize(1350, 850)
        central = W.QWidget()
        layout = W.QVBoxLayout(central)
        self.summary = W.QLabel()
        self.summary.setStyleSheet("font-size: 19px; font-weight: 600; padding: 12px")
        layout.addWidget(self.summary)
        self.tabs = W.QTabWidget()
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.scenes = table(["シーン", "プロジェクト / Maya", "採用ファイル", "更新日時", "候補数", "更新・状態", "進捗", "担当PC", "経過", "再起動", "エラー",
                             "再生範囲", "アニメーション全体", "Render Settings範囲", "レンダー刻み", "時間単位 / FPS", "解像度", "非デフォルトカメラ / renderable", "警告", "シーン読込時間", "最長フレーム時間"])
        self.jobs = table(["ジョブ / シーン", "Maya", "状態", "進捗", "担当PC", "経過", "再起動", "エラー", "シーン読込時間", "最長フレーム時間"])
        self.workers = table(["PC", "状態", "Maya", "最終応答", "ジョブ", "エラー",
                              "工程", "進捗", "完了率", "現在のフレーム", "レイヤー / カメラ", "経過", "現在フレーム経過", "シーン読込時間", "最長フレーム時間"])
        self.scene_page = self.page(self.scenes, [("プロジェクトを登録", self.add_project), ("更新を確認", self.scan),
            ("シーン情報を強制再取得", lambda: self.scan(force=True)),
            ("プロジェクト登録を解除", self.remove_project),
            ("選択シーンを投入", self.enqueue_selected), ("直近の結果を開く", self.open_result), ("詳細", self.details)])
        self.scan_label = W.QLabel("プロジェクト調査：待機中")
        self.scan_label.setTextFormat(QtCore.Qt.PlainText)
        self.scan_label.setWordWrap(True)
        self.scan_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.scan_bar = W.QProgressBar()
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scene_page.layout().insertWidget(1, self.scan_label)
        self.scene_page.layout().insertWidget(2, self.scan_bar)
        preview_row = W.QHBoxLayout()
        self.preview_button = W.QPushButton("結果HTML・全動画を連結")
        self.preview_button.clicked.connect(self.create_results_dashboard)
        preview_row.addWidget(self.preview_button)
        preview_open = W.QPushButton("結果HTMLを開く")
        preview_open.clicked.connect(self.open_results_dashboard)
        preview_row.addWidget(preview_open)
        preview_row.addWidget(W.QLabel("連番FPS"))
        self.preview_fps = W.QLineEdit("24")
        self.preview_fps.setMaximumWidth(70)
        preview_row.addWidget(self.preview_fps)
        self.preview_cancel = threading.Event()
        self.preview_stop = W.QPushButton("HTML作成を停止")
        self.preview_stop.setEnabled(False)
        self.preview_stop.clicked.connect(self.preview_cancel.set)
        preview_row.addWidget(self.preview_stop)
        self.preview_label = W.QLabel("結果HTML：未作成")
        preview_row.addWidget(self.preview_label, 1)
        self.preview_bar = W.QProgressBar()
        self.preview_bar.setMaximumWidth(180)
        preview_row.addWidget(self.preview_bar)
        self.scene_page.layout().insertLayout(3, preview_row)
        self.scan_display_timer = QtCore.QTimer(self)
        self.scan_display_timer.timeout.connect(self.update_scan_display)
        self.scan_display_timer.start(1000)
        self.tabs.addTab(self.scene_page, "シーン一覧")
        self.job_page = self.page(self.jobs, [("上へ", lambda: self.job_action("up")), ("下へ", lambda: self.job_action("down")),
            ("保留 / 停止", lambda: self.job_action("hold")), ("再開", lambda: self.job_action("resume")),
            ("再試行", lambda: self.job_action("retry")), ("削除", lambda: self.job_action("delete")),
            ("後処理設定を変更", self.edit_job_post),
            ("画像フォルダを開く", self.open_job_images), ("ログ・作業領域", self.open_work), ("詳細", self.details)])
        post_row = W.QHBoxLayout()
        self.late_post_button = W.QPushButton("後から動画化・デノイズ")
        self.late_post_button.clicked.connect(self.late_post_selected)
        post_row.addWidget(self.late_post_button)
        open_post = W.QPushButton("後処理結果を開く")
        open_post.clicked.connect(self.open_post_result)
        post_row.addWidget(open_post)
        self.late_post_cancel = threading.Event()
        self.late_post_stop = W.QPushButton("後処理を停止")
        self.late_post_stop.setEnabled(False)
        self.late_post_stop.clicked.connect(lambda: self.late_post_cancel.set())
        post_row.addWidget(self.late_post_stop)
        post_row.addStretch()
        self.job_page.layout().insertLayout(1, post_row)
        self.late_post_label = W.QLabel("追加後処理：待機中")
        self.late_post_label.setWordWrap(True)
        self.late_post_bar = W.QProgressBar()
        self.late_post_bar.setValue(0)
        self.job_page.layout().insertWidget(2, self.late_post_label)
        self.job_page.layout().insertWidget(3, self.late_post_bar)
        self.history = W.QCheckBox("削除済みも表示")
        self.history.toggled.connect(self.refresh)
        self.job_page.layout().insertWidget(1, self.history)
        self.tabs.addTab(self.job_page, "ジョブキュー")
        self.tabs.addTab(self.page(self.workers, [("このPCで処理開始", self.start_worker), ("このPCをフレーム後に停止", self.stop_worker),
            ("環境診断", self.diagnose), ("停止済みPCのジョブを回収", self.recover)]), "PCの状態")
        self.build_package_page()
        self.build_settings_page()
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(2000)
        self.scan_timer = QtCore.QTimer(self)
        # Scene files are inspected only by the explicit update action.
        self.refresh()

    def page(self, widget, actions):
        page = W.QWidget()
        page.action_buttons = {}
        layout = W.QVBoxLayout(page)
        row = W.QHBoxLayout()
        for text, fn in actions:
            button = W.QPushButton(text)
            button.clicked.connect(fn)
            page.action_buttons[text] = button
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(widget)
        return page

    def background(self, fn, label, callback=None, quiet=False, progress=None, finished=None, failed=None):
        task = Task(fn, self)
        if progress:
            task.fn = lambda: fn(task.progress.emit)
            task.progress.connect(progress)
        if finished:
            task.finished.connect(finished)
        self.tasks.append(task)
        self.statusBar().showMessage(label)
        def succeeded(value):
            self.statusBar().showMessage(label + "：完了", 15000)
            if callback:
                callback(value)
            self.refresh()
        task.succeeded.connect(succeeded)
        task.failed.connect(lambda error: self.statusBar().showMessage(error.splitlines()[-1], 30000) if quiet else W.QMessageBox.critical(self, label, error))
        if failed:
            task.failed.connect(failed)
        task.finished.connect(lambda: self.tasks.remove(task))
        task.start()
        return task

    def refresh(self):
        import time
        try:
            state = self.store.read()
            self.state = state
            jobs = [display_metrics(self.root, job) for job in state["jobs"].values()]
            now = time.time()
            stale = {name: worker_unresponsive(worker, now) for name, worker in state["workers"].items()}
            def unresponsive(job):
                return job.get("state") in ACTIVE and (stale.get(job.get("owner"), True)
                    or state["workers"].get(job.get("owner"), {}).get("state") == "stopped")
            running = sum(j["state"] in ACTIVE and not unresponsive(j) for j in jobs)
            uncertain = sum(unresponsive(j) for j in jobs)
            pending = sum(j["state"] == "pending" for j in jobs)
            visible = [scene for scene in state["scenes"].values() if not scene.get("superseded")]
            self.summary.setText(f"Maya Render Farm    応答中の実行 {running}   更新なし {uncertain}   待機 {pending}   シーン {len(visible)}")
            rows = []
            scene_colors = {}
            for scene in sorted(visible, key=lambda s: s["name"]):
                relevant = [j for j in jobs if j["scene_id"] == scene["id"] and j["scene_revision"] == scene["revision"] and j["state"] != "deleted"]
                j = max(relevant, key=lambda j: j["created"]) if relevant else {}
                status = j.get("state", scene["state"]) if scene["state"] not in {"updated", "missing", "error"} else scene["state"]
                stale_job = unresponsive(j)
                if status in {"failed", "error", "missing"}:
                    scene_colors[scene["id"]] = "#FDE2E2"
                elif stale_job:
                    scene_colors[scene["id"]] = "#E5E5E5"
                elif status in ACTIVE:
                    scene_colors[scene["id"]] = "#DDEEFF"
                elif status in {"pending", "queued", "held", "paused"}:
                    scene_colors[scene["id"]] = "#FFF2CC"
                info = scene.get("metadata", {})
                ranges = info.get("ranges", {})
                def range_text(key):
                    r = ranges.get(key, {})
                    return f"{r['start']:g} ～ {r['end']:g}" if "start" in r and "end" in r else ""
                resolution = info.get("resolution", {})
                camera_text = "; ".join(c.get("transform", c["name"]) + " / renderable=" + str(c.get("renderable", c.get("enabled", False)))
                                        for c in info.get("non_default_cameras", []))
                rows.append((scene["id"], [Path(scene.get("logical", scene["name"])).name, state["projects"][scene["project_id"]]["name"] + " / " + scene["maya_version"],
                    scene["relative"], time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(scene["source_mtime"])) if scene.get("source_mtime") else "",
                    scene.get("candidate_count", ""),
                    ("更新なし（最終記録: " + LABELS.get(status, status) + "）" if stale_job else LABELS.get(status, status)) + ("（日時確認のみ・詳細未確認）" if info.get("inspection") == "lightweight" else "（情報取得済み・参照なし）" if info.get("inspection") == "catalog" else ""),
                    self.progress(j), j.get("owner", ""), self.elapsed(j), sum(j.get("restarts", {}).values()), scene.get("error") or j.get("error", ""),
                    range_text("playback"), range_text("animation"), range_text("render"), ranges.get("render", {}).get("step", ""),
                    (str(info.get("time_unit", "")) + " / " + str(info.get("settings", {}).get("fps", ""))) if info.get("time_unit") else "",
                    f"{resolution['width']} × {resolution['height']}" if "width" in resolution and "height" in resolution else "",
                    camera_text, "\n".join(info.get("warnings", [])), self.loading_time(j), self.duration(j.get("max_frame_seconds"))]))
            populate(self.scenes, rows, scene_colors)
            populate(self.jobs, [(j["id"], [j["name"] + " / " + j["id"][:8],
                j["maya_version"] if j.get("maya_version_required", True) else
                ("指定なし → " + j["maya_version"] if j.get("started") else "指定なし"),
                ("更新なし（最終記録: " + LABELS.get(j["state"], j["state"]) + "）" if unresponsive(j) else LABELS.get(j["state"], j["state"])) + ("（停止要求中）" if j.get("control") else ""),
                self.progress(j), j.get("owner"), self.elapsed(j), sum(j.get("restarts", {}).values()), j.get("error"),
                self.loading_time(j), self.duration(j.get("max_frame_seconds"))])
                for j in sorted(jobs, key=lambda j: j["order"]) if self.history.isChecked() or j["state"] != "deleted"])
            jobs_by_id = {job["id"]: job for job in jobs}
            worker_rows = []
            for name, w in state["workers"].items():
                job = jobs_by_id.get(w.get("job"), {})
                if job.get("owner") != name or w.get("state") != "working":
                    job = {}
                offline = stale[name]
                total, done = job.get("total", 0), job.get("completed", 0)
                rendering = job.get("state") == "rendering" and not offline
                worker_rows.append((name, [name, ("更新なし（最終記録: " + LABELS.get(w.get("state"), w.get("state", "不明")) + "）") if offline else LABELS.get(w.get("state"), w.get("state")), ", ".join(w.get("capabilities", {})),
                    str(int(time.time() - w["updated"])) + "秒前", job.get("name", (w.get("job") or "")[:8]), str(w.get("error") or job.get("error") or ""),
                    "更新なし" if offline else (job.get("maya_phase") if job.get("state") == "opening" else "") or LABELS.get(job.get("state"), LABELS.get(w.get("state"), "")),
                    self.progress(job) if job else "—", f"{min(100, done / total * 100):.1f}%" if total else "—",
                    job.get("frame", "") if rendering else "", (str(job.get("layer", "")) + " / " + str(job.get("camera", ""))) if rendering else "",
                    self.elapsed(job) if job else "—", self.duration(time.time() - job["frame_started"]) if rendering and job.get("frame_started") else "—",
                    self.loading_time(job) if job else "—", self.duration(job.get("max_frame_seconds")) if job else "—"]))
            populate(self.workers, worker_rows)
            errors = [p["name"] + ": " + p["error"] for p in state["projects"].values() if p.get("error")]
            if errors:
                self.statusBar().showMessage(" / ".join(errors))
        except Exception as exc:
            self.statusBar().showMessage("共有先を確認できません: " + str(exc))

    @staticmethod
    def progress(job):
        return str(job.get("completed", 0)) + " / " + str(job.get("total", "?"))

    @staticmethod
    def duration(seconds):
        if seconds is None:
            return "未計測"
        seconds = max(0, int(seconds))
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

    @classmethod
    def loading_time(cls, job):
        if job.get("loading_seconds") is not None:
            return cls.duration(job["loading_seconds"])
        if job.get("loading_started"):
            end = time.time() if job.get("state") == "opening" else job.get("updated", job["loading_started"])
            return cls.duration(end - job["loading_started"]) + "（未完了）"
        return "未計測"

    @staticmethod
    def elapsed(job):
        seconds = int(job.get("elapsed", 0))
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

    def add_project(self):
        if self.scanning or self.registering_project or self.preparing_scenes:
            self.statusBar().showMessage("調査中はプロジェクトを登録できません。完了後に登録してください。", 10000)
            return
        self.registering_project = True
        self.update_project_actions()
        registered = False
        try:
            path = W.QFileDialog.getExistingDirectory(self, "Mayaプロジェクトを選択")
            if not path:
                return
            version, ok = W.QInputDialog.getItem(self, "Mayaバージョン", "このプロジェクトに使用するMaya", ["2025", "2026", "2027"], 2, False)
            if ok:
                self.store.add_project(path, version)
                registered = True
        except Exception as exc:
            W.QMessageBox.critical(self, "登録失敗", str(exc))
        finally:
            self.registering_project = False
            self.update_project_actions()
        if registered:
            self.refresh()
            self.statusBar().showMessage("登録しました。「更新を確認」でシーン一覧・情報を取得してください。", 15000)

    def update_project_actions(self):
        enabled = not (self.scanning or self.registering_project or self.preparing_scenes)
        for name in ("プロジェクトを登録", "プロジェクト登録を解除", "更新を確認", "シーン情報を強制再取得", "選択シーンを投入"):
            button = self.scene_page.action_buttons[name]
            button.setEnabled(enabled)
            button.setToolTip("" if enabled else "プロジェクトの調査・登録が終わるまでお待ちください。")

    def remove_project(self):
        if self.scanning or self.registering_project or self.preparing_scenes:
            return
        self.registering_project = True
        self.update_project_actions()
        try:
            projects = list(self.store.read()["projects"].values())
            if not projects:
                return
            labels = [p["name"] + " / Maya " + p["maya_version"] + " / " + str(self.store.project_path(p)) for p in projects]
            label, ok = W.QInputDialog.getItem(self, "プロジェクト登録を解除", "解除する登録先", labels, 0, False)
            if not ok:
                return
            project = projects[labels.index(label)]
            path = str(self.store.project_path(project))
            answer = W.QMessageBox.question(self, "登録解除の確認", path +
                "\n\nこのプロジェクトを調査対象とシーン一覧から外します。待機・保留・停止中のジョブは取り消します。"
                "\n元ファイル、完成画像、ログ、ジョブ履歴は保持します。実行中のジョブがある場合は解除できません。",
                W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No)
            if answer != W.QMessageBox.Yes:
                return
            result = self.store.remove_project(project["id"], expected_project=project)
            self.refresh()
            self.statusBar().showMessage(f"登録解除：{result['project']}（ジョブ{result['canceled_jobs']}件を取消）", 15000)
        except Exception as exc:
            W.QMessageBox.warning(self, "登録解除できません", str(exc))
        finally:
            self.registering_project = False
            self.update_project_actions()

    def scan(self, force=False):
        if self.scanning or self.registering_project or self.preparing_scenes:
            return
        # Fix the project list before starting the thread; later registrations belong to the next scan.
        projects = self.store.read()["projects"]
        if not projects:
            return
        self.scanning = True
        self.scan_result_received = False
        self.update_project_actions()
        self.scan_started = time.monotonic()
        self.scan_phase_started = self.scan_started
        self.scan_project_started = self.scan_started
        self.scan_event = {"phase": "調査を開始しています", "total": None}
        self.update_scan_display()
        def work(emit):
            result = {}
            last_emit = 0
            last_phase = None
            def report(event):
                nonlocal last_emit, last_phase
                now = time.monotonic()
                phase = (pid, event.get("phase"))
                if now - last_emit >= .15 or phase != last_phase:
                    emit({"project_index": project_index, "project_total": len(projects), "project_id": pid,
                          "project_root": str(self.store.project_path(projects[pid])), **event})
                    last_emit, last_phase = now, phase
            for project_index, pid in enumerate(projects, 1):
                report({"project": projects[pid]["name"], "phase": "プロジェクトの調査を開始", "total": None})
                try:
                    options = {"force": True} if force else {}
                    result[pid] = refresh_project(self.store, pid, worker_config(self.root), progress=report, **options)
                except (TimeoutError, Exception) as exc:
                    result[pid] = str(exc)
            return result
        self.background(work, "シーン情報を強制再取得中" if force else "プロジェクト更新を確認中", callback=self.scan_complete, quiet=True,
                        progress=self.scan_progress, finished=self.scan_finished)

    @QtCore.Slot(object)
    def scan_progress(self, event):
        if event.get("project_id") != self.scan_event.get("project_id"):
            self.scan_project_started = time.monotonic()
        if (event.get("phase"), event.get("scene"), event.get("project_id")) != (
                self.scan_event.get("phase"), self.scan_event.get("scene"), self.scan_event.get("project_id")):
            self.scan_phase_started = time.monotonic()
        self.scan_event = event
        self.update_scan_display()

    def update_scan_display(self):
        if not self.scanning or self.scan_result_received:
            return
        event = self.scan_event
        elapsed = int(time.monotonic() - self.scan_started)
        phase_elapsed = int(time.monotonic() - self.scan_phase_started)
        project_elapsed = int(time.monotonic() - self.scan_project_started)
        total, done = event.get("total"), event.get("completed", 0)
        count = f"{done:,} / {total:,} 件" if total is not None else f"{done:,} 件検出（総数を確認中）"
        title = f"プロジェクト {event.get('project_index', 0)} / {event.get('project_total', '?')}：{event.get('project', '')}"
        if event.get("project_root"):
            title += "\n登録先：" + event["project_root"]
        detail = (f"{event.get('phase', '')}　{count}　全体経過 {elapsed // 60}分{elapsed % 60:02}秒"
                  f"　このプロジェクト {project_elapsed // 60}分{project_elapsed % 60:02}秒（工程 {phase_elapsed}秒）")
        if event.get("file_size") is not None:
            detail += f"\n現在のファイル読み込み：{event.get('file_bytes', 0) / 1048576:,.1f} / {event['file_size'] / 1048576:,.1f} MiB"
        if event.get("scene"):
            detail += "\n対象シーン：" + event["scene"]
        self.scan_label.setText(title + "\n" + detail + "\n" + event.get("path", ""))
        self.scan_bar.setRange(0, total if total else 0)
        self.scan_bar.setValue(done)
        self.scan_bar.setFormat("現在の工程：%v / %m 件（%p%）")

    def scan_complete(self, result):
        self.scan_result_received = True
        errors = [str(value) for value in result.values() if isinstance(value, str)]
        elapsed = int(time.monotonic() - self.scan_started)
        self.scan_bar.setRange(0, 1)
        self.scan_bar.setValue(1)
        self.scan_bar.setFormat("調査終了（エラーあり）" if errors else "調査完了")
        self.scan_label.setText(f"プロジェクト調査：{len(result)}件終了　経過 {elapsed // 60}分{elapsed % 60:02}秒"
                                + ("\n" + "\n".join(errors) if errors else "\nシーン一覧に結果を反映しました。シーンごとのエラーも確認してください。"))

    def scan_finished(self):
        if self.scanning and not self.scan_result_received:
            self.scan_label.setText("プロジェクト調査を終了しました。画面下部のエラーを確認してください。")
            self.scan_bar.setRange(0, 1)
            self.scan_bar.setValue(0)
            self.scan_bar.setFormat("調査中断")
        self.scanning = False
        self.update_project_actions()

    def enqueue_selected(self):
        if self.scanning or self.registering_project or self.preparing_scenes:
            return
        ids = selected(self.scenes)
        if not ids:
            return
        self.preparing_scenes = True
        self.update_project_actions()
        try:
            self.refresh()
            self.enqueue_dialogs(ids)
        finally:
            self.preparing_scenes = False
            self.update_project_actions()

    def enqueue_dialogs(self, ids):
        entries = []
        for sid in ids:
            scene = self.state["scenes"][sid]
            if scene["state"] in {"missing", "error"}:
                W.QMessageBox.warning(self, "投入できません", scene.get("error") or "元ファイルがありません")
                continue
            try:
                stored_job_metadata(scene)
            except ValueError as exc:
                W.QMessageBox.warning(self, "投入できません", str(exc))
                continue
            dialog = JobDialog(scene, self.state["settings"], self)
            if dialog.exec() == W.QDialog.Accepted:
                entries.append((sid, dialog.values(), scene["revision"], dialog.maya_version.currentData()))
        if entries:
            self.background(lambda: [enqueue(self.store, sid, settings, prepared_revision=revision, maya_version=version)
                                     for sid, settings, revision, version in entries], "保存済み設定でキューに投入中")

    def edit_job_post(self):
        ids = selected(self.jobs)
        if len(ids) != 1:
            W.QMessageBox.information(self, "後処理設定", "変更するジョブを一つ選択してください。")
            return
        jid = ids[0]
        job = self.state["jobs"][jid]
        if job["state"] not in {"pending", "held", "paused", "failed"}:
            W.QMessageBox.warning(self, "後処理設定", "待機・保留・停止・エラーのジョブだけ変更できます。実行中は停止を確認してください。")
            return
        dialog = W.QDialog(self)
        dialog.setWindowTitle("後処理設定 — " + job.get("name", jid))
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(W.QLabel("保存済み画像は保持します。エラー・停止のジョブは保存後に再試行・再開してください。"))
        video = W.QCheckBox("FFV1動画を作成")
        denoise = W.QCheckBox("FastDVDnet sigma 10でデノイズ")
        options = job["settings"].get("post", {})
        video.setChecked(options.get("video", False))
        denoise.setChecked(options.get("fastdvdnet", False))
        layout.addWidget(video)
        layout.addWidget(denoise)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Save | W.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != W.QDialog.Accepted:
            return
        try:
            self.store.update_job_post(jid, {"video": video.isChecked(), "fastdvdnet": denoise.isChecked()}, job["revision"])
        except Exception as exc:
            W.QMessageBox.warning(self, "操作を反映できません", str(exc))
        self.refresh()

    def job_action(self, action):
        ids = selected(self.jobs)
        if not ids:
            return
        if action == "delete" and W.QMessageBox.question(self, "ジョブ削除", "選択ジョブをキューから削除します。実行中ジョブは停止します。画像・ログは保持されます。") != W.QMessageBox.Yes:
            return
        try:
            for jid in ids:
                self.store.action(jid, action, self.state["jobs"][jid]["revision"])
        except Exception as exc:
            W.QMessageBox.warning(self, "操作を反映できません", str(exc))
        self.refresh()

    def start_worker(self):
        directory = worker_dir(self.root)
        try:
            with FileLock(directory / "worker.lock", timeout=0):
                pass
        except TimeoutError:
            W.QMessageBox.information(self, "ワーカー", "このPCのワーカーは既に動作しています")
            return
        log = (directory / "supervisor.log").open("a", encoding="utf-8")
        self.worker_process = subprocess.Popen(python_command(sys.executable, Path(__file__).resolve().parent.parent / "farm_cli.py",
            ["--root", self.root, "worker"]), cwd=self.root, env=runtime_env(self.root), stdout=log, stderr=subprocess.STDOUT, **command_flags())
        log.close()
        self.statusBar().showMessage("ワーカーを起動しました。環境診断後にジョブを取得します")

    def stop_worker(self):
        atomic_json(worker_dir(self.root) / "worker_control.json", {"stop": True})
        self.statusBar().showMessage("現在のフレーム完了後に停止します。フリーズ時はタイムアウトで停止します")

    def diagnose(self):
        self.background(lambda: diagnose(self.root), "環境診断中", lambda result: self.show_text("環境診断", result))

    def recover(self):
        candidates = [j for j in self.store.read()["jobs"].values() if j["state"] in ACTIVE]
        if not candidates:
            return
        labels = [j["name"] + " / " + j["id"][:8] + " / " + str(j["owner"]) for j in candidates]
        label, ok = W.QInputDialog.getItem(self, "停止済みPCのジョブを回収", "対象ジョブ", labels, 0, False)
        if not ok:
            return
        job = candidates[labels.index(label)]
        if W.QMessageBox.question(self, "旧PCの停止確認", "元のPCのワーカーと担当Mayaが停止していることを確認しましたか？\n確認後、所有権を解除して保留状態に戻します。") != W.QMessageBox.Yes:
            return
        try:
            with FileLock(self.root / "data" / ("run_" + job["id"] + ".lock"), timeout=0):
                with self.store.transaction() as state:
                    current = state["jobs"][job["id"]]
                    if current["token"] != job["token"]:
                        raise ValueError("所有権が変更されています")
                    current.update(state="paused", token=None, control=None, error="手動回収。再開操作で途中から続行できます")
                    current["revision"] += 1
        except Exception as exc:
            W.QMessageBox.warning(self, "回収できません", str(exc))
        self.refresh()

    def open_result(self):
        for sid in selected(self.scenes):
            path = self.state["scenes"][sid].get("latest")
            if path:
                os.startfile(str(self.root / path))

    def open_work(self):
        for jid in selected(self.jobs):
            path = self.state["jobs"][jid].get("work")
            if path:
                os.startfile(str(self.root / path))

    def open_job_images(self):
        ids = selected(self.jobs)
        if not ids:
            W.QMessageBox.information(self, "画像フォルダ", "ジョブを選択してください。")
            return
        state = self.store.read()
        opened = set()
        unavailable = []
        for jid in ids:
            job = state["jobs"].get(jid, {})
            base = job.get("publication") if job.get("state") == "complete" else job.get("work")
            name = state["scenes"].get(job.get("scene_id"), {}).get("name", jid)
            if not base:
                unavailable.append(name + "：画像フォルダはまだ作成されていません。")
                continue
            try:
                directory = inside(self.root, base) / "raw"
                if not directory.is_dir():
                    unavailable.append(name + "：画像フォルダが存在しません。")
                    continue
                if directory not in opened:
                    os.startfile(str(directory))
                    opened.add(directory)
            except (OSError, ValueError) as exc:
                unavailable.append(name + "：" + str(exc))
        if unavailable:
            W.QMessageBox.information(self, "画像フォルダ", "\n".join(unavailable))

    def open_results_dashboard(self):
        path = self.root / "results/dashboard.html"
        if not path.is_file():
            W.QMessageBox.information(self, "結果HTML", "先に「結果HTML・全動画を連結」を実行してください。")
            return
        os.startfile(str(path))

    def create_results_dashboard(self):
        from .results_dashboard import build_dashboard
        config = {**worker_config(self.root), 'sequence_fps': self.preview_fps.text().strip()}
        self.preview_cancel.clear()
        self.preview_button.setEnabled(False)
        self.preview_stop.setEnabled(True)
        self.preview_bar.setRange(0, 0)
        def update(event):
            self.preview_label.setText("結果HTML：" + event["phase"])
            total = event.get("total", 0)
            if total:
                self.preview_bar.setRange(0, 100)
                self.preview_bar.setValue(round(event.get("completed", 0) / total * 100))
            else:
                self.preview_bar.setRange(0, 0)
        def success(result):
            self.preview_bar.setRange(0, 100)
            self.preview_bar.setValue(100)
            self.preview_label.setText(f"結果HTML：完了 / {result['videos']}動画 / results/dashboard.html")
            self.open_results_dashboard()
        def failed(error):
            self.preview_label.setText("結果HTML：停止または失敗 / " + error.splitlines()[-1])
            self.preview_bar.setRange(0, 100)
        def finished():
            self.preview_button.setEnabled(True)
            self.preview_stop.setEnabled(False)
        self.background(lambda report: build_dashboard(self.store, config, progress=report,
                        cancelled=self.preview_cancel.is_set), "結果HTML・連結動画を作成中",
                        callback=success, progress=update, failed=failed, finished=finished)

    def open_post_result(self):
        state = self.store.read()
        for jid in selected(self.jobs):
            runs = state["jobs"].get(jid, {}).get("postprocess_runs", [])
            latest = next((r for r in reversed(runs) if r["state"] == "complete"), None)
            if not latest:
                W.QMessageBox.information(self, "後処理結果", "このジョブの追加後処理結果はまだありません。")
                continue
            try:
                os.startfile(str(inside(self.root, latest["publication"])))
            except OSError as exc:
                W.QMessageBox.warning(self, "後処理結果", str(exc))

    def late_post_selected(self):
        ids = selected(self.jobs)
        if len(ids) != 1:
            W.QMessageBox.information(self, "追加後処理", "完了したジョブを一つ選択してください。")
            return
        jid = ids[0]
        try:
            job, source, manifest, groups = published_groups(self.store, jid)
        except (ValueError, KeyError, OSError) as exc:
            W.QMessageBox.warning(self, "追加後処理", str(exc))
            return
        dialog = W.QDialog(self)
        dialog.setWindowTitle("後から動画化・デノイズ — " + job.get("name", jid))
        dialog.resize(640, 420)
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(W.QLabel("公開済みのPNG Beauty連番を使用します。原本画像・AOVは保持します。"))
        video = W.QCheckBox("FFV1動画を作成")
        video.setChecked(True)
        denoise = W.QCheckBox("FastDVDnet sigma 10でデノイズ（GPU認識時はGPU）")
        layout.addWidget(video)
        layout.addWidget(denoise)
        form = W.QFormLayout()
        fps = W.QLineEdit(str(manifest["job"]["settings"]["fps"]))
        form.addRow("FPS（24000/1001 等も可）", fps)
        layout.addLayout(form)
        checklist = W.QListWidget()
        for layer, camera in sorted(groups):
            item = W.QListWidgetItem(layer + " / " + camera + "（" + str(len(groups[(layer, camera)])) + "フレーム）")
            item.setData(QtCore.Qt.UserRole, (layer, camera))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked)
            checklist.addItem(item)
        layout.addWidget(checklist)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Ok).setText("処理開始")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != W.QDialog.Accepted:
            return
        options = {"video": video.isChecked(), "fastdvdnet": denoise.isChecked()}
        pairs = [checklist.item(i).data(QtCore.Qt.UserRole) for i in range(checklist.count()) if checklist.item(i).checkState() == QtCore.Qt.Checked]
        rate = fps.text().strip()
        self.late_post_cancel.clear()
        self.late_post_button.setEnabled(False)
        self.late_post_stop.setEnabled(True)
        self.late_post_bar.setRange(0, 0)
        def update(event):
            count, total = event.get("completed", 0), event.get("total", 0)
            label = event["phase"]
            if event.get("groups"):
                label += f" / 対象 {event['group']}・全{event['groups']}組"
            if total:
                self.late_post_bar.setRange(0, 100)
                self.late_post_bar.setValue(round(count / total * 100))
                label += f" / {count}・全{total}フレーム"
            else:
                self.late_post_bar.setRange(0, 0)
            self.late_post_label.setText("追加後処理：" + label)
        def success(result):
            self.late_post_bar.setRange(0, 100)
            self.late_post_bar.setValue(100)
            self.late_post_label.setText("追加後処理：完了 / " + result["publication"])
        def failure(error):
            self.late_post_bar.setRange(0, 100)
            self.late_post_label.setText("追加後処理：停止または失敗 / " + error.splitlines()[-1])
        def finish():
            self.late_post_button.setEnabled(True)
            self.late_post_stop.setEnabled(False)
        self.background(lambda report: run_late_postprocess(self.store, jid, options, worker_config(self.root),
                        selection=pairs, fps=rate, progress=report, cancelled=self.late_post_cancel.is_set),
                        "追加後処理中", callback=success, progress=update, failed=failure, finished=finish)

    def details(self):
        is_scene = self.tabs.currentWidget() == self.scene_page
        ids = selected(self.scenes if is_scene else self.jobs)
        if ids:
            self.show_text("詳細", [self.state["scenes" if is_scene else "jobs"][i] for i in ids])

    def show_text(self, title, value):
        dialog = W.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(900, 650)
        layout = W.QVBoxLayout(dialog)
        text = W.QPlainTextEdit(json.dumps(value, ensure_ascii=False, indent=2))
        text.setReadOnly(True)
        layout.addWidget(text)
        dialog.exec()

    def build_package_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        text = W.QLabel("シーン一覧で持ち出すシーンを選択してから、梱包を実行します。\nプロジェクト全体・外部素材・ツール・独立キューを空の保存先へ書き出します。\n検査が成功したパッケージだけが持ち出し可能になります。")
        layout.addWidget(text)
        pack = W.QPushButton("選択したシーンをUSB用に梱包")
        self.pack_button = pack
        self.packing = False
        pack.clicked.connect(self.pack)
        layout.addWidget(pack)
        extras = W.QPushButton("プロジェクトに追加素材フォルダを登録")
        self.pack_extras_button = extras
        extras.clicked.connect(self.extra_folder)
        layout.addWidget(extras)
        self.pack_label = W.QLabel("素材の梱包：待機中")
        self.pack_label.setTextFormat(QtCore.Qt.PlainText)
        self.pack_label.setWordWrap(True)
        self.pack_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.pack_label)
        self.pack_bar = W.QProgressBar()
        self.pack_bar.setRange(0, 100)
        self.pack_bar.setValue(0)
        layout.addWidget(self.pack_bar)
        self.pack_reports = W.QTabWidget()
        self.pack_warnings = table(["対象シーン", "警告"])
        self.pack_mappings = table(["対象シーン", "元のパス", "調査時の読み替え先", "梱包内のパス（保存先からの相対パス）"])
        self.pack_reports.addTab(self.pack_warnings, "警告（0件）")
        self.pack_reports.addTab(self.pack_mappings, "対応表（0件）")
        layout.addWidget(self.pack_reports, 1)
        self.pack_load_button = W.QPushButton("保存済みの梱包結果を読み込む")
        self.pack_load_button.clicked.connect(self.load_package_reports)
        layout.addWidget(self.pack_load_button)
        self.pack_timer = QtCore.QTimer(self)
        self.pack_timer.timeout.connect(self.update_pack_display)
        self.pack_timer.start(1000)
        layout.addStretch()
        self.tabs.addTab(page, "素材の梱包")

    def pack(self):
        if self.packing:
            return
        ids = selected(self.scenes)
        if not ids:
            W.QMessageBox.information(self, "素材の梱包", "シーン一覧で対象シーンを選択してください")
            return
        destination = W.QFileDialog.getExistingDirectory(self, "空の書き出し先フォルダを選択")
        if destination:
            from .package import export_package
            self.packing = True
            self.pack_started = time.monotonic()
            self.pack_event = {"phase": "梱包の準備中", "total": None}
            self.pack_destination = destination
            self.update_pack_reports({"warnings": [], "mappings": []})
            self.pack_button.setEnabled(False)
            self.pack_extras_button.setEnabled(False)
            self.pack_load_button.setEnabled(False)
            self.update_pack_display()
            def work(emit):
                last_emit, last_phase = 0, None
                def report(event):
                    nonlocal last_emit, last_phase
                    now = time.monotonic()
                    if "warnings" in event or "mappings" in event or now - last_emit >= .15 or event.get("phase") != last_phase:
                        emit(event)
                        last_emit, last_phase = now, event.get("phase")
                return export_package(self.store, destination, ids, worker_config(self.root), progress=report)
            self.background(work, "素材を梱包・検証中", self.pack_complete, progress=self.pack_progress,
                            finished=self.pack_finished, failed=self.pack_failed)

    def pack_progress(self, event):
        self.update_pack_reports(event)
        self.pack_event = event
        self.update_pack_display()

    def update_pack_reports(self, value):
        if "warnings" in value:
            warnings = value["warnings"]
            populate(self.pack_warnings, [(str(index), [w.get("scene", ""), w.get("message", "")])
                                         for index, w in enumerate(warnings)])
            self.pack_reports.setTabText(0, f"警告（{len(warnings)}件）")
        if "mappings" in value:
            mappings = value["mappings"]
            populate(self.pack_mappings, [(str(index), [m.get("scene", ""), m.get("source", ""),
                                                       m.get("resolved", ""), m.get("target", "")])
                                         for index, m in enumerate(mappings)])
            self.pack_reports.setTabText(1, f"対応表（{len(mappings)}件）")

    def load_package_reports(self):
        if self.packing:
            return
        directory = W.QFileDialog.getExistingDirectory(self, "梱包済みの保存先フォルダを選択")
        if not directory:
            return
        try:
            root = Path(directory)
            value = read_json(root / "package_status.json", {})
            warnings = read_json(root / "package_warnings.json", value.get("warnings", []))
            mappings = read_json(root / "package_mappings.json", value.get("mappings", []))
            if not (root / "package_status.json").exists():
                raise ValueError("梱包結果がありません")
            if not mappings:
                packaged = read_json(root / "data/state.json", {})
                for project in packaged.get("projects", {}).values():
                    mappings.extend({"scene": project["name"], "source": source, "resolved": "", "target": target}
                                    for source, target in project.get("portable", {}).get("mappings", []))
            self.update_pack_reports({"warnings": warnings, "mappings": mappings})
            self.pack_label.setText(("保存済み梱包：完了" if value.get("ready") else "保存済み梱包：未完了") + "\n保存先：" + directory)
            self.pack_bar.setRange(0, 100)
            self.pack_bar.setValue(100 if value.get("ready") else 0)
        except Exception as exc:
            W.QMessageBox.warning(self, "梱包結果を読めません", str(exc))

    def update_pack_display(self):
        if not self.packing:
            return
        event = self.pack_event
        elapsed = int(time.monotonic() - self.pack_started)
        text = f"{event.get('phase', '')}　経過 {elapsed // 60}分{elapsed % 60:02d}秒"
        done, total = event.get("completed", 0), event.get("total")
        if total is not None:
            text += f"　{done:,} / {total:,} 件"
        if event.get("file_size") is not None:
            text += f"\n現在のファイル：{event.get('file_bytes', 0) / 1048576:,.1f} / {event['file_size'] / 1048576:,.1f} MiB"
        if event.get("total_bytes") is not None:
            text += f"\n素材のコピー量：{event.get('copied_bytes', 0) / 1048576:,.1f} / {event['total_bytes'] / 1048576:,.1f} MiB"
        self.pack_label.setText(text + "\n" + event.get("path", event.get("scene", "")) + "\n保存先：" + self.pack_destination)
        self.pack_bar.setRange(0, total if total else 0)
        self.pack_bar.setValue(done)
        self.pack_bar.setFormat("現在の工程：%v / %m 件（%p%）")

    def pack_complete(self, value):
        self.update_pack_reports(value)
        self.pack_progress({"phase": "梱包完了", "completed": len(value["jobs"]), "total": len(value["jobs"])})
        self.pack_finished()
        self.show_text("梱包結果", value)

    def pack_failed(self, error):
        self.pack_progress({"phase": "梱包失敗：" + error.splitlines()[-1], "total": None})
        self.pack_bar.setRange(0, 100)
        self.pack_bar.setValue(0)
        self.pack_finished()

    def pack_finished(self):
        self.packing = False
        self.pack_button.setEnabled(True)
        self.pack_extras_button.setEnabled(True)
        self.pack_load_button.setEnabled(True)

    def extra_folder(self):
        projects = list(self.state["projects"].values())
        if not projects:
            return
        labels = [p["name"] + " / " + p["id"] for p in projects]
        label, ok = W.QInputDialog.getItem(self, "追加素材", "プロジェクト", labels, 0, False)
        if not ok:
            return
        folder = W.QFileDialog.getExistingDirectory(self, "追加素材フォルダ")
        if folder:
            with self.store.transaction() as s:
                s["projects"][projects[labels.index(label)]["id"]]["extras"].append(folder)
            self.statusBar().showMessage("追加素材を登録しました。必要に応じて「更新を確認」を押してください。", 15000)

    def build_settings_page(self):
        page = W.QWidget()
        form = W.QFormLayout(page)
        self.config_widgets = {}
        config = worker_config(self.root)
        self.versions = {}
        detected = installations(config.get("maya_paths"))
        for version in ("2025", "2026", "2027"):
            line = W.QHBoxLayout()
            checkbox = W.QCheckBox("使用する")
            checkbox.setChecked(version in config.get("enabled_versions", []))
            field = W.QLineEdit(config.get("maya_paths", {}).get(version, detected.get(version, "")))
            field.setPlaceholderText("Mayaインストールフォルダ（未指定なら自動検出）")
            line.addWidget(checkbox)
            line.addWidget(field)
            self.versions[version] = (checkbox, field)
            form.addRow("Maya " + version, line)
        for key, label in [("ffmpeg", "FFmpeg実行ファイル"), ("fastdvdnet_python", "FastDVDnet用Python"),
                           ("fastdvdnet_repo", "FastDVDnetコードフォルダ"), ("fastdvdnet_model", "FastDVDnetモデル")]:
            row = W.QHBoxLayout()
            field = W.QLineEdit(config.get(key, ""))
            button = W.QPushButton("参照")
            def browse(checked=False, field=field, key=key):
                path = W.QFileDialog.getExistingDirectory(self, "フォルダを選択") if key == "fastdvdnet_repo" else W.QFileDialog.getOpenFileName(self, "ファイルを選択")[0]
                if path:
                    field.setText(path)
            button.clicked.connect(browse)
            row.addWidget(field)
            row.addWidget(button)
            form.addRow(label, row)
            self.config_widgets[key] = field
        settings = self.store.read()["settings"]
        self.default_timeout = W.QDoubleSpinBox()
        self.default_timeout.setRange(.1, 100000)
        self.default_timeout.setValue(settings["timeout_minutes"])
        form.addRow("既定タイムアウト（分）", self.default_timeout)
        self.default_restarts = W.QSpinBox()
        self.default_restarts.setRange(0, 100)
        self.default_restarts.setValue(settings["restart_limit"])
        form.addRow("既定の再起動上限", self.default_restarts)
        save = W.QPushButton("設定を保存（PC設定は待機時・次のジョブで反映）")
        save.clicked.connect(self.save_settings)
        form.addRow(save)
        self.tabs.addTab(page, "設定")

    def save_settings(self):
        config = worker_config(self.root)
        config.update({key: widget.text().strip() for key, widget in self.config_widgets.items()})
        for key in ("ffmpeg", "fastdvdnet_python", "fastdvdnet_repo", "fastdvdnet_model"):
            if config.get(key):
                path = Path(config[key])
                if path.is_absolute() and path.is_relative_to(self.root):
                    config[key] = path.relative_to(self.root).as_posix()
        config["enabled_versions"] = [version for version, (check, _) in self.versions.items() if check.isChecked()]
        config["maya_paths"] = {version: field.text().strip() for version, (_, field) in self.versions.items() if field.text().strip()}
        atomic_json(worker_dir(self.root) / "config.json", config)
        with self.store.transaction() as s:
            s["settings"].update(timeout_minutes=self.default_timeout.value(), restart_limit=self.default_restarts.value())
        self.statusBar().showMessage("設定を保存しました", 10000)

    def closeEvent(self, event):
        if self.tasks:
            W.QMessageBox.information(self, "処理中", "シーン調査・梱包などの処理が完了してから閉じてください")
            event.ignore()
        else:
            # Supervisor is intentionally independent; closing the dashboard does not cancel renders.
            event.accept()


def run(root):
    app = W.QApplication.instance() or W.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Yu Gothic UI", 10))
    window = Dashboard(root)
    window.show()
    return app.exec()
