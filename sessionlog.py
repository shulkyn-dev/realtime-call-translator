"""Сохранение расшифровки звонка в файл на диске.

Один файл на каждый звонок (создаётся при «Старт», имя = дата-время начала).
Формат — двумя блоками: сначала весь английский текст, затем весь русский
перевод (не построчно вперемешку). Файл переписывается целиком при каждой новой
реплике — так сохраняется целостность, даже если приложение закроют аварийно.
Ничего не удаляется автоматически — старые файлы можно почистить вручную,
когда накопится, вместо того чтобы скроллить один бесконечный лог.
"""
import os
from datetime import datetime

import paths

LOGS_DIR = paths.LOGS_DIR


class SessionLog:
    def __init__(self):
        self.path = None
        self._started_at = None
        self._en_lines = []
        self._ru_lines = []

    def start(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        self._started_at = datetime.now()
        name = self._started_at.strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
        self.path = os.path.join(LOGS_DIR, name)
        self._en_lines = []
        self._ru_lines = []
        self._flush()
        return self.path

    def write(self, en: str, ru: str):
        if not self.path:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self._en_lines.append(f"[{ts}] {en}")
        self._ru_lines.append(f"[{ts}] {ru}")
        self._flush()

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"# Call started {self._started_at.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("=== ORIGINAL ===\n\n")
            f.write("\n".join(self._en_lines))
            f.write("\n\n=== TRANSLATION (RU) ===\n\n")
            f.write("\n".join(self._ru_lines))
            f.write("\n")

    def close(self):
        if self.path:
            self._flush()
            f_end = f"\n# Call ended {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f_end)
        self.path = None
