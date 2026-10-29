# Rollout simulation

This is a simulated rollout. The conversations were test questions written
during development, not customer traffic, so there are no real volumes or
rates here. Every finding below did happen in testing, and each one points
to the change it caused in this repository.

## Why Germany first

Germany combines the hardest requirements of the three markets, so a bot that
passes there needs mostly configuration for the others:

- **Transparency:** the EU AI Act's transparency rules (Article 50) require
  a chatbot to tell people they are talking to an AI. This applies in Spain
  too, but Germany was where the disclosure was built and checked first.
- **Formal register:** German support uses the formal "Sie", and a bot that
  slips into "du" reads as careless. Spanish ("tú") and English need less
  care here.
- **Hard language for keyword retrieval:** German compounds
  ("Buchungsstatus", "Bearbeitungsgebühr") and separable verbs
  ("gebe ... zurück") break simple word matching first, so problems show up
  early.

## Go-live criteria for each market

A market goes live only when all of these hold:

| Check | How it is verified |
|---|---|
| Every reply identifies itself as AI | Enforced in code (`models/disclosure.py`), so it cannot be skipped |
| Regulated topics go to a person | Test messages with the market's restricted words (accident, lawsuit, Unfall, accidente) must escalate |
| Core questions get the right supplier's entry | Retrieval test questions for cancel, change, booking status and late return pass for every supplier |
| The bot promises nothing it cannot do | Reviewed replies contain no "I will connect you" without an actual escalation |
| Known gaps are written down | Open gaps are listed in `knowledge_base/CHANGELOG.md` before launch |

## What happened, phase by phase

### Phase 1: Germany pilot with one FAQ per market

Escalation, the AI notice and gap recording worked. The first real gap was
recorded: "Kann ich meinen Hund mitnehmen?" (pets). It was closed in
phase 11.

### Phase 2: Spain

**Finding:** "¿Dónde puedo encontrar el estatus de mi reserva?" was neither
answered nor recorded as a gap. The FAQ says "estado", the customer wrote
"estatus", and the shared word "reserva" pulled in the cancellation entry
instead. This is worse than a gap, because the gap rate showed 0% while a
customer got no answer.

**Change:** the retriever gained synonyms, accent folding, and a rule that
words appearing in most entries ("reserva", "Buchung") do not count as
evidence (`knowledge/retriever.py`).

**Lesson:** a low gap rate is not proof of good answers. Answered
conversations need sampling too, not only the gap list.

### Phase 3: United States

**Finding:** "What would happen if I return the car 30 minutes later than the
agreed return time?" was recorded as a gap although the FAQ answers it. Long,
polite questions diluted the score below the threshold.

**Change:** words the FAQ never uses no longer count against a match, and
word endings are matched ("later" finds "late").

### Phase 4: answers per supplier

**Finding:** answers depend on the supplier as much as on the market. Prepaid
cancellation, for example, costs $50 or $150 with Avis in the US but is free
up to 7 days before pick-up with Enterprise in Germany.

**Change:** one FAQ file per market and supplier, built from the suppliers'
public help pages (`knowledge_base/SOURCES.md`), and a supplier choice at
the start of each chat.

Testing with the real supplier texts led to three more corrections:

- **Retrieval:** 13 of 81 test questions failed on the new texts. Heading
  words now count double and German compounds are matched; all 81 pass.
- **An overstated answer:** for SIXT Germany the bot said "Es gibt keine
  Kulanzzeit" and that 30 minutes late costs an extra day. SIXT's page only
  says it does not state a grace period. The KB text was corrected (see the
  CHANGELOG) and the prompt now forbids turning "not stated" into "does not
  exist".
- **Form of address:** the bot switched between "du" and "Sie" within one
  chat. The prompt now fixes the form of address per market.

### Phase 5: web chat

**Finding:** for an unanswered question the bot wrote "stelle ich Sie gerne
mit einem Mitglied unseres Teams in Verbindung", but nothing was escalated.
The customer would wait for a person who never comes.

**Change:** the bot may not offer actions it cannot take; for unanswered
questions it points to the supplier's customer service instead.

### Phase 6: follow-up questions

**Finding:** "Und bei zwei Stunden?" after a late-return question was matched
to the *cancellation* entry (through "24 Stunden").

**Change:** the bot keeps the last three turns and rewrites a follow-up into
a self-contained question before searching the FAQ (`flow/memory.py`).

### Phase 7: Botpress as a second channel

**Finding:** a chat platform runs in the cloud and needs to reach the bot over
the internet, but the web chat has no login and every message costs model
tokens.

**Change:** a separate channel API (`bot_api.py`) on its own port that
refuses requests without the key and caps requests per minute. The Botpress
flow asks for market and supplier with buttons and sends each question to
it. During the build, the condition Botpress generated for its error branch
was `true`, so every reply went down the error path; it became
`!!workflow.apiError`.

### Phase 8: retrieval by meaning, measured before switching

**Finding:** each phase so far had needed another hand-written matching
rule. Embeddings were the obvious fix, but the switch was decided on
numbers, not assumed: 51 labelled questions in three languages, against all
three suppliers (153 checks), including phrasings the keyword rules were
never tuned on and questions the FAQ does not answer
(`tests/evaluate_retrieval.py`).

| | Correct | Held-out phrasings | Gaps kept | Wrong entry |
|---|---|---|---|---|
| Keyword only | 116 | 33/69 | 38/39 | 19 |
| Vector only (LangChain + Chroma) | 119 | 44/69 | 32/39 | 30 |
| Hybrid (chosen) | 124 | 41/69 | 38/39 | 22 |

Vector search alone understood rephrasings ("Ich stehe im Stau…" found the
late-return entry without a shared word) but confused neighbouring topics
(cancel vs. change a booking) and answered questions the FAQ doesn't cover.

**Change:** hybrid retrieval: keyword matching first, the vector search only
when it finds nothing, at a threshold of 0.55, chosen on a plateau of the
threshold sweep rather than a single peak. If the model can't be loaded,
the bot falls back to keywords.

**Lesson:** the more modern method was not better on its own. Measuring
showed where each fails, and the combination keeps the keyword matcher's
gap handling while catching rephrasings.

### Phase 9: repeated gaps go to a person

**Finding:** in Botpress, a customer who asked three unanswerable questions
got the same "I don't have that information" three times, with no way out.

**Change:** after two unanswered questions in a row the bot hands over to a
person (`SUPPORT_BOT_MAX_CONSECUTIVE_GAPS`). The rule lives in the Python
bot, so the web chat and Botpress needed no change to their flows.

### Phase 10: dashboard, linked to LangSmith

**Finding:** the text report counted gaps but didn't say what to do about
them, and a gap's LangSmith trace showed *that* nothing matched but not
*why*: the list of scores is empty when no entry is used.

**Change:** a dashboard (`/dashboard`) with fallback and escalation rates per
market and supplier, and the unanswered questions grouped per market with a
next step each. Every question links to its latest trace, which now carries
the gap reason, for example "No knowledge base match above 0.55
(best 0.246)" for "Can I bring my cat with me?".

### Phase 11: closing the top gap (pets)

**Finding:** the dashboard listed pets as the most-asked unanswered topic, in
all three markets and for every supplier ("Kann ich meinen Hund
mitnehmen?", "¿Puedo llevar a mi perro?", "Can I bring my cat with me?").
Its trace showed the reason: best match 0.246, threshold 0.55.

**Change:** a new knowledge base version dated 2026-10-29 with a pets entry,
researched per supplier and market (`knowledge_base/CHANGELOG.md`,
`SOURCES.md`). The policies differ: Avis US allows pets at most locations
with a cleaning fee of up to $450, Avis Spain needs the office's prior
approval, Enterprise US requires a crate, and Enterprise publishes no pet
policy for Germany or Spain, so those entries say to ask the branch. SIXT
Spain sells a pet add-on that covers normal soiling; its page could not be
read automatically and was copied by hand.

Four words in the new texts pulled unrelated questions to the pets entry in
testing (for example "según" matched "segundo conductor"). They were
reworded before release, and the evaluation set gained seven pet questions.
With keyword matching alone, 131 of 168 checks are right with the new
version against 125 with the old one, and no question that was right
before is wrong now. With the hybrid retrieval the bot uses, 139 of 168
are right, every pet question included, and 0.55 is still on the
plateau of the threshold sweep (138–139 between 0.40 and 0.60), so the
threshold stays.

**Lesson:** a new entry can break questions that have nothing to do with it.
Re-running the evaluation set before every KB release is what catches that.

## How a gap becomes a fix

![How a gap becomes a fix: conversations feed the report, each failure is classified, and the fix is a new KB version, a matching rule, or a corrected text, followed by re-running the test questions](diagrams/improvement-loop.png)

Source: [diagrams/improvement-loop.mmd](diagrams/improvement-loop.mmd)

## Open findings

- **Topics not covered:** additional drivers, deposits, fuel and age limits
  are answered with "no information". These are the next KB entries.
- **SIXT cancellation fees:** the amounts are not on SIXT's help pages and
  still need to be added by hand.
- **Intent analytics:** late-return questions are counted as "unknown"
  because `flow/intents.py` has no intent for them. The intent only decides
  routing, so the answers are not affected.
- **Confident keyword mistakes are not corrected:** in hybrid mode the
  keyword matcher goes first, so "Is my reservation confirmed?" still lands
  on *Cancel a booking* through the word "reservation".
- **Rephrasings count as separate gaps:** "Kann ich meinen Hund mitnehmen?"
  and "Darf ich einen Hund mitbringen?" are listed as two questions on the
  dashboard. Grouping by meaning would use the same embedding model.
- **The evaluation set is small** (56 questions) and the retrieval settings
  were chosen on it, so its numbers are optimistic. Real questions from the
  gap list should be added before the settings change again.
