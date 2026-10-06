# Knowledge base changelog

Every knowledge base version is a dated file:
`knowledge_base/<market>/<supplier>/<YYYY-MM-DD>.md`. The date is the day the
version takes effect. The bot always answers from the newest file whose date
is today or earlier, and older files stay in place as the record of what the
bot said at the time.

Run `python knowledge/kb_loader.py` to see which version is live for every
market and supplier.

## How to release a change

1. Copy the newest file of that market and supplier to a new file named
   after the date the change takes effect (today, or a future date for an
   announced policy change).
2. Edit the new file. Never edit a file that is already in effect, or the
   history stops matching what customers were told.
3. Add an entry below: date, market/supplier, what changed, why, and the
   source. Update `SOURCES.md` if the source page changed.
4. Restart the bot. To roll back, delete the new file and note it here.

Entry types: **Added** (new topic or new supplier), **Changed** (policy or
wording changed), **Corrected** (the previous text was wrong), **Removed**.

## 2026-10-06

First versioned release. All nine files are dated 2026-10-06.

- **Added, all markets and suppliers (Avis, SIXT, Enterprise in DE, ES,
  US).** Four topics per supplier: cancel a booking, change a booking,
  check booking status, late return.
  *Why:* answers differ by supplier as well as by market, most of all for
  prepaid cancellations and late returns, so one FAQ per market gave
  answers that were wrong for most bookings.
  *Source:* the suppliers' public help pages, listed in `SOURCES.md`.
- **Removed, all markets: `faq.md`.** The single FAQ per market described a
  made-up company (free cancellation up to 24 hours before the rental, a
  15 EUR / $15 fee after that, no late fee up to 59 minutes). None of these
  figures match a real supplier.
- **Corrected before release, `de/sixt` and `us/sixt`, late return.** The
  first draft said SIXT's help pages "state no grace period". In testing,
  the bot turned this into "there is no grace period" and told a customer
  that 30 minutes late costs an extra day, which the source does not say.
  The text now says the pages do not state whether a grace period applies
  to short delays and points to SIXT support.
- **Changed before release, wording only, `us/sixt`, `us/enterprise`,
  `es/sixt`, `es/enterprise`, `es/avis`.** Replaced incidental words that
  caused wrong matches in retrieval ("bring back" became "return",
  "entregar" became "devolver", "modificar tu contrato" became "ajustar tu
  contrato"). No policy content changed.

### Known gaps at this version

Questions on these topics are answered with "no information" and recorded
as knowledge gaps: pets, additional drivers, deposits, fuel, age limits.
Missing figures inside the covered topics are listed in `SOURCES.md`.
