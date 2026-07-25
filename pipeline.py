"""Распознавание (faster-whisper) + перевод (DeepL) в фоновом потоке."""
import os
import sys
import time
import threading
import queue

import paths

if sys.platform == "win32":
    # ctranslate2 грузит cuBLAS/cuDNN/nvrtc через явный LoadLibrary(имя_файла) без пути —
    # такой вызов ищет DLL по стандартному порядку Windows: папка python.exe → System32 →
    # Windows → текущая директория → PATH. add_dll_directory на этот вызов не влияет.
    if paths.FROZEN:
        # замороженное приложение: nvidia-пакеты (~2-3 ГБ) НЕ входят в сборку —
        # докачиваются в рантайме через cuda_setup.py в
        # %APPDATA%\RealtimeTranslator\cuda\<pkg>\bin. Папки на момент импорта
        # этого модуля могут ещё не существовать (докачка происходит позже,
        # при нажатии Start) — добавляем пути в PATH БЕЗ проверки os.path.isdir:
        # несуществующие записи Windows просто пропускает при поиске DLL, а как
        # только cuda_setup их создаст, LoadLibrary найдёт файлы без перезапуска.
        for _pkg in ("cublas", "cudnn", "cuda_nvrtc"):
            _bin_dir = os.path.join(paths.CUDA_DIR, _pkg, "bin")
            os.environ["PATH"] = _bin_dir + os.pathsep + os.environ["PATH"]
    else:
        # dev-режим: nvidia-пакеты стоят в venv как обычные зависимости.
        # sys.executable = <venv>\Scripts\python.exe → site-packages на уровень выше Scripts.
        _venv_dir = os.path.dirname(os.path.dirname(sys.executable))
        _nvidia_dir = os.path.join(_venv_dir, "Lib", "site-packages", "nvidia")
        for _pkg in ("cublas", "cudnn", "cuda_nvrtc"):
            _bin_dir = os.path.join(_nvidia_dir, _pkg, "bin")
            if os.path.isdir(_bin_dir):
                os.environ["PATH"] = _bin_dir + os.pathsep + os.environ["PATH"]

import numpy as np
import deepl
from faster_whisper import WhisperModel

import config


class Transcriber(threading.Thread):
    """Берёт фразы из очереди аудио → EN-текст → перевод → очередь субтитров.

    Модель грузится ВНУТРИ потока (в run), чтобы не подвешивать интерфейс.
    О ходе работы сообщает через status_queue.
    """

    def __init__(self, utterance_queue: queue.Queue,
                 subtitle_queue: queue.Queue, stop_event: threading.Event,
                 status_queue: queue.Queue = None):
        super().__init__(daemon=True)
        self.inp = utterance_queue
        self.out = subtitle_queue          # кладём (kind, text, translated, lang)
        self.stop_event = stop_event
        self.status = status_queue
        self.model = None
        self.translator = None
        self._last_interim_tr = 0.0        # когда последний раз переводили черновик
        self._last_reported_lang = None    # чтобы не спамить UI одним и тем же языком

    def _emit(self, kind, value):
        if self.status is not None:
            try:
                self.status.put_nowait((kind, value))
            except queue.Full:
                pass

    def _load(self):
        self._emit("state", "Loading model…")
        self.model = WhisperModel(
            config.MODEL_SIZE, device=config.DEVICE, compute_type=config.COMPUTE_TYPE
        )
        if not config.DEEPL_API_KEY:
            raise RuntimeError("DEEPL_API_KEY is not set in .env")
        self.translator = deepl.Translator(config.DEEPL_API_KEY)
        self._emit("state", "Ready — listening")
        self._emit("ready", True)

    def _transcribe(self, audio: np.ndarray, quick: bool = False) -> tuple[str, str]:
        # quick=True для «черновых» кусков: beam_size=1 и без VAD-фильтра — быстрее.
        # WHISPER_LANG="auto" → language=None: Whisper сам определяет язык каждой
        # фразы (это уже встроено в transcribe, отдельного запроса не требует).
        lang_param = None if config.WHISPER_LANG == "auto" else config.WHISPER_LANG
        segments, info = self.model.transcribe(
            audio,
            language=lang_param,
            vad_filter=not quick,
            beam_size=1 if quick else config.FINAL_BEAM_SIZE,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        detected = lang_param or info.language
        return text, detected

    def _translate_needed(self, lang: str) -> bool:
        # для языков, которые пользователь понимает сам (ru/uk), перевод не
        # нужен — только распознавание + подсказка ИИ
        return config.CLIENT_LANGUAGES.get(lang, {}).get("translate", True)

    def _translate(self, text: str) -> str:
        # короткая сеть моргнула — не страшно, пробуем ещё раз, прежде чем сдаться
        last_err = None
        for attempt in range(2):
            try:
                res = self.translator.translate_text(
                    text, source_lang=config.SOURCE_LANG, target_lang=config.TARGET_LANG
                )
                return res.text
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.5)
        return f"[translation error: {last_err}]"

    def run(self):
        try:
            self._load()
        except Exception as e:
            self._emit("error", f"Failed to load model: {e}")
            return

        while not self.stop_event.is_set():
            try:
                item = self.inp.get(timeout=0.3)
            except queue.Empty:
                continue

            # собираем всё, что накопилось, чтобы не отставать: финалы обрабатываем
            # все по порядку, из «черновых» берём только самый свежий.
            batch = [item]
            try:
                while True:
                    batch.append(self.inp.get_nowait())
            except queue.Empty:
                pass

            finals = [a for kind, a in batch if kind == "final"]
            interims = [a for kind, a in batch if kind == "interim"]

            # одна сбойная фраза (редкий CUDA-глюк, битый буфер) не должна
            # убивать весь поток на середине звонка — ловим и едем дальше
            try:
                if finals:
                    # финалы в приоритете — обрабатываем все, чтобы не копился лаг.
                    # если их накопилось больше одной — значит, речь идёт быстрее,
                    # чем успевает GPU; показываем это явно, а не молчим (выглядит
                    # как зависание, хотя на деле просто разгребаем очередь)
                    backlog = len(finals)
                    for i, audio in enumerate(finals):
                        if backlog > 1:
                            self._emit("state", f"Catching up… ({backlog - i} queued)")
                        else:
                            self._emit("state", "Recognizing…")
                        en, lang = self._transcribe(audio)
                        if not en:
                            continue
                        if lang != self._last_reported_lang:
                            self._emit("lang", lang)
                            self._last_reported_lang = lang
                        # для ru/uk перевод не нужен — клиент и так понятен пользователю
                        ru = self._translate(en) if self._translate_needed(lang) else en
                        self.out.put(("final", en, ru, lang))
                    self._emit("state", "Ready — listening")
                elif interims:
                    en, lang = self._transcribe(interims[-1], quick=True)
                    if en:
                        ru = None
                        # переводим черновик вживую, но не чаще INTERIM_TR_MIN_MS
                        # (и только если для этого языка перевод вообще нужен)
                        if config.INTERIM_TRANSLATE and self._translate_needed(lang):
                            now = time.monotonic()
                            if (now - self._last_interim_tr) * 1000 >= config.INTERIM_TR_MIN_MS:
                                ru = self._translate(en)
                                self._last_interim_tr = now
                        self.out.put(("interim", en, ru, lang))
            except Exception as e:
                self._emit("state", f"Hiccup on one phrase, continuing: {e}")
