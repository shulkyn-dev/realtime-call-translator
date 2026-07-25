"""Диалог ввода API-ключей — тёмная тема в стиле приложения.

Показывается модально при первом запуске, если ANTHROPIC_API_KEY пуст
(он обязателен — на нём держатся Text Translate, Cover Letter и ИИ-подсказки),
а также по кнопке ⚙ в верхней панели в любой момент. DEEPL_API_KEY —
опциональный, нужен только для перевода в Live Call.

Сохранение всегда идёт через save_keys_to_env() — она переписывает только
строки ANTHROPIC_API_KEY=.../DEEPL_API_KEY=..., не трогая остальные
настройки (MODEL_SIZE, DEVICE и т.п.) и комментарии в .env. Сами значения
ключей нигде не логируются и не печатаются.
"""
import os
import re

from PyQt6 import QtCore, QtWidgets

import config
import paths

ENV_PATH = paths.ENV_PATH


def _upsert_env_var(lines: list, key: str, value: str) -> list:
    """Заменяет значение key= в списке строк .env (первое совпадение —
    не закомментированную строку), либо дописывает новую строку в конец,
    если такого ключа ещё не было."""
    pattern = re.compile(rf"^{re.escape(key)}=")
    for i, line in enumerate(lines):
        if pattern.match(line.strip()):
            lines[i] = f"{key}={value}\n"
            return lines
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{key}={value}\n")
    return lines


def save_keys_to_env(anthropic_key: str, deepl_key: str, env_path: str = ENV_PATH):
    """Пишет оба ключа в .env, сохраняя все остальные строки файла как есть."""
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    lines = _upsert_env_var(lines, "ANTHROPIC_API_KEY", anthropic_key)
    lines = _upsert_env_var(lines, "DEEPL_API_KEY", deepl_key)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


class _KeyRow(QtWidgets.QWidget):
    """Подпись + поле пароля + кнопка-глаз, показать/скрыть значение.
    Переиспользуется для обоих полей ключей."""

    def __init__(self, label_text, placeholder="", parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        label = QtWidgets.QLabel(label_text)
        label.setWordWrap(True)
        label.setStyleSheet("color:#6b7089;font-size:11px;font-weight:600;")
        v.addWidget(label)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)

        self.edit = QtWidgets.QLineEdit()
        self.edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,12);border:1px solid rgba(255,255,255,25);"
            "border-radius:8px;color:#eaeaf0;font-size:13px;padding:7px 10px;}"
        )
        row.addWidget(self.edit, 1)

        self.eye_btn = QtWidgets.QPushButton("\U0001F441")  # 👁
        self.eye_btn.setFixedSize(32, 32)
        self.eye_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.eye_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,12);color:#9aa0b5;"
            "border:1px solid rgba(255,255,255,25);border-radius:8px;font-size:13px;}"
            "QPushButton:hover{color:#eaeaf0;}"
        )
        self.eye_btn.clicked.connect(self._toggle_visibility)
        row.addWidget(self.eye_btn)

        v.addLayout(row)

    def _toggle_visibility(self):
        if self.edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password:
            self.edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
        else:
            self.edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, value):
        self.edit.setText(value or "")


class KeysDialog(QtWidgets.QDialog):
    """Модальный диалог ввода ANTHROPIC_API_KEY (обязательный) и
    DEEPL_API_KEY (опциональный). При успешном Save сама записывает ключи
    в .env и вызывает config.reload() — вызывающему коду ничего досчитывать
    не нужно."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API Keys")
        self.setModal(True)
        self.setFixedWidth(440)
        self.setStyleSheet("QDialog{background:rgba(22,22,30,238);}")

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)

        title = QtWidgets.QLabel("API Keys")
        title.setStyleSheet("color:#eaeaf0;font-size:15px;font-weight:700;")
        v.addWidget(title)

        subtitle = QtWidgets.QLabel("Keys are stored locally in .env — this app never sends them anywhere else.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#6b7089;font-size:11px;")
        v.addWidget(subtitle)

        self.anthropic_row = _KeyRow("Anthropic API key (required)", "sk-ant-…")
        v.addWidget(self.anthropic_row)

        self.deepl_row = _KeyRow(
            "DeepL API key (optional — only for Live Call translation)", "xxxxxxxx-…:fx"
        )
        v.addWidget(self.deepl_row)

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
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.save_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:7px 18px;font-weight:600;}QPushButton:hover{background:#379268;}"
        )
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)

        v.addLayout(btn_row)

        self._prefill()

    def _prefill(self):
        # если ключи уже есть в config (перезапуск диалога через ⚙, или .env
        # уже был заполнен раньше) — подставляем их в поля
        self.anthropic_row.set_text(config.ANTHROPIC_API_KEY)
        self.deepl_row.set_text(config.DEEPL_API_KEY)

    def _on_save(self):
        anthropic_key = self.anthropic_row.text()
        deepl_key = self.deepl_row.text()
        if not anthropic_key:
            self.error_lbl.setText("Anthropic API key is required.")
            self.error_lbl.setVisible(True)
            return
        save_keys_to_env(anthropic_key, deepl_key)
        config.reload()
        self.accept()
