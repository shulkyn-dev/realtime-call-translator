"""Перевод произвольного текста через Claude — для вкладки ручного перевода.

Язык исходного текста определяется автоматически, целевой выбирается в UI.
Claude заодно сообщает, каким был исходный язык — это нужно, чтобы кнопка
"Swap" могла сама проставить правильное направление перевода обратно, а не
гадать. Каждый вызов — отдельный короткий фоновый поток (не привязан к
Старту/Стопу звонка, работает независимо), результат кладётся в очередь.
"""
import threading
import queue
import time

import anthropic

import config

TARGET_LANGS = ["Russian", "English", "Turkish", "Ukrainian"]

SYSTEM_PROMPT = """Translate the text the user gives you into {target}. The source
language isn't specified — detect it automatically (it could be Russian, English,
Turkish, Ukrainian, or something else).

Reply in exactly this format, two parts:
SOURCE: <detected source language name, in English, one word, e.g. Turkish>
<a blank line, then the translation itself — nothing else, no quotes, no labels>"""

# для скриншотов — та же логика перевода, но сначала нужно снять текст с
# картинки; протокол ответа (SOURCE: + перевод) намеренно тот же самый, чтобы
# кнопка Swap работала одинаково независимо от того, был вход текстом или
# картинкой
IMAGE_SYSTEM_PROMPT = """The user gives you a screenshot with text on it (any
language — could be Russian, English, Turkish, Ukrainian, or something else).
Extract all the text from the image, detect its language, and translate it into
{target}. Preserve a reasonable line/paragraph structure from the image — don't
collapse everything into one run-on paragraph if the original clearly had
separate lines or blocks.

Reply in exactly this format, two parts:
SOURCE: <detected source language name, in English, one word, e.g. Turkish>
<a blank line, then ONLY the translation itself — nothing else: no quotes, no
labels, no description of the image, no mention that you extracted text from it>"""


def translate_async(text: str, target_lang: str, result_queue: queue.Queue, image_b64: str = None):
    """Запускает перевод в фоновом потоке. Кладёт в result_queue:
    ("ok", translated_text, detected_source_lang) или ("error", message, None) —
    не блокирует UI на время сетевого запроса.

    Если передан image_b64 (base64 PNG скриншота) — переводится текст с
    картинки, а не аргумент text (он в этом случае игнорируется); формат
    ответа (SOURCE: + перевод) остаётся тем же самым, чтобы Swap работал
    одинаково для обоих путей."""

    def _run():
        if not config.ANTHROPIC_API_KEY:
            result_queue.put(("error", "No Anthropic key in .env", None))
            return
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        if image_b64:
            system = IMAGE_SYSTEM_PROMPT.format(target=target_lang)
            content = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": f"Translate the text in this image into {target_lang}."},
            ]
        else:
            system = SYSTEM_PROMPT.format(target=target_lang)
            content = text
        last_err = None
        for attempt in range(2):  # короткая сеть моргнула — пробуем ещё раз
            try:
                resp = client.messages.create(
                    model=config.AI_MODEL,
                    max_tokens=1500,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                )
                raw = "".join(
                    b.text for b in resp.content if b.type == "text"
                ).strip()
                source_lang, translated = _parse(raw)
                result_queue.put(("ok", translated, source_lang))
                return
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.7)
        result_queue.put(("error", str(last_err), None))

    threading.Thread(target=_run, daemon=True).start()


def _parse(raw: str):
    """Разбирает "SOURCE: X\\n\\n<перевод>" на (X, перевод). Если формат вдруг
    не совпал (модель не послушалась) — считаем весь ответ переводом, источник
    неизвестен, чтобы не терять результат из-за мелкого расхождения в формате."""
    if raw.upper().startswith("SOURCE:"):
        first_line, _, rest = raw.partition("\n")
        source = first_line.split(":", 1)[1].strip()
        return source, rest.strip()
    return None, raw
