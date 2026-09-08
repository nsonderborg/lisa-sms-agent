import logging

import anthropic

from app.config import (
    ANTHROPIC_API_KEY,
    CLASSIFICATION_MODEL,
    FAQ,
    MAX_SMS_LENGTH,
    PRICING,
    SCHOOL_ADDRESS,
    SCHOOL_NAME,
    TARGET_SMS_LENGTH,
    THEORY_SCHEDULE,
)
from app.response.templates import get_template

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

LLM_RESPONSE_SYSTEM_PROMPT = """\
Du er Lisa, en venlig AI-sekretær for en dansk køreskole. Skriv et kort SMS-svar.

Regler:
- Maks {max_chars} tegn
- Skriv kun ren tekst. Brug aldrig emoji. Brug aldrig markdown-symboler som *, **, _, # eller punktopstillinger — SMS viser disse som rå tegn.
- Vær varm og venlig, men kort
- Skriv på {language}
- Svar kun på det der bliver spurgt om
- Skolens fagord staves "manøvrebane" og "kørelektioner" — skriv aldrig "manøver".
- Du har IKKE adgang til elevens personlige oplysninger (deres bookinger, næste køretime, lektionsplan-position eller betalingsstatus). Opfind ALDRIG sådanne svar og find ALDRIG på systemer som "loginkort", "login", "min side" eller "elevportal" — de findes ikke. Sig kort at du ikke kan se det; for at booke skriver de "Jeg vil gerne booke en køretime", og deres lektionsplan-position tager de med kørelæreren.
"""


def generate_response(
    intent: str,
    language: str,
    original_message: str,
    conversation_context: str = "",
) -> str:
    """Generate a response for the given intent. Uses template if available, falls back to LLM."""
    # Try template first
    template = get_template(intent, language)
    if template:
        logger.info("Using template for intent=%s language=%s", intent, language)
        return template

    # Fall back to LLM generation
    logger.info("No template for intent=%s, generating via LLM", intent)
    return _generate_llm_response(intent, language, original_message, conversation_context)


def _generate_llm_response(
    intent: str,
    language: str,
    original_message: str,
    conversation_context: str = "",
) -> str:
    """Generate a response using Claude when no template is available."""
    lang_name = "dansk" if language == "da" else "English"
    system = LLM_RESPONSE_SYSTEM_PROMPT.format(
        max_chars=TARGET_SMS_LENGTH,
        language=lang_name,
    )

    user_content = f"Besked fra elev: {original_message}"
    if conversation_context:
        user_content = f"Samtalehistorik:\n{conversation_context}\n\n{user_content}"
    user_content += f"\n\nIntent: {intent}\nSvar kort og venligt (maks {TARGET_SMS_LENGTH} tegn):"

    try:
        response = client.messages.create(
            model=CLASSIFICATION_MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()

        # Enforce max length
        if len(text) > MAX_SMS_LENGTH:
            text = text[: MAX_SMS_LENGTH - 1] + "…"

        return text

    except Exception:
        logger.exception("LLM response generation failed")
        # Fallback response
        if language == "en":
            return "Thanks for your message! We'll get back to you shortly."
        return "Tak for din besked! Vi vender tilbage snarest."


FAQ_RESPONSE_SYSTEM_PROMPT = """\
Du er Lisa, en venlig AI-sekretær for {school_name}. Svar kortfattet på elevens spørgsmål.

Skolens oplysninger:
- Navn: {school_name}
- Adresse: {school_address}
- Priser: {pricing}
- Teori: {theory_schedule}

FAQ-svar fra ejeren:
{faq_context}

Regler:
- Maks {max_chars} tegn
- Skriv kun ren tekst. Brug aldrig emoji. Brug aldrig markdown-symboler som *, **, _, # eller punktopstillinger — SMS viser disse som rå tegn.
- Skriv på {language}
- Svar kun på det der bliver spurgt om
- Vær varm og venlig, men kort
- Hvis spørgsmålet handler om at booke, reservere, aftale eller få en tid til køretime/manøvrebane/teoriprøve/køreprøve — ELLER hvis det er et "hvordan booker jeg"-spørgsmål — så skal du ALDRIG nævne telefonnummer eller e-mailadresse i svaret. Du ER booking-kanalen. Fortæl eleven at de kan booke direkte her ved at skrive fx "Jeg vil gerne booke en køretime".
- Du har IKKE adgang til elevens personlige oplysninger (deres egne bookinger, deres næste køretime, deres lektionsplan-position eller betalingsstatus). Hvis eleven spørger om sådan noget, så opfind ALDRIG et svar og find ALDRIG på systemer som "loginkort", "login", "min side" eller "elevportal" — de findes ikke. Sig kort at du ikke kan se deres personlige oplysninger; for at booke en ny tid kan de skrive "Jeg vil gerne booke en køretime", og spørgsmål om deres lektionsplan-position tager de med deres kørelærer.
- Brug skolens præcise fagord: skriv "manøvrebane" og "kørelektioner" (en kørelektion = 45 min). Skriv ALDRIG "manøver" (forkert stavning) eller andre forkortede/opfundne former. Forkort, afrund eller opfind aldrig priser, varigheder, enheder eller pakkeindhold — gengiv dem præcis som de står under "Priser" og "FAQ-svar" ovenfor.
{pricing_instruction}"""


def generate_faq_response(
    language: str,
    original_message: str,
    conversation_context: str = "",
) -> str:
    """Generate an FAQ answer using LLM with owner-written FAQ context (per D-14)."""
    lang_name = "dansk" if language == "da" else "English"

    # Build FAQ context string from config
    faq_lines = []
    for key, value in FAQ.items():
        if value:  # Skip empty entries (per Pitfall 4)
            faq_lines.append(f"- {key}: {value}")
    faq_context = "\n".join(faq_lines) if faq_lines else "(Ingen FAQ-svar konfigureret)"

    # Detect if pricing-related for soft prompt (per D-11)
    pricing_keywords = ["koster", "pris", "price", "cost", "betale", "betaling", "afdrag", "payment"]
    is_pricing = any(kw in original_message.lower() for kw in pricing_keywords)
    pricing_instruction = (
        '- Afslut dit svar med: "Vil du hore mere om at komme i gang?"'
        if is_pricing
        else ""
    )

    system = FAQ_RESPONSE_SYSTEM_PROMPT.format(
        school_name=SCHOOL_NAME or "køreskolen",
        school_address=SCHOOL_ADDRESS or "",
        pricing=PRICING or "",
        theory_schedule=THEORY_SCHEDULE or "",
        faq_context=faq_context,
        max_chars=TARGET_SMS_LENGTH,
        language=lang_name,
        pricing_instruction=pricing_instruction,
    )

    user_content = f"Spørgsmål fra elev: {original_message}"
    if conversation_context:
        user_content = f"Samtalehistorik:\n{conversation_context}\n\n{user_content}"

    try:
        response = client.messages.create(
            model=CLASSIFICATION_MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()

        # Enforce max length
        if len(text) > MAX_SMS_LENGTH:
            text = text[:MAX_SMS_LENGTH - 3] + "..."

        return text

    except Exception:
        logger.exception("FAQ response generation failed")
        if language == "en":
            return "Thanks for your question! We'll get back to you shortly."
        return "Tak for dit spørgsmål! Vi vender tilbage snarest."
