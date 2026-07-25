# AI Engineering Interview Topics

> Prep for AI-interview platforms (micro1 and similar). Topics: Agentic AI Workflows,
> Prompt & Context Engineering, AI Output Evaluation, AI-Assisted Problem Solving.
> Everything below is grounded in my real projects — use these as first-person talking
> points, not textbook definitions.

## Agentic AI Workflows

How I'd define it in plain words: a regular automation follows a fixed path — trigger,
steps, done. An agentic workflow gives the model a decision: it looks at the input and
context and chooses what to do next — answer, ask, use a tool, or do nothing. The skill
is deciding WHERE the model gets a vote and where the flow stays deterministic.

My real examples:
- I built a desktop AI assistant that listens to live calls and on every new phrase
  decides between three outcomes: ANSWER (question is clear, knowledge base has the
  facts), ASK (they want something that needs my judgment — surface a one-line note
  instead of guessing), or SKIP (not addressed to me, or the question hasn't fully
  formed yet). That three-way gate is what keeps it from hallucinating answers to
  half-finished sentences.
- A cover-letter agent with a fit-check gate: before writing anything it honestly
  evaluates whether the job matches my real skills, and refuses (NOT_A_FIT with a
  specific reason) rather than stretching. Then it's a multi-turn conversation — I can
  push back with corrections and it re-evaluates without just caving in.
- An AI booking assistant for a massage studio: Make.com + OpenAI + Setmore + FB
  Messenger — the model handles the conversation, deterministic scenario handles
  calendar writes. LLM never touches the booking system directly; it only fills
  validated slots. That separation is deliberate.
- CRM funnels on Make.com (4 connected scenarios: ClickFunnels webhook → ActiveCampaign
  → Pipedrive → cal.com) — mostly deterministic, with AI only where interpretation is
  needed. Not everything should be an agent; a webhook-to-CRM sync must never "decide".

On guardrails (I bring this up myself — interviewers like it): any agent that touches
real systems needs scope limits — what it can never do, validation of its outputs
before they hit an API, and treating user text as data, not instructions (prompt
injection). In my booking bot the model can propose a slot but the scenario validates
it against the calendar before writing.

RAG vs fine-tuning: for personal/company knowledge I use RAG — my call assistant loads
a folder of markdown facts into the system prompt fresh on every request. Zero training
cost, instantly editable, no stale weights. Fine-tuning I'd reserve for style/format at
scale, not for facts.

## Prompt & Context Engineering

My core techniques, all from shipped code:
- Tag-prefix protocols for machine-parseable outputs: I make the model start its reply
  with exact tags (ANSWER: / ASK: / SKIP, or NOT_A_FIT: / COVER_LETTER:) so code can
  route the response reliably without JSON-parsing fragility. Cheap structured output.
- Rolling context window: the call assistant keeps a deque of the last N conversation
  turns, including its own earlier answers marked as YOU — so it knows what's already
  covered and doesn't repeat itself. Context is curated, not dumped.
- Self-check inside the prompt: for cover letters the last instruction is "before you
  finalize, verify: is there a hard number in the proof? is the closing question too
  specific to reuse? any AI-tell phrasing? If no — rewrite." Quality gate built into
  the prompt itself, not a second API call.
- Negative constraints with concrete examples: I keep an explicit forbidden-phrases
  list ("I am excited to apply", "Furthermore", "Thank you for your consideration") —
  phrases that scream AI. Telling the model what NOT to sound like works better than
  "sound human".
- Language routing rules: analysis replies in Russian (for me to read fast), final
  letters in English (for clients), with an explicit override if I ask otherwise —
  all controlled in the system prompt, not by UI switches.
- Letting the model infer instead of adding UI: I removed a "platform" dropdown and
  replaced it with a length-guide reference in the prompt — the model reads the posting
  and picks the right length itself. Fewer controls, same quality.

Pitfalls I've actually hit and fixed:
- max_tokens vs internal reasoning: newer Claude models spend part of the token budget
  thinking before answering; with a tight limit the visible text can come back EMPTY
  while the API still returns success. Fix: raise the budget and add an explicit
  empty-response guard that surfaces an error instead of silently showing nothing.
- Environment shadowing: load_dotenv doesn't override existing OS env vars by default —
  a system-level API key silently shadowed the project's key. One flag (override=True),
  hours of debugging saved for the next person.

## AI Output Evaluation

My approach in one line: never trust a single good-looking output — build gates and
test with cases designed to fail.

Concrete practices from my projects:
- Negative test cases: when I added the job-fit gate, I tested it with vacancies that
  should be rejected (a nurse position, a licensed structural engineer) — the agent must
  say NOT_A_FIT with the real reason, not write a letter anyway. Passing only happy-path
  tests proves nothing.
- Honesty checks against pushback: I test whether the agent caves when I push back with
  weak arguments (it shouldn't) and whether it updates when I give genuinely new facts
  (it should). Both directions matter.
- Programmatic assertions, not vibes: after every prompt change I re-run scripted
  checks — does the output start with the required tag, is the length in range, does
  the proof contain a hard number, are forbidden phrases absent.
- Expert-review loop (LLM-as-judge style): I had the model review its own generated
  cover letters in the role of a top-tier copywriter — it found systematic flaws
  (vague proof without numbers, reusable closing questions), which I then turned into
  hard rules in the prompt. Review findings become permanent constraints.
- Verifying the model actually did the work: when I added screenshot input, my headless
  test rendered text the model couldn't read — and it SAID so, describing the exact
  render artifact. That confirmed vision was really analyzing pixels, not bluffing.
- Guard the pipeline, not just the model: empty response → explicit error status, not
  a silent blank field; API hiccup → one retry then a visible error; the UI never
  pretends success.

## AI-Assisted Problem Solving

I use Claude Code daily as my main development environment. My working loop:
1. Reproduce first — write a minimal script that triggers the bug before touching
   anything (e.g., when a "duplicated cover letter" was reported, I first proved the
   generation was correct and the bug was purely in UI rendering).
2. Diagnose the root cause, not the symptom — a wrong taskbar icon turned out to be
   Windows AppUserModelID grouping, not the icon file; an "empty letter" turned out to
   be token budget, not broken code.
3. Fix minimally, then verify programmatically — headless UI tests, asserts, checking a
   process is actually alive — not "looks fine to me".
4. Document the pitfall so it's never re-debugged (I keep a knowledge base of every
   gotcha: DLL load order, dotenv override, PyQt6 API renames).

Rule I follow with external systems: for Make.com fixes I export the scenario blueprint
as JSON, edit it with AI assistance, and import it back — I never let AI edit a live
scenario through the API directly (proven to break structure). Same principle
everywhere: AI drafts, deterministic process applies, human confirms the risky step.

How I answer "do you blindly trust AI output?": no — I treat it like a very fast junior
colleague. It writes, I review and test. The productivity comes from the loop being
fast, not from skipping verification.

## Quick facts if asked directly

- Main stack: Claude API (daily), OpenAI GPT-4, Make.com (deep), n8n, Zapier, Python,
  FastAPI, aiogram Telegram bots, Docker, Airtable, Shopify, Pipedrive, ActiveCampaign.
- Built and shipped: multi-scenario CRM funnels, AI chatbots (booking, qualification,
  business-card bots), a real-time speech translation + interview-assistant desktop app
  (faster-whisper on CUDA, DeepL, Claude, PyQt6), cover-letter agent with fit gate.
- RAG over fine-tuning for facts; structured outputs via tag protocols; guardrails and
  human-in-the-loop for anything that writes to real systems.
