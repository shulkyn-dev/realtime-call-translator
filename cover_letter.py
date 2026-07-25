"""Генерация cover letter под вакансию — вкладка "Cover Letter".

Два отдельных режима, оба через один и тот же диалог (cover_messages):
1) analyze — бот сначала честно разбирает вакансию: что нужно, что реально
   совпадает с навыками Олександра, чего не хватает, и вердикт. Пользователь
   может поправить бота репликой ("я знаю аналог такого-то инструмента"),
   прежде чем соглашаться с оценкой.
2) write — только когда пользователь жмёт "Generate Cover Letter", бот
   пишет само письмо по фреймворку курса (гачок → доказательства →
   highlight → CTA), с NOT_A_FIT-гейтом как последним рубежом на случай,
   если фит так и не сошёлся даже после обсуждения.

Кейсы и навыки берутся из той же knowledge_base/, что и ИИ-подсказки на
собеседовании (assistant.py) — общая база, не дублируем.
"""
import threading
import queue
import time

import anthropic

import config
from assistant import load_knowledge

# ориентир по длине — модель сама решает, к какому случаю ближе вставленный
# текст, и подбирает длину; пользователю ничего выбирать не нужно
LENGTH_GUIDE = """- Casual freelance/Upwork-style project post: 100-150 words
- Formal LinkedIn-style full-time job posting: 80-150 words
- Very short Telegram/direct message about a quick task: 50-80 words
- Long, detailed enterprise job description: up to 200 words"""

# фразы, которые сразу выдают, что письмо писал ИИ — из реального разбора ошибок
FORBIDDEN_PHRASES = [
    "I am excited to apply", "I am a passionate professional",
    "I have extensive experience in", "Please find my proposal attached",
    "I believe I would be a great fit", "Looking forward to hearing from you",
    "Feel free to reach out", "Thank you for your consideration",
    "I am confident that", "With my expertise in", "Furthermore,", "Moreover,",
    "Additionally,", "In conclusion,", "It is worth noting that",
    "I look forward to hearing from you", "Please don't hesitate to contact me",
    "I would be happy to discuss further", "Dear Sir/Madam",
    "I am writing to express",
]

ANALYZE_SYSTEM_PROMPT = """You are helping Oleksandr decide whether a job posting or freelance task is worth a cover letter — before any letter gets written.

What you know about him:
{knowledge}

He'll paste a job posting or project description below. Read it the way an expert would: figure out the REAL problem behind their words, not just what they literally listed.

Reply with a short, honest analysis — not a cover letter:
1. One line on what the role/task actually needs.
2. What genuinely overlaps with his real skills above — be specific, name the actual tools/experience that match (including adjacent-tools reasoning, e.g. deep Make.com experience carries over to n8n/Zapier).
3. Anything missing or uncertain — say it plainly, don't paper over real gaps.
4. A one-line verdict:
   - Solid fit, or fit via adjacent skills → say so plainly, ready to write.
   - Clearly not his lane → don't soften it. List the specific mismatches (name the actual things that don't line up, not a vague "not a fit") and end with a direct "пропускаем" — he shouldn't waste a cover letter on it.

This is a conversation: he may push back or add context — "I've actually used a close analog" or "here's a case that fits better." Take corrections seriously and re-evaluate honestly each time; don't cave just because he pushed back, but don't stay stubborn if the new information genuinely changes the picture.

He may also attach a screenshot instead of (or along with) text — usually extra
application-form questions from a job site (Upwork screening questions, a LinkedIn
application form, a custom questionnaire) that need direct answers, not a cover
letter. When that happens: read the screenshot, pull out each question exactly as
asked, and give him a direct, ready-to-use suggested answer for each one — grounded
in his real background above, honest about gaps, same plain style as everything
else in this chat. If the screenshot is something else (the vacancy itself, a
company page, a form he's unsure about), just react to what's actually in it.

Always reply in Russian, regardless of what language the posting itself is in — this analysis is for Oleksandr to read quickly, not for the client.

Keep it tight (under ~120 words), plain and direct — no hook, no CTA, no letter, just the honest read. No tags, no preamble, just the analysis itself."""

WRITE_SYSTEM_PROMPT = """You are a top-tier freelance copywriter — the kind clients pay premium rates for specifically because your proposals get replies, not silence. You've read thousands of job postings and you know the difference between a cover letter that sounds like everyone else's and one that makes a client stop scrolling. You're writing this one for Oleksandr, following the exact framework that already works for him — not a generic template.

What you know about him:
{knowledge}

Above this message, he already discussed the posting with you — an analysis of the fit, maybe some back-and-forth corrections. That conversation already settled whether this is worth pursuing. He's now asking you to write the actual cover letter based on it.

Before writing, do one last honest check: given everything discussed, is this something he could credibly do? If the conversation genuinely shows it's not his lane, don't write a letter pretending otherwise — start your reply with exactly:
NOT_A_FIT: <one honest, specific sentence — what the role actually needs and why that's not his lane>
and output nothing else.

Otherwise, start your reply with exactly:
COVER_LETTER:
then the letter itself, following the rules below.

RULES:
1. Default to English — his clients typically read English, regardless of what language the posting itself is in or what language the discussion above used. Exception: if Oleksandr explicitly asked for a different language somewhere in the conversation above (e.g. "write it in Russian" / "на українській"), use that language instead.
2. NEVER start with "I", "Hello, my name is", or "I am excited to apply" — open with a HOOK instead: reference something SPECIFIC from the posting (their tool, their exact problem, a number) in the first 1-2 lines. Or mirror their pain point back in your own words. Or lead with a concrete result from a similar project.
3. Structure: HOOK -> PROOF -> one HIGHLIGHT that sets him apart -> CTA.
4. The PROOF must include a hard number — hours saved, a speed multiplier, a dollar figure, a count of something. "Increased efficiency" or "streamlined the process" is a failure grade — that's the exact vague language a mediocre proposal uses. Pull the closest honest number from his background rather than dropping it entirely.
5. Frame everything around what the CLIENT gets, not Oleksandr's resume. "I built 40 Make scenarios" is weak. "Your lead pipeline goes from 4 hours of manual work to 30 minutes" is strong. Reframe every sentence through the client's benefit, not his.
6. The closing question must be so specific to THIS posting that it couldn't be pasted into any other cover letter. Not "can we discuss your stack" — ask about the one detail they mentioned that proves you actually thought about their setup. Pair it with a concrete day/time — never "happy to hop on a call" alone.
7. Figure out the target length yourself from what kind of posting this reads like — don't ask, don't ramble, cut anything that doesn't add value:
{length_guide}
8. Never use any of these phrases or anything close to them, they scream "AI wrote this": {forbidden}
9. No bullet list with 5+ items at the start. No bold headers stuffed into a short message.
10. Before you finalize, check your own draft: is there a hard number in the proof? Is the closing question specific enough that it can't be reused elsewhere? Any AI-tell phrasing? If any answer is no, rewrite that part — don't ship a weak draft.

Output nothing beyond the required tag (NOT_A_FIT: or COVER_LETTER:) and its content — no extra preamble, no quotes, no explanation."""

NOT_FIT_PREFIX = "NOT_A_FIT:"
COVER_LETTER_PREFIX = "COVER_LETTER:"


def _send(system_prompt: str, messages: list, result_queue: queue.Queue, parse_tags: bool):
    """Общий воркер для обоих режимов — отличаются только системным промптом
    и тем, нужно ли парсить NOT_A_FIT/COVER_LETTER теги в ответе."""

    def _run():
        if not config.ANTHROPIC_API_KEY:
            result_queue.put(("error", "No Anthropic key in .env", ""))
            return
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        last_err = None
        for attempt in range(2):  # короткая сеть моргнула — пробуем ещё раз
            try:
                resp = client.messages.create(
                    # запас на случай, если модель тратит часть бюджета на
                    # внутренние раздумья перед ответом в спорных случаях —
                    # иначе текст обрывается пустым, а статус всё равно "ok"
                    model=config.AI_MODEL,
                    max_tokens=2000,
                    system=system_prompt,
                    messages=messages,
                )
                raw = "".join(
                    b.text for b in resp.content if b.type == "text"
                ).strip()
                if not raw:
                    result_queue.put(("error", "Model returned an empty response — try again", ""))
                elif not parse_tags:
                    result_queue.put(("ok", raw, raw))
                elif raw.startswith(NOT_FIT_PREFIX):
                    reason = raw[len(NOT_FIT_PREFIX):].strip()
                    result_queue.put(("not_fit", reason, raw))
                else:
                    text = raw[len(COVER_LETTER_PREFIX):].strip() if raw.startswith(
                        COVER_LETTER_PREFIX) else raw
                    result_queue.put(("ok", text, raw))
                return
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.7)
        result_queue.put(("error", str(last_err), ""))

    threading.Thread(target=_run, daemon=True).start()


def send_analysis_async(messages: list, result_queue: queue.Queue):
    """Разбор вакансии/задачи — что нужно, что совпадает, вердикт. Не письмо."""
    knowledge = load_knowledge()
    system = ANALYZE_SYSTEM_PROMPT.format(knowledge=knowledge)
    _send(system, messages, result_queue, parse_tags=False)


def send_letter_async(messages: list, result_queue: queue.Queue):
    """Собственно письмо — вызывается только по кнопке Generate, когда
    обсуждение фита уже состоялось выше в той же истории сообщений."""
    knowledge = load_knowledge()
    forbidden = "; ".join(FORBIDDEN_PHRASES)
    system = WRITE_SYSTEM_PROMPT.format(
        knowledge=knowledge, length_guide=LENGTH_GUIDE, forbidden=forbidden
    )
    _send(system, messages, result_queue, parse_tags=True)
