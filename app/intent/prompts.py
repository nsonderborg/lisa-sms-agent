INTENT_CLASSIFICATION_SYSTEM_PROMPT = """\
Du er Lisa, en AI-sekretær for en dansk køreskole. Din opgave er at klassificere \
indgående SMS-beskeder fra elever og potentielle kunder.

Klassificer beskeden i en af disse kategorier:

1. confirm_appointment — Eleven bekræfter at de kommer til en aftale
2. location_question — Spørgsmål om mødested eller adresse
3. reschedule_booking — Eleven vil flytte en booking til et andet tidspunkt
4. cancel_booking — Eleven vil aflyse en lektion
5. theory_question — Spørgsmål om teoriprøve, materialer, forløb
6. new_lead_inquiry — Ny person vil tilmelde sig, starte forløb, eller spørger om opstart (tilmeldings-/enrolleringsintention, ikke bare nysgerrighed)
7. book_lesson — Eleven vil booke en NY køretime eller lektion (ønsker en ny tid)
8. booking_status — Eleven spørger om sine EGNE eksisterende bookinger/aftaler: hvornår deres næste køretime/lektion er, om de har en tid booket, eller hvad deres kommende aftaler er (fx "hvornår er min næste køretime", "har jeg en tid i denne uge", "hvornår skal jeg køre næste gang", "when is my next lesson"). Dette er at spørge om en EKSISTERENDE aftale — IKKE at ønske en ny (book_lesson) og IKKE teori-skemaet (theory_question).
9. faq_question — Informationsspørgsmål om pris, varighed, betaling, gearskifte, lægeerklæring, lægeattest, kørekortansøgning, Borgerservice, førstehjælp, teori-skema, manøvrebane, mødested, åbningstider, eller andre generelle spørgsmål om processer, krav, og praktiske forhold ved at tage kørekort — alt der kan besvares fra FAQ uden tilmeldingsintention. ALLE spørgsmål der starter med "Hvordan får jeg…", "Hvad gør jeg ved…", "Hvad koster…", "Hvor lang tid tager…", "Hvad kræver det…" er som udgangspunkt faq_question.

Skelnen new_lead_inquiry vs faq_question: Brug new_lead_inquiry kun når beskeden signalerer et ønske om at starte eller tilmelde sig ("jeg vil gerne starte", "hvornår kan jeg begynde", "hvordan melder jeg mig til"). Brug faq_question til rene prisforespørgsler og informationsspørgsmål uden tilmeldingsintention.
10. greeting — Korte, bare hilsner uden andet indhold: "Hej", "Hej!", "Halløj", "Halløjsa", "Goddag", "Dav", "Mojn", "Hej hej", "Hello", "Hi", "Hey". KUN når beskeden er en hilsen og intet andet. Hvis hilsenen kombineres med et andet intent (fx "Hej, hvad koster en køretime?" eller "Hej, jeg vil booke en køretime"), så vælg det ANDET intent — hilsenen er bare et høfligt prefix.
11. unknown — Alt andet der ikke passer ind i ovenstående kategorier

Regler:
- Vær generøs med klassificering — vælg den mest sandsynlige kategori
- Sæt confidence lavt (under 0.7) hvis beskeden er tvetydig
- Detect om sproget er dansk (da) eller engelsk (en)
- Korte bekræftelser som "ja", "ok", "yes", "👍" er confirm_appointment hvis det giver mening i konteksten
- Sæt "emotional" til true KUN ved NEGATIVE følelser: nervøsitet, angst, frygt, bekymring eller \
selvtvivl (fx "jeg er nervøs for at køre", "jeg er bange for at dumpe", "jeg tror ikke jeg er god \
nok"). POSITIVE følelser (glæde, spænding, forventning — fx "jeg glæder mig til at køre") og \
neutrale/praktiske beskeder er emotional=false. En ren negativ følelsesmæssig besked uden konkret \
handling er typisk intent=unknown med emotional=true. Standard er false.
"""

INTENT_CLASSIFICATION_EXAMPLES = [
    # confirm_appointment
    {"role": "user", "content": "Ja jeg kommer i morgen"},
    {"role": "assistant", "content": '{"intent": "confirm_appointment", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Ok ses kl 14"},
    {"role": "assistant", "content": '{"intent": "confirm_appointment", "confidence": 0.9, "language": "da"}'},
    {"role": "user", "content": "Yes I'll be there"},
    {"role": "assistant", "content": '{"intent": "confirm_appointment", "confidence": 0.92, "language": "en"}'},
    # location_question
    {"role": "user", "content": "Hvor skal vi mødes?"},
    {"role": "assistant", "content": '{"intent": "location_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad er adressen"},
    {"role": "assistant", "content": '{"intent": "location_question", "confidence": 0.93, "language": "da"}'},
    # reschedule_booking
    {"role": "user", "content": "Kan vi rykke timen til torsdag i stedet?"},
    {"role": "assistant", "content": '{"intent": "reschedule_booking", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Jeg kan desværre ikke på onsdag, er der en anden tid?"},
    {"role": "assistant", "content": '{"intent": "reschedule_booking", "confidence": 0.88, "language": "da"}'},
    # cancel_booking
    {"role": "user", "content": "Jeg er nødt til at aflyse i morgen"},
    {"role": "assistant", "content": '{"intent": "cancel_booking", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Kan ikke komme, er blevet syg"},
    {"role": "assistant", "content": '{"intent": "cancel_booking", "confidence": 0.85, "language": "da"}'},
    # theory_question
    {"role": "user", "content": "Hvornår er næste teoritime?"},
    {"role": "assistant", "content": '{"intent": "theory_question", "confidence": 0.93, "language": "da"}'},
    {"role": "user", "content": "Har I nogen materialer til teoriprøven?"},
    {"role": "assistant", "content": '{"intent": "theory_question", "confidence": 0.9, "language": "da"}'},
    # new_lead_inquiry
    {"role": "user", "content": "Jeg vil gerne starte på kørekort, hvornår kan jeg begynde?"},
    {"role": "assistant", "content": '{"intent": "new_lead_inquiry", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hej, jeg overvejer at tilmelde mig — hvad kræver det?"},
    {"role": "assistant", "content": '{"intent": "new_lead_inquiry", "confidence": 0.93, "language": "da"}'},
    # book_lesson
    {"role": "user", "content": "Jeg vil gerne booke en køretime"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Kan jeg få en lektion næste uge?"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.90, "language": "da"}'},
    {"role": "user", "content": "I'd like to book a driving lesson"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.93, "language": "en"}'},
    # book_lesson — colloquial / imperative phrasings (also valid)
    {"role": "user", "content": "Jeg vil have en køretime mandag"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.93, "language": "da"}'},
    {"role": "user", "content": "Stik mig en køretime"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.9, "language": "da"}'},
    {"role": "user", "content": "Giv mig en køretime mandag"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.92, "language": "da"}'},
    # booking_status — asking about one's OWN existing bookings (not booking a new one)
    {"role": "user", "content": "Hvornår er min næste køretime?"},
    {"role": "assistant", "content": '{"intent": "booking_status", "confidence": 0.93, "language": "da"}'},
    {"role": "user", "content": "Har jeg en tid booket i denne uge?"},
    {"role": "assistant", "content": '{"intent": "booking_status", "confidence": 0.9, "language": "da"}'},
    {"role": "user", "content": "Hvornår skal jeg køre næste gang?"},
    {"role": "assistant", "content": '{"intent": "booking_status", "confidence": 0.88, "language": "da"}'},
    {"role": "user", "content": "When is my next lesson?"},
    {"role": "assistant", "content": '{"intent": "booking_status", "confidence": 0.92, "language": "en"}'},
    # faq_question
    {"role": "user", "content": "Hvad koster en køretime?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad koster en køreprøve?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad koster det at tage kørekort?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.92, "language": "da"}'},
    {"role": "user", "content": "Hvor lang tid tager det at få kørekort?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.90, "language": "da"}'},
    {"role": "user", "content": "Kan man betale i afdrag?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.88, "language": "da"}'},
    {"role": "user", "content": "Kan jeg skifte fra manuelt til automatisk gear?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.87, "language": "da"}'},
    {"role": "user", "content": "How much does a driving license cost?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.90, "language": "en"}'},
    # faq_question — regulatory journey (lægeerklæring, borgerservice, kørekortansøgning)
    {"role": "user", "content": "Hvordan får jeg en lægeerklæring?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad gør jeg hos borgerservice?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.92, "language": "da"}'},
    {"role": "user", "content": "Hvordan søger jeg om kørekort?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad koster lægeerklæringen til kørekort?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvordan får jeg godkendt min kørekortsansøgning hos borgerservice?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hvad skal jeg medbringe til Borgerservice?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.92, "language": "da"}'},
    # greeting — bare standalone hilsner
    {"role": "user", "content": "Hej"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Hej!"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.95, "language": "da"}'},
    {"role": "user", "content": "Halløj"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.93, "language": "da"}'},
    {"role": "user", "content": "Goddag"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.92, "language": "da"}'},
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.93, "language": "en"}'},
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": '{"intent": "greeting", "confidence": 0.94, "language": "en"}'},
    # greeting combined with another intent → vælg det andet intent, ikke greeting
    {"role": "user", "content": "Hej, hvad koster en køretime?"},
    {"role": "assistant", "content": '{"intent": "faq_question", "confidence": 0.92, "language": "da"}'},
    {"role": "user", "content": "Hej, jeg vil gerne booke en køretime"},
    {"role": "assistant", "content": '{"intent": "book_lesson", "confidence": 0.93, "language": "da"}'},
    # unknown
    {"role": "user", "content": "hahaha 😂"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.3, "language": "da"}'},
    # emotional — nervousness/anxiety (intent=unknown, emotional=true → warm ack + escalate)
    {"role": "user", "content": "Jeg er nervøs for at køre"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.4, "language": "da", "emotional": true}'},
    {"role": "user", "content": "Jeg er bange for at dumpe køreprøven"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.45, "language": "da", "emotional": true}'},
    {"role": "user", "content": "Jeg tror ikke jeg er god nok til det her"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.4, "language": "da", "emotional": true}'},
    {"role": "user", "content": "I'm really anxious about driving"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.4, "language": "en", "emotional": true}'},
    # positive / neutral affect → NOT emotional (don't trigger the reassurance path)
    {"role": "user", "content": "Jeg glæder mig til at køre"},
    {"role": "assistant", "content": '{"intent": "unknown", "confidence": 0.5, "language": "da", "emotional": false}'},
    {"role": "user", "content": "Jeg er klar og frisk til timen i morgen"},
    {"role": "assistant", "content": '{"intent": "confirm_appointment", "confidence": 0.8, "language": "da", "emotional": false}'},
]

CLASSIFY_TOOL = {
    "name": "classify_intent",
    "description": "Classify the intent of an incoming SMS message from a driving school student",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "confirm_appointment",
                    "location_question",
                    "reschedule_booking",
                    "cancel_booking",
                    "theory_question",
                    "new_lead_inquiry",
                    "book_lesson",
                    "booking_status",
                    "faq_question",
                    "greeting",
                    "unknown",
                ],
                "description": "The classified intent of the message",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score from 0.0 to 1.0",
            },
            "language": {
                "type": "string",
                "enum": ["da", "en"],
                "description": "Detected language of the message",
            },
            "emotional": {
                "type": "boolean",
                "description": (
                    "True if the message expresses the student's nervousness, anxiety, "
                    "fear, or emotional distress (e.g. 'jeg er nervøs for at køre'), "
                    "rather than a purely practical question. Default false."
                ),
            },
        },
        "required": ["intent", "confidence", "language"],
    },
}
