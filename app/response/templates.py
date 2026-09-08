from app.config import SCHOOL_ADDRESS, SCHOOL_CITY, THEORY_SCHEDULE, PRICING, SCHOOL_NAME

# Response templates per intent, keyed by (intent, language)
# Keep under 160 chars where possible

TEMPLATES: dict[tuple[str, str], str] = {
    # confirm_appointment
    ("confirm_appointment", "da"): "Tak for bekræftelsen! Vi ses 👋",
    ("confirm_appointment", "en"): "Thanks for confirming! See you soon 👋",
    # location_question
    ("location_question", "da"): f"Vi mødes på {SCHOOL_ADDRESS}, {SCHOOL_CITY}. Skriv hvis du har brug for mere info!",
    ("location_question", "en"): f"We meet at {SCHOOL_ADDRESS}, {SCHOOL_CITY}. Let me know if you need more info!",
    # reschedule_booking
    ("reschedule_booking", "da"): "Jeg sender din besked videre, så vender vi tilbage hurtigst muligt med en ny tid 📅",
    ("reschedule_booking", "en"): "I'll pass your message along and we'll get back to you with a new time shortly 📅",
    # cancel_booking
    ("cancel_booking", "da"): "Jeg sender din aflysning videre. Du hører fra os snarest.",
    ("cancel_booking", "en"): "I'll pass along your cancellation. You'll hear from us shortly.",
    # book_lesson
    ("book_lesson", "da"): "Tak for din henvendelse! Jeg sender din bookinganmodning videre, og vi vender tilbage hurtigst muligt.",
    ("book_lesson", "en"): "Thanks for reaching out! I'll forward your booking request and we'll get back to you shortly.",
    # theory_question
    ("theory_question", "da"): f"Teoriundervisning: {THEORY_SCHEDULE}. Skriv hvis du har andre spørgsmål!",
    ("theory_question", "en"): f"Theory lessons: {THEORY_SCHEDULE}. Let me know if you have other questions!",
    # new_lead_inquiry — triggers lead capture flow, this is just the opening
    ("new_lead_inquiry", "da"): f"Hej! Fedt at du er interesseret 😊 {PRICING}. Hvad er dit fulde navn?",
    ("new_lead_inquiry", "en"): f"Hi! Great that you're interested 😊 {PRICING}. What's your full name?",
    # greeting — short intro when student opens with a bare "Hej" / "Hello"
    ("greeting", "da"): f"Hej! Jeg er Lisa, AI-sekretæren for {SCHOOL_NAME}. Hvad kan jeg hjælpe med?",
    ("greeting", "en"): f"Hi! I'm Lisa, the AI secretary at {SCHOOL_NAME}. How can I help?",
    # unknown — first attempt clarification
    ("unknown", "da"): "Undskyld, jeg forstod ikke helt. Kan du prøve at omformulere?",
    ("unknown", "en"): "Sorry, I didn't quite understand. Could you try rephrasing?",
    # unknown — second attempt, escalation notice
    ("unknown_escalated", "da"): "Jeg sender din besked videre til kørelæreren, som vender tilbage hurtigst muligt.",
    ("unknown_escalated", "en"): "I'm forwarding your message to the instructor, who will get back to you shortly.",
}


def get_template(intent: str, language: str) -> str | None:
    """Get a response template for the given intent and language."""
    return TEMPLATES.get((intent, language))
