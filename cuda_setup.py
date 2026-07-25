"""Докачка CUDA-библиотек (cuBLAS/cuDNN/cuda NVRTC) для распознавания речи на GPU.

Установщик (ЭТАП 3) сознательно НЕ включает nvidia-пакеты в дистрибутив —
это ~1.3 ГБ, а Live Call на CPU ими не пользуется вовсе. Вместо этого при
первом запуске Live Call с DEVICE=cuda в замороженной сборке приложение само
качает нужные .whl с PyPI (те же колёса, что ставятся через pip в
dev-режиме) и раскладывает .dll из них в
%APPDATA%\\RealtimeTranslator\\cuda\\<pkg>\\bin — этот путь pipeline.py
безусловно добавляет в PATH (папки могут появиться уже после импорта
pipeline, поэтому проверка os.path.isdir там намеренно убрана для
frozen-ветки).

Версии пакетов зафиксированы такими же, как в requirements.txt для
dev-режима — докачанные .dll должны быть той же версии, что и то, с чем
собран/протестирован ctranslate2 в этой сборке.
"""
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

from PyQt6 import QtCore, QtWidgets

import paths

# (pypi-имя, версия, внутренняя папка nvidia/<pkg>/bin, человекочитаемое имя)
_PACKAGES = [
    ("nvidia-cublas-cu12", "12.9.2.10", "cublas", "cuBLAS"),
    ("nvidia-cudnn-cu12", "9.23.2.1", "cudnn", "cuDNN"),
    ("nvidia-cuda-nvrtc-cu12", "12.9.86", "cuda_nvrtc", "cuda NVRTC"),
]

_PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
_USER_AGENT = "RealtimeTranslator-CudaSetup/1.0"

TOTAL_DOWNLOAD_MB = 1300  # ориентировочно, для текста в диалоге


def cuda_ready() -> bool:
    """True, если в paths.CUDA_DIR/<pkg>/bin для всех трёх пакетов лежит
    папка и в ней есть хотя бы один .dll."""
    for _, _, pkg, _ in _PACKAGES:
        bin_dir = os.path.join(paths.CUDA_DIR, pkg, "bin")
        if not os.path.isdir(bin_dir):
            return False
        try:
            names = os.listdir(bin_dir)
        except OSError:
            return False
        if not any(n.lower().endswith(".dll") for n in names):
            return False
    return True


def _pick_win_wheel(pypi_json: dict):
    """Из ответа PyPI /pypi/<name>/<version>/json выбирает .whl под win_amd64.
    Возвращает (url, size) либо None, если такого колеса для версии нет."""
    for entry in pypi_json.get("urls", []):
        url = entry.get("url", "")
        if url.endswith(".whl") and "win_amd64" in url:
            return url, entry.get("size", 0)
    return None


class _CancelledError(Exception):
    """Внутренний сигнал: пользователь нажал Cancel во время закачки/распаковки."""


class _DownloadWorker(QtCore.QThread):
    """Качает и распаковывает все пакеты по очереди в отдельном потоке —
    сеть и распаковка большого архива не должны подвешивать UI."""

    # label, pkg_index(1-based), pkg_total, downloaded_bytes, total_bytes
    progress = QtCore.pyqtSignal(str, int, int, int, int)
    status = QtCore.pyqtSignal(str)
    finished_ok = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel_requested = False

    def request_cancel(self):
        self._cancel_requested = True

    def _check_cancel(self):
        if self._cancel_requested:
            raise _CancelledError()

    def run(self):
        tmp_path = None
        try:
            total = len(_PACKAGES)
            for i, (pypi_name, version, pkg, label) in enumerate(_PACKAGES, start=1):
                self._check_cancel()
                self.status.emit(f"Resolving download for {label}…")
                url, size = self._resolve(pypi_name, version)
                self._check_cancel()
                tmp_path = self._download(url, size, label, i, total)
                self._check_cancel()
                self.status.emit(f"Unpacking {label}…")
                self._extract(tmp_path, pkg)
                self._remove_tmp(tmp_path)
                tmp_path = None
            self.finished_ok.emit()
        except _CancelledError:
            self._remove_tmp(tmp_path)
        except Exception as e:
            self._remove_tmp(tmp_path)
            self.failed.emit(str(e))

    def _resolve(self, pypi_name, version):
        url = _PYPI_JSON_URL.format(name=pypi_name, version=version)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"Network error while resolving {pypi_name}: {e}")
        found = _pick_win_wheel(data)
        if not found:
            raise RuntimeError(f"No Windows wheel found for {pypi_name}=={version}")
        return found

    def _download(self, url, size_hint, label, idx, total) -> str:
        fd, tmp_path = tempfile.mkstemp(suffix=".whl", prefix="rtt_cuda_")
        os.close(fd)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_length = int(resp.headers.get("Content-Length") or size_hint or 0)
                downloaded = 0
                chunk_size = 1024 * 256
                with open(tmp_path, "wb") as f:
                    while True:
                        self._check_cancel()
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(label, idx, total, downloaded, content_length)
        except _CancelledError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"Network error while downloading {label}: {e}")
        return tmp_path

    def _extract(self, wheel_path, pkg):
        """Достаёт из колеса (это zip-архив) только файлы из
        nvidia/<pkg>/bin/*.dll — структуру выпрямляем: nvidia/cublas/bin/x.dll
        превращается в CUDA_DIR/cublas/bin/x.dll."""
        target_dir = os.path.join(paths.CUDA_DIR, pkg, "bin")
        os.makedirs(target_dir, exist_ok=True)
        prefix = f"nvidia/{pkg}/bin/"
        try:
            with zipfile.ZipFile(wheel_path) as zf:
                members = [
                    m for m in zf.namelist()
                    if m.replace("\\", "/").startswith(prefix)
                    and m.lower().endswith(".dll")
                ]
                if not members:
                    raise RuntimeError(f"Wheel for {pkg} has no files under {prefix}")
                for member in members:
                    filename = os.path.basename(member.replace("\\", "/"))
                    dest = os.path.join(target_dir, filename)
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except zipfile.BadZipFile as e:
            raise RuntimeError(f"Corrupted download for {pkg}: {e}")

    @staticmethod
    def _remove_tmp(path):
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


class CudaSetupDialog(QtWidgets.QDialog):
    """Модальный диалог докачки GPU-компонентов. Тёмная тема в стиле
    остального приложения (см. KeysDialog в settings_dialog.py).

    exec() возвращает QDialog.DialogCode.Accepted, если все три пакета
    докачаны и распакованы успешно — тогда вызывающий код может продолжать
    старт Live Call. Rejected — пользователь отменил."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GPU Setup")
        self.setModal(True)
        self.setFixedWidth(440)
        self.setStyleSheet("QDialog{background:rgba(22,22,30,238);}")
        self._worker = None

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)

        title = QtWidgets.QLabel("GPU Setup")
        title.setStyleSheet("color:#eaeaf0;font-size:15px;font-weight:700;")
        v.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "Speech recognition needs GPU components (~1.3 GB download, one time)."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#6b7089;font-size:11px;")
        v.addWidget(subtitle)

        panel = QtWidgets.QFrame()
        panel.setStyleSheet(
            "QFrame{background:rgba(255,255,255,12);border-radius:8px;}"
        )
        pv = QtWidgets.QVBoxLayout(panel)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(8)

        self.status_lbl = QtWidgets.QLabel("Ready to download.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#d8dae5;font-size:12px;")
        pv.addWidget(self.status_lbl)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,14);border:1px solid rgba(255,255,255,25);"
            "border-radius:6px;color:#eaeaf0;text-align:center;height:18px;}"
            "QProgressBar::chunk{background:#2e7d5b;border-radius:6px;}"
        )
        pv.addWidget(self.progress_bar)

        self.detail_lbl = QtWidgets.QLabel("")
        self.detail_lbl.setWordWrap(True)
        self.detail_lbl.setStyleSheet("color:#6b7089;font-size:11px;")
        pv.addWidget(self.detail_lbl)

        v.addWidget(panel)

        self.error_lbl = QtWidgets.QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setStyleSheet("color:#ff6b6b;font-size:11px;")
        self.error_lbl.setVisible(False)
        v.addWidget(self.error_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,14);color:#d8dae5;"
            "border:1px solid rgba(255,255,255,28);border-radius:8px;"
            "padding:7px 16px;font-weight:600;}"
            "QPushButton:hover{background:rgba(255,255,255,26);color:#ffffff;}"
        )
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)

        self.action_btn = QtWidgets.QPushButton("Download")
        self.action_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.action_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:7px 18px;font-weight:600;}QPushButton:hover{background:#379268;}"
        )
        self.action_btn.clicked.connect(self._on_action)
        btn_row.addWidget(self.action_btn)

        v.addLayout(btn_row)

    # ---------- UI-логика ----------

    def _on_action(self):
        # кнопка одна: Download до запуска, Retry после ошибки — оба ведут
        # в _start_download
        self._start_download()

    def _start_download(self):
        self.error_lbl.setVisible(False)
        self.progress_bar.setValue(0)
        self.status_lbl.setText("Starting download…")
        self.detail_lbl.setText("")
        self.action_btn.setEnabled(False)
        self.action_btn.setText("Downloading…")

        self._worker = _DownloadWorker(self)
        self._worker.progress.connect(self._on_progress)
        self._worker.status.connect(self._on_status)
        self._worker.finished_ok.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.start()

    def _on_progress(self, label, idx, total, downloaded, total_bytes):
        self.status_lbl.setText(f"Downloading {label} ({idx}/{total})…")
        if total_bytes > 0:
            pct = int(downloaded * 100 / total_bytes)
            self.progress_bar.setValue(pct)
            self.detail_lbl.setText(
                f"{downloaded / (1024*1024):.1f} MB / {total_bytes / (1024*1024):.1f} MB"
            )
        else:
            self.detail_lbl.setText(f"{downloaded / (1024*1024):.1f} MB downloaded")

    def _on_status(self, text):
        self.status_lbl.setText(text)

    def _on_success(self):
        self.status_lbl.setText("GPU components installed.")
        self.progress_bar.setValue(100)
        self.detail_lbl.setText("")
        self.action_btn.setEnabled(True)
        self.accept()

    def _on_failure(self, message):
        self.error_lbl.setText(f"Download failed: {message}")
        self.error_lbl.setVisible(True)
        self.status_lbl.setText("Download failed.")
        self.action_btn.setEnabled(True)
        self.action_btn.setText("Retry")

    def _on_cancel(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self._worker.wait(3000)
        self.reject()

    def closeEvent(self, e):
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self._worker.wait(3000)
        super().closeEvent(e)
