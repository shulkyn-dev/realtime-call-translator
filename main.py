"""Точка входа. Запуск:  python main.py  (или ярлык на рабочем столе)

Открывает компактную панель субтитров. Дальше — кнопка «Старт» в окне.
Модель грузится в фоне после «Старт», интерфейс не подвисает.
Повторный запуск (второй клик по ярлыку) не создаёт второе окно — тихо выходит,
пока первое ещё работает.
"""
import os
import sys
import threading
import traceback
from datetime import datetime

import config
import paths

_MUTEX_NAME = "Global\\RealtimeTranslatorEnRu_SingleInstance"
_CRASH_LOG = os.path.join(paths.LOGS_DIR, "crash.log")


def _log_crash(exc_type, exc_value, exc_tb):
    """Приложение запускается без консоли (см. launch.vbs) — без этого любая
    необработанная ошибка исчезает бесследно, и непонятно, что вообще случилось."""
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception:
        pass


def _log_thread_crash(args):
    # рабочие потоки (звук/распознавание/ИИ) sys.excepthook не ловит — нужен отдельный хук
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback)


sys.excepthook = _log_crash
threading.excepthook = _log_thread_crash


def _acquire_single_instance() -> bool:
    """True, если это единственный запущенный экземпляр. На не-Windows — всегда True."""
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    # ERROR_ALREADY_EXISTS = 183 — мьютекс уже держит другой процесс приложения
    return kernel32.GetLastError() != 183


def main():
    if not _acquire_single_instance():
        return  # приложение уже запущено — тихо выходим, окно не открываем повторно

    if not config.DEEPL_API_KEY:
        print("!! No DeepL key in .env — translation will not work.")

    from app import run
    run()


if __name__ == "__main__":
    main()
