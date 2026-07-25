"""Пути к файлам приложения — единая точка правды для dev/frozen режимов.

В dev-режиме (запуск из исходников, python main.py) всё лежит в папке
проекта, как и раньше. В замороженном режиме (PyInstaller onedir, ЭТАП 2
установщика) код живёт в Program Files — туда нельзя писать без прав
администратора, поэтому пользовательские данные (.env с ключами, логи,
крашлоги, докачанные CUDA-библиотеки) переезжают в %APPDATA%\\RealtimeTranslator.

frozen определяем через getattr(sys, "frozen", False) — так делает PyInstaller.
"""
import os
import sys

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # sys.executable = dist\RealtimeTranslator\RealtimeTranslator.exe
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # dev-режим: папка проекта — путь строим от этого файла, а не от cwd
    # (иначе запуск с другой рабочей директорией молча не найдёт файлы)
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    """В dev — та же папка проекта, что и раньше (ничего не меняется).
    В frozen — %APPDATA%\\RealtimeTranslator, создаём при первом обращении
    (Program Files не доступен для записи без прав администратора)."""
    if not FROZEN:
        return APP_DIR
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RealtimeTranslator")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _data_dir()

ENV_PATH = os.path.join(DATA_DIR, ".env")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
# CUDA-библиотеки (cublas/cudnn/cuda_nvrtc), докачиваемые установщиком на
# ЭТАПЕ 3 — актуально только для frozen-режима; в dev используются пакеты
# nvidia-* из venv (см. pipeline.py)
CUDA_DIR = os.path.join(DATA_DIR, "cuda")


def resource_path(rel: str) -> str:
    """Путь к read-only ресурсу (assets/, knowledge_base/) относительно
    корня приложения. В dev — от папки проекта. В frozen (PyInstaller,
    onedir) datas распаковываются в _internal рядом с exe, и путь к этой
    папке лежит в sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)
