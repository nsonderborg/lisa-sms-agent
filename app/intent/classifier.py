import logging
from dataclasses import dataclass

import anthropic

from app.config import ANTHROPIC_API_KEY, CLASSIFICATION_MODEL, CONFIDENCE_THRESHOLD
from app.intent.prompts import (
    CLASSIFY_TOOL,
    INTENT_CLASSIFICATION_EXAMPLES,
    INTENT_CLASSIFICATION_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


@dataclass
class ClassificationResult:
    intent: str
    confidence: float
    language: str
    # Phase 999.35: True when the message carries the student's nervousness /
    # anxiety / fear / distress (e.g. "jeg er nervøs for at køre"), independent
    # of the practical intent. Preserved across the low-confidence -> unknown
    # downgrade so an emotional opener still gets a warm acknowledgment.
    emotional: bool = False


def classify_message(message: str, conversation_context: str = "") -> ClassificationResult:
    """Classify an SMS message into one of the configured intents (see CLASSIFY_TOOL) using Claude API with tool_use."""
    user_content = message
    if conversation_context:
        user_content = f"[Samtalehistorik]\n{conversation_context}\n\n[Ny besked]\n{message}"

    messages = [
        *INTENT_CLASSIFICATION_EXAMPLES,
        {"role": "user", "content": user_content},
    ]

    try:
        response = client.messages.create(
            model=CLASSIFICATION_MODEL,
            max_tokens=256,
            system=INTENT_CLASSIFICATION_SYSTEM_PROMPT,
            messages=messages,
            tools=[CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_intent"},
        )

        # Extract tool use result
        for block in response.content:
            if block.type == "tool_use" and block.name == "classify_intent":
                result = ClassificationResult(
                    intent=block.input["intent"],
                    confidence=block.input["confidence"],
                    language=block.input["language"],
                    emotional=bool(block.input.get("emotional", False)),
                )

                # Apply confidence threshold
                if result.confidence < CONFIDENCE_THRESHOLD:
                    logger.info(
                        "Low confidence %.2f for intent '%s', treating as unknown",
                        result.confidence,
                        result.intent,
                    )
                    result.intent = "unknown"

                logger.info(
                    "Classified: intent=%s confidence=%.2f language=%s",
                    result.intent,
                    result.confidence,
                    result.language,
                )
                return result

        # Fallback if no tool use in response
        logger.warning("No tool_use block in classification response")
        return ClassificationResult(intent="unknown", confidence=0.0, language="da")

    except Exception:
        logger.exception("Classification API call failed")
        return ClassificationResult(intent="unknown", confidence=0.0, language="da")
