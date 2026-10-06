# Multi-Market Rental Support Bot

A modular, production-minded chatbot flow for a fictional international
rental/mobility company, built to practice the skills behind an
"AI Bot Consultant" role: market rollout, bot behavior design, localized
knowledge base management, compliance, and performance analytics.

Runs with **zero LLM API keys** (uses a deterministic `MockProvider`),
so the conversation logic can be tried without paying for a model. Real
providers (OpenAI, Anthropic) are optional drop-in upgrades controlled by
environment variables. **LangSmith tracing is required**: every
conversation turn is traced, and the bot refuses to start without a working
`LANGSMITH_API_KEY`.

## Quick start

```bash
git clone <this-repo>
cd support-bot
cp .env.example .env
# fill in LANGSMITH_API_KEY (required)
pip install -r requirements.txt
python main.py
```

On startup the bot checks the LangSmith connection and creates the
`support-bot` project if it doesn't exist yet. It then asks for the market
(de, es, us) and the rental supplier (Avis, SIXT, Enterprise), because the
answer to most questions depends on both. To use real LLM providers,
also fill in `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY` in `.env`.

Type `report` inside the chat session at any point to see the analytics
summary (KB gap rate, escalation rate, estimated cost). The report covers
every turn stored in `support_bot.db`, so it includes earlier sessions, not
only the current one.

## Architecture

```
support-bot/
├── config.py             # all settings: markets, suppliers; forces LangSmith tracing on
├── exceptions.py          # shared exception hierarchy for precise error handling
├── main.py                 # CLI entry point
├── models/
│   ├── llm_client.py        # provider chain + retry + fallback + graceful degrade
│   └── disclosure.py         # enforces "I'm an AI" in code, not just a prompt
├── knowledge/
│   ├── kb_loader.py           # loads the KB version in effect for one market + supplier
│   └── retriever.py            # weighted keyword retrieval + KB-gap detection
├── flow/
│   ├── intents.py               # rule-based intent classification
│   └── flow_engine.py            # orchestrates one full conversation turn; builds the prompt
├── compliance/
│   └── rules.py                   # fail-CLOSED escalation rules per market
├── observability/
│   ├── langsmith_setup.py          # startup check + creates the LangSmith project
│   ├── tracing.py                  # one LangSmith trace per turn (always on)
│   └── logger.py                    # SQLite logging with flat-file fallback
├── analytics/
│   └── dashboard.py                  # reads the log, reports gaps/cost/escalations
└── knowledge_base/
    ├── CHANGELOG.md                  # what changed in each KB version, and why
    ├── SOURCES.md                    # where every FAQ fact comes from, and known gaps
    ├── de/
    │   ├── avis/2026-10-06.md        # one dated file per KB version
    │   ├── enterprise/2026-10-06.md
    │   └── sixt/2026-10-06.md
    ├── es/  (same layout)
    └── us/  (same layout)
```

Each module does one job and can be read/reviewed independently — you
don't need to understand LangSmith to review `flow_engine.py`.

## How this maps to real "AI Bot Consultant" requirements

| Requirement | Where it lives |
|---|---|
| Market deployment & prioritization | `knowledge_base/{market}/{supplier}/`, `config.SUPPORTED_MARKETS`, `config.SUPPLIER_NAMES` — a new market is a folder, a new supplier is one folder per market plus one line in `config.py` |
| Feature design & bot behavior | `flow/intents.py` + `flow/flow_engine.py` — the actual conversation logic, readable without touching the LLM layer |
| Knowledge base management | `knowledge/kb_loader.py` — plain markdown, one dated file per KB version for each market and supplier; `knowledge_base/CHANGELOG.md` records what changed and why, `knowledge_base/SOURCES.md` the source of every fact |
| Compliance & localization | `compliance/rules.py` (fail-closed escalation) + `models/disclosure.py` (AI disclosure enforced in code) + per-market tone in `flow_engine._escalation_response` |
| Performance analytics | `analytics/dashboard.py` — KB gap rate, escalation rate, and cost per market and supplier from real logged data |

## Design decisions worth explaining in an interview

- **Retry, then fallback, then degrade — never crash.** `models/llm_client.py`
  retries the primary provider on transient errors, falls through to a
  secondary provider, and if both fail, returns a safe human-handoff
  message instead of raising an exception. Auth errors skip retries
  entirely, since retrying a bad key never helps.
- **Compliance fails CLOSED.** If the compliance rule engine itself throws
  an unexpected error, `flow_engine.py` treats that as "escalate to a
  human" rather than "skip the check." Getting this backwards turns a bug
  into a compliance incident.
- **AI disclosure is structural, not a prompt instruction.** Every response
  passes through `disclosure.py`, so you can prove compliance with a code
  path instead of trusting the model to remember to mention it.
- **Logging failures never break the conversation.** If SQLite is
  unavailable, `observability/logger.py` falls back to a flat JSONL file
  rather than raising — you lose query-ability, never data, and the user
  never sees a broken bot because of a logging problem.
- **KB gaps are a first-class signal, not a silent failure.** When nothing
  in the knowledge base clears the match threshold, that's logged
  explicitly (`KBGapError`) and surfaced in the analytics report as a
  candidate for a new KB entry — this is what "identify knowledge gaps"
  actually looks like in code.
- **Tracing is mandatory and fails fast.** `observability/langsmith_setup.py`
  verifies the key and endpoint at startup and creates the project if it
  doesn't exist; a missing key, missing SDK, or rejected login stops the
  bot with a clear message instead of producing conversations nobody can
  inspect later.
- **Answers are per supplier, never mixed.** Cancellation fees and late-
  return rules differ between Avis, SIXT and Enterprise and between
  markets, so each combination has its own FAQ file and a question is only
  ever answered from the customer's supplier. The bot asks for the supplier
  at the start of a session; a real deployment would read it from the
  booking number. The FAQ content is taken from the suppliers' public help
  pages (see `knowledge_base/SOURCES.md`) and must be re-checked before
  real use.
- **Knowledge base versions are dated files, never edits.** A policy
  change is a new file named after the day it takes effect
  (`knowledge_base/de/sixt/2026-11-01.md`); the loader uses the newest
  file dated today or earlier. Old versions stay on disk, so you can always
  show what the bot was allowed to say on a given day, prepare an announced
  change in advance with a future date, and roll back by deleting one
  file. Each LangSmith trace records the KB version that answered, and
  `python knowledge/kb_loader.py` lists which version is live everywhere.
- **One trace per conversation turn, not per model call.** Every customer
  message becomes a `conversation_turn` trace in the `support-bot` project,
  with the customer message as input and the bot's reply as output. The
  model call is a step inside it (prompt, reply, model, token counts).
  Because the trace wraps the whole turn, messages that are escalated
  before any model is called are visible too. Turns are tagged
  `market:<market>` and `supplier:<supplier>`, plus `escalated` and `kb_gap`
  where they apply, so
  each can be filtered in LangSmith.
- **Keyword retrieval that knows its limits.** `knowledge/retriever.py`
  scores FAQ entries by shared words, with six rules that each fix a wrong
  answer seen in testing: accents are folded ("devolucion" = "devolución"),
  synonyms map customer words to the FAQ's words ("estatus" → "estado"),
  word forms are matched ("later" = "late", "Status" finds
  "Buchungsstatus"), a word counts for less the more entries contain it
  ("booking" alone never matches), a word in an entry's heading counts
  double, and words the FAQ never uses don't dilute the score, so long
  questions match as well as short ones. Ties return all tied entries instead of the first
  one in the file.
- **Reply rules live in the prompt, with their reasons.** `flow_engine.py`
  tells the model to write plain text (the chat window doesn't render
  Markdown) and to keep time limits, amounts and conditions exactly as the
  FAQ states them. These are instructions, not guarantees: unlike the AI
  disclosure, they are not enforced in code.
- **Region-aware LangSmith endpoint.** LangSmith defaults to its US
  endpoint; EU-hosted accounts need `LANGSMITH_ENDPOINT` set explicitly
  (defaults to `https://eu.api.smith.langchain.com` in `config.py`) or
  traces fail to authenticate silently. This is set on both the
  `LANGSMITH_*` and legacy `LANGCHAIN_*` env vars *and* passed explicitly
  to the `Client`, since different SDK versions read different names —
  a small but real "it works on my machine" trap worth knowing about.

## Known limitations (intentional, for a learning project)

- Intent classification and KB retrieval are keyword-based, not
  embeddings/ML-based — this keeps the project dependency-free and easy
  to review, but a production system would swap `knowledge/retriever.py`
  for a real vector store (the module docstring explains exactly what to
  change). Two consequences today: synonyms must be added by hand to
  `_SYNONYMS` when the gap report shows a question the FAQ does answer,
  and a question on another topic that happens to contain one
  FAQ-specific word still matches that entry without being recorded as a
  gap.
- Session state (`models/disclosure.py`) is in-memory and resets on
  restart — fine for a demo, would move to Redis/a DB in production.

## Next steps to extend this further

- Swap `flow/intents.py`'s keyword classifier for an LLM-based one and
  compare accuracy/cost trade-offs.
- Add an entry to each supplier file covering a new topic (pets,
  additional drivers, deposits), then use
  `analytics/dashboard.py`'s gap report to verify the gap disappears.
- Wire up real Voiceflow/Botpress flows that call into this backend via
  webhook, so the flow-builder UI and this orchestration logic work
  together.
