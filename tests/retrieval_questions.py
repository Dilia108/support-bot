"""
tests/retrieval_questions.py

Evaluation set for knowledge base retrieval: customer questions with the FAQ
topic that should answer them, or None when the KB has no answer (a gap the
bot must record, not paper over).

Every question is checked against all three suppliers of its market. All
supplier files cover the four core topics under the same headings. Topics
in OPTIONAL_TOPICS (added later, from the gap list) are only in some
supplier files: where a supplier has no such entry, the question is
expected to be a gap for that supplier.

held_out=True marks phrasings written after the keyword retriever was tuned.
The keyword rules were adjusted until the other questions passed, so only the
held-out ones say how a retriever copes with wording it has never seen.
"""

# FAQ heading per market for each topic.
HEADINGS = {
    "de": {"cancel": "Buchung stornieren", "change": "Buchung ändern",
           "status": "Buchungsstatus prüfen", "late": "Verspätete Rückgabe",
           "pets": "Haustiere mitnehmen"},
    "es": {"cancel": "Cancelar una reserva", "change": "Modificar una reserva",
           "status": "Consultar el estado de la reserva", "late": "Devolución tardía",
           "pets": "Mascotas en el coche"},
    "us": {"cancel": "Cancel a booking", "change": "Change a booking",
           "status": "Check booking status", "late": "Late return",
           "pets": "Pets in the car"},
}

# Added with the 2026-10-29 KB version. Every supplier has the pets entry
# today; a topic added later may start in only some supplier files.
OPTIONAL_TOPICS = {"pets"}

# (market, question, expected topic or None for a gap, held_out)
QUESTIONS = [
    # --- Germany ---
    ("de", "Wie kann ich meine Buchung stornieren?", "cancel", False),
    ("de", "Was kostet eine Stornierung?", "cancel", False),
    ("de", "Ich möchte meine Reservierung absagen, geht das kostenlos?", "cancel", True),
    ("de", "Bekomme ich mein Geld zurück, wenn ich die Reise nicht antrete?", "cancel", True),
    ("de", "Kann ich meine Buchung ändern?", "change", False),
    ("de", "Ich muss das Abholdatum verschieben.", "change", True),
    ("de", "Kann ich den Mietwagen einen Tag später abholen?", "change", True),
    ("de", "Wo sehe ich den Status meiner Buchung?", "status", False),
    ("de", "Ist meine Reservierung schon bestätigt?", "status", True),
    ("de", "Wo finde ich meine Buchungsdetails?", "status", True),
    ("de", "Was passiert, wenn ich das Auto 30 Minuten zu spät zurückgebe?", "late", False),
    ("de", "Gibt es eine Kulanzzeit bei verspäteter Rückgabe?", "late", False),
    ("de", "Ich stehe im Stau und bin eine Stunde später an der Station.", "late", True),
    ("de", "Kostet es extra, wenn ich den Wagen erst abends zurückbringe?", "late", True),
    ("de", "Kann ich meinen Hund mitnehmen?", "pets", False),
    ("de", "Darf meine Katze im Mietwagen mitfahren?", "pets", True),
    ("de", "Darf ich ein Hund mit bringen?", "pets", True),
    ("de", "Kann ich einen zweiten Fahrer zu meiner Buchung hinzufügen?", None, False),
    ("de", "Was sind die Kosten für einen Kindersitz?", None, False),
    ("de", "Darf ich mit dem Mietwagen nach Polen fahren?", None, True),
    ("de", "Wie hoch ist die Kaution?", None, True),
    # --- Spain ---
    ("es", "¿Cómo puedo cancelar mi reserva?", "cancel", False),
    ("es", "¿Me devuelven el dinero si anulo la reserva?", "cancel", True),
    ("es", "Ya no voy a viajar, ¿qué hago con mi reserva?", "cancel", True),
    ("es", "¿Puedo modificar mi reserva?", "change", False),
    ("es", "Quiero cambiar las fechas de mi reserva", "change", False),
    ("es", "¿Puedo recoger el coche un día más tarde?", "change", True),
    ("es", "¿Dónde puedo encontrar el estatus de mi reserva?", "status", False),
    ("es", "¿Dónde veo el estado de mi reserva?", "status", False),
    ("es", "¿Está confirmada mi reserva?", "status", True),
    ("es", "¿Qué pasaría si devuelvo el auto 30 minutos más tarde de lo acordado?", "late", False),
    ("es", "que pasa con una devolucion tardia", "late", False),
    ("es", "Hay mucho tráfico y llegaré una hora tarde a devolver el coche.", "late", True),
    ("es", "¿Puedo llevar a mi perro?", "pets", False),
    ("es", "¿Se admiten gatos en el coche de alquiler?", "pets", True),
    ("es", "¿Puedo añadir un segundo conductor a mi reserva?", None, False),
    ("es", "¿Cuánto es la fianza?", None, True),
    ("es", "¿Puedo cruzar a Portugal con el coche?", None, True),
    # --- United States ---
    ("us", "which is the process to follow in order to cancel my booking?", "cancel", False),
    ("us", "How do I cancel?", "cancel", False),
    ("us", "Is there a fee if I cancel my prepaid reservation?", "cancel", False),
    ("us", "My trip got called off, can I get my money back?", "cancel", True),
    ("us", "Can I change my booking?", "change", False),
    ("us", "I need to modify the dates of my reservation", "change", False),
    ("us", "Can I pick up the car a day later?", "change", True),
    ("us", "where can I find the status of my booking?", "status", False),
    ("us", "Is my reservation confirmed?", "status", True),
    ("us", "what would happen if I return the car 30 minutes later than the agreed return time?", "late", False),
    ("us", "Is there a grace period if I'm late?", "late", False),
    ("us", "I'm stuck in traffic and will drop the car off an hour late.", "late", True),
    ("us", "Can I bring my dog?", "pets", False),
    ("us", "Can I bring my cat with me?", "pets", True),
    ("us", "Is there a fee if my dog leaves hair in the car?", "pets", True),
    ("us", "Can I add a second driver to my booking?", None, False),
    ("us", "How much is the security deposit?", None, True),
    ("us", "Can I drive the car into Canada?", None, True),
]
