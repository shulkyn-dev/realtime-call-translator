"""Настройки приложения. Значения читаются из .env (см. .env.example)."""
import os
from dotenv import load_dotenv

import paths

# путь к .env — paths.py сам решает, где он лежит (dev: папка проекта,
# frozen: %APPDATA%\RealtimeTranslator, т.к. Program Files недоступен для записи)
_ENV_PATH = paths.ENV_PATH

# override=True: .env этого проекта должен побеждать чужие переменные окружения
# (на этой машине уже есть системный ANTHROPIC_API_KEY от другого инструмента).
load_dotenv(_ENV_PATH, override=True)

# --- Перевод (DeepL) ---
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
TARGET_LANG = os.getenv("TARGET_LANG", "RU")        # язык, НА который переводим
SOURCE_LANG = os.getenv("SOURCE_LANG", "EN")        # язык речи собеседников

# --- Подсказки ответов (Claude) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")
AI_CONTEXT_TURNS = int(os.getenv("AI_CONTEXT_TURNS", "10"))  # сколько последних реплик держим в контексте

# --- Распознавание (faster-whisper) ---
# large-v3 = максимум качества (для NVIDIA GPU). Альтернативы: medium, small.
MODEL_SIZE = os.getenv("MODEL_SIZE", "large-v3")
DEVICE = os.getenv("DEVICE", "cuda")                # cuda (GPU) или cpu
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "float16")  # float16 для GPU, int8 для CPU
WHISPER_LANG = os.getenv("WHISPER_LANG", "auto")    # "auto" — Whisper сам определяет язык каждой фразы; или en/ru/uk — принудительно

# Языки, которые пользователь понимает сам — перевод для них не нужен, нужна
# только ИИ-подсказка. Название на русском — для системного промпта ассистента
# (чтобы ответ-подсказка писался на языке, на котором реально говорит клиент).
CLIENT_LANGUAGES = {
    "en": {"name": "English", "translate": True},
    "ru": {"name": "Russian", "translate": False},
    "uk": {"name": "Ukrainian", "translate": False},
}

# --- Аудио / VAD ---
SAMPLE_RATE = 16000          # частота, на которой работает Whisper
FRAME_MS = 30                # длина кадра анализа тишины, мс
SILENCE_RMS = float(os.getenv("SILENCE_RMS", "0.006"))  # порог тишины (подстрой при шуме)
SILENCE_HANG_MS = 400        # сколько тишины = конец фразы (быстрый коммит)

# если реального звука (уровень выше SILENCE_RMS) не было столько минут —
# автоматически жмём Stop. Защита от забытого включённым Live Call: тихий
# фоновый шум/гул может держать VAD «в речи» и заставлять Whisper на
# каждый MAX_UTTERANCE_MS выдумывать текст из тишины (галлюцинация), а
# каждая такая «финальная» фраза с непустым текстом уходит в ИИ-подсказку
# и реально тратит деньги на Anthropic API, пока никто не говорит
AUTO_STOP_IDLE_MINUTES = float(os.getenv("AUTO_STOP_IDLE_MINUTES", "10"))
MIN_UTTERANCE_MS = 300       # короче — игнорируем (щелчки)
MAX_UTTERANCE_MS = 2500      # мягкий предел: коммитим фразу даже без паузы

# --- Потоковый режим (real-time) ---
# Пока человек говорит без пауз, перетранскрибируем растущий буфер каждые
# INTERIM_INTERVAL_MS и показываем «черновой» английский И русский на лету.
# Перевод чернового делаем не чаще INTERIM_TR_MIN_MS, чтобы не жечь лимит DeepL.
INTERIM_ENABLED = os.getenv("INTERIM_ENABLED", "1") == "1"
INTERIM_INTERVAL_MS = 1000   # как часто обновлять «живой» текст (реже — меньше нагрузка на GPU)
INTERIM_TRANSLATE = os.getenv("INTERIM_TRANSLATE", "1") == "1"  # переводить черновики
INTERIM_TR_MIN_MS = 900      # не переводить черновик чаще, чем раз в столько мс

# при долгой непрерывной речи финалы могут копиться быстрее, чем GPU успевает
# их обрабатывать — beam_size поменьше ускоряет финальную транскрипцию, чтобы
# конвейер не отставал и не «зависал» с виду, пока разгребает очередь
FINAL_BEAM_SIZE = int(os.getenv("FINAL_BEAM_SIZE", "3"))

# --- Окно субтитров ---
FONT_SIZE_RU = int(os.getenv("FONT_SIZE_RU", "16"))


def reload():
    """Перечитывает .env и обновляет ключи в этом модуле — без перезапуска
    приложения, вызывается после того, как KeysDialog сохранил новые ключи.
    override=True обязателен: на этой машине есть системная переменная
    ANTHROPIC_API_KEY от другого инструмента, и .env должен её перекрывать —
    как и при обычной загрузке модуля выше."""
    global ANTHROPIC_API_KEY, DEEPL_API_KEY
    load_dotenv(_ENV_PATH, override=True)
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
    DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
