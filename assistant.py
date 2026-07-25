"""Подсказка ответа на вопрос клиента/интервьюера — по базе знаний пользователя.

Копит недавние реплики собеседника (то, что слышно в звонке) в скользящее окно
контекста и на каждую новую реплику просит Claude понять по ходу разговора и тону,
что происходит, и выбрать один из трёх исходов:
  ANSWER — вопрос ясен и по базе знаний можно ответить → готовый ответ вслух (EN)
  ASK    — что-то явно спрашивают/просят, но это не факт из базы (нужно решение
           самого пользователя) → короткая пометка "что хочет клиент" (RU)
  SKIP   — вопрос ещё не сложился, или это вообще не обращение к пользователю
           (видео, лекция, фон) → ничего не показываем
"""
import os
import glob
import queue
import threading
import time
from collections import deque

import anthropic

import config
import paths

KB_DIR = paths.resource_path("knowledge_base")

SYSTEM_PROMPT = """You're helping Oleksandr during a live call or job interview. He's on the spot and needs something he can say out loud in the next few seconds — not an essay.

What you know about him:
{knowledge}

Below is a rolling window of the recent conversation, most recent line last. Lines
marked THEM are picked up live by speech recognition from whatever audio is playing —
could be a real conversation with Oleksandr, or could be a YouTube video, a podcast, a
lecture, or background media not addressed to him at all. Lines marked YOU are answers
you already gave earlier in this same window — use them to know what's already been
covered, so you don't repeat yourself or re-answer something already settled.

Read it the way a person listening in real time would: use the whole flow and tone to
figure out where things stand, not just the last line in isolation — a question is
often built up over a couple of sentences, or someone rephrases mid-thought.

You have three ways to respond — pick exactly one, and start your reply with that
exact tag:

ANSWER: — a clear, direct question about Oleksandr (his background, skills,
experience, projects, rate, availability, anything he can just answer) has landed.
Write a real answer, not a one-word hint — but keep it TIGHT: 3-4 short sentences,
one clear point plus at most one concrete example. He's reading this off a screen
in a live call, under time pressure — anything longer than that and he won't
finish reading it before he needs to speak. Cut every sentence that isn't the
core point or its one example. He'll say it in his own words, not recite it
verbatim, so a short scaffold beats a full essay. First person, like he's
explaining it himself.
  - plain words, no "furthermore", "leverage", "robust", "seamlessly", no corporate buzzwords
  - keep it EXTREMELY SIMPLE: short sentences (5-10 words each), everyday words, no
    fancy or clever vocabulary and no complicated terms — even if a fancier word
    exists, use the plain one instead. He has to say this out loud live, under
    pressure — a word he'd stumble on or a sentence with three clauses does him
    no good, no matter the language
  - don't sound smart or impressive — sound like an ordinary person casually explaining
    something to a colleague, not a thought-leader post or a textbook. No abstract
    framing sentences ("the model gets to decide what happens next", "that separation
    is deliberate") — just say plainly what he built and what it does, like describing
    it to a friend who isn't in tech
  - don't wrap it up with a neat summary sentence at the end
  - it's fine to start with "yeah" or "so" — real speech isn't perfectly structured
  - be specific, use his actual projects and background from above — don't stay vague
  - if he's asked about a tool he hasn't personally used, check the adjacent-tools notes
    above first — if it's in the same category as something he knows deeply, answer
    confidently: "haven't used X specifically, but I've done a lot with Y which works
    the same way, so I'd be productive fast" — that's an honest, normal thing to say in
    an interview, not a guess
  - if there's genuinely no real fact or adjacent skill to answer with, say something
    honest instead of inventing one
  - write it in {answer_language}, since that's the language he'll actually say it in out loud

ASK: — someone is clearly asking or requesting something from Oleksandr, but it's
not a simple fact you can just answer for him — it needs his own judgment (price
negotiation, personal opinion, a decision, something ambiguous, or you just don't have
enough to go on). Instead of guessing, write ONE short plain sentence saying what they
want, so he can see it at a glance and answer it himself — plain everyday words, no
fancy terms, he needs to grasp it in one glance mid-conversation. Example: "They want
to know your day rate for a 3-month contract." Write this one in Russian, since it's a
note for Oleksandr, not something he says out loud.

SKIP — nothing worth surfacing right now: the point isn't fully made yet (still
talking, still building up to it), it's not actually addressed to him (narration, a
lecture, a rhetorical question, unrelated background media), or it's a rephrase /
continuation of something already covered by an earlier ANSWER or ASK in this window.

Output the tag followed by a space, then the content — nothing else, no quotes, no
extra labels. For SKIP, output exactly the word SKIP and nothing else."""

SKIP_MARKER = "SKIP"
ANSWER_PREFIX = "ANSWER:"
ASK_PREFIX = "ASK:"


def load_knowledge() -> str:
    parts = []
    for path in sorted(glob.glob(os.path.join(KB_DIR, "*.md"))):
        if os.path.basename(path).lower() == "readme.md":
            continue
        with open(path, encoding="utf-8") as f:
            parts.append(f"## {os.path.basename(path)}\n{f.read()}")
    return "\n\n".join(parts)


class InterviewAssistant(threading.Thread):
    """Копит контекст разговора → на каждую новую реплику спрашивает Claude,
    сложился ли вопрос и как на него ответить → очередь ответов (или ничего)."""

    def __init__(self, input_queue: queue.Queue, output_queue: queue.Queue,
                 stop_event: threading.Event, status_queue: queue.Queue = None):
        super().__init__(daemon=True)
        self.inp = input_queue
        self.out = output_queue
        self.stop_event = stop_event
        self.status = status_queue
        self.client = None
        self.knowledge = ""
        self.context = deque(maxlen=config.AI_CONTEXT_TURNS)

    def _emit(self, kind, value):
        if self.status is not None:
            try:
                self.status.put_nowait((kind, value))
            except queue.Full:
                pass

    def _answer_language(self, lang_code: str) -> str:
        # Олександр отвечает на том же языке, на котором говорит клиент —
        # lang_code приходит с каждой репликой (авто-определён Whisper'ом
        # или выбран вручную), а не читается из глобального конфига
        return config.CLIENT_LANGUAGES.get(lang_code, {}).get("name", "English")

    def run(self):
        if not config.ANTHROPIC_API_KEY:
            self._emit("ai_error", "No Anthropic key in .env")
            return
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.knowledge = load_knowledge()

        while not self.stop_event.is_set():
            try:
                item = self.inp.get(timeout=0.3)
            except queue.Empty:
                continue
            # разбираем всё, что накопилось в очереди — ничего не теряем из контекста
            batch = [item]
            try:
                while True:
                    batch.append(self.inp.get_nowait())
            except queue.Empty:
                pass
            self.context.extend(f"THEM: {text}" for text, _lang in batch)
            current_lang = batch[-1][1]  # язык самой свежей реплики — на нём и отвечаем

            self._emit("ai_state", "thinking")
            transcript = "\n".join(self.context)
            # сбой одного запроса (лимит, обрыв сети, странный формат ответа) не
            # должен убивать поток и не должен засорять диалог мусорной строкой —
            # просто пропускаем эту реплику и едем дальше
            try:
                resp = None
                last_err = None
                for attempt in range(2):  # короткая сеть моргнула — пробуем ещё раз
                    try:
                        resp = self.client.messages.create(
                            model=config.AI_MODEL,
                            # ответ теперь короткий (3-4 предложения) — меньший
                            # лимит и меньше времени на генерацию; запас всё
                            # равно есть на случай, если часть бюджета уйдёт
                            # на внутренние раздумья модели
                            max_tokens=500,
                            system=SYSTEM_PROMPT.format(
                                knowledge=self.knowledge,
                                answer_language=self._answer_language(current_lang),
                            ),
                            messages=[{"role": "user", "content": transcript}],
                        )
                        break
                    except Exception as e:
                        last_err = e
                        if attempt == 0:
                            time.sleep(0.7)
                if resp is None:
                    raise last_err
                # ответ может содержать не только текстовый блок (например,
                # блок размышлений) — берём именно текстовые куски, не content[0]
                answer = "".join(
                    b.text for b in resp.content if b.type == "text"
                ).strip()

                if answer == SKIP_MARKER:
                    pass  # ничего не показываем
                elif answer.startswith(ANSWER_PREFIX):
                    text = answer[len(ANSWER_PREFIX):].strip()
                    self.out.put(text)
                    self.context.append(f"YOU (answered): {text}")
                elif answer.startswith(ASK_PREFIX):
                    text = answer[len(ASK_PREFIX):].strip()
                    self.out.put(f"🎯 Wants to know: {text}")
                    self.context.append(f"YOU (noted what they want): {text}")
                elif answer:
                    # подстраховка на случай неожиданного формата ответа
                    self.out.put(answer)
                    self.context.append(f"YOU: {answer}")
            except Exception as e:
                self._emit("ai_error", f"AI request hiccup, continuing: {e}")
            self._emit("ai_state", "ready")
