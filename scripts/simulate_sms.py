"""Post a single simulated inbound SMS to the local webhook.

Requires ``LISA_SKIP_TWILIO_SIGNATURE=1`` in the environment running uvicorn —
the payload below carries no Twilio signature, so without it /sms answers 403:

    LISA_SKIP_TWILIO_SIGNATURE=1 uvicorn app.main:app --reload --port 8000

(The webhook used to wave through anything claiming ``Host: localhost``, but
that header is client-supplied, so it authenticated nothing.)
"""
import sys
import requests

def send_simulated_sms(message: str):
    print(f"🚀 Sender simuleret SMS: '{message}'")
    
    # Det payload Twilio normalt ville sende til din webhook
    payload = {
        "From": "+4500000002",  # Dit faste test-nummer
        "Body": message
    }
    
    try:
        response = requests.post("http://localhost:8000/sms", data=payload)
        print(f"📥 HTTP Status: {response.status_code}")
        print(f"💬 Serverens Svar (TwiML / Text):\n{response.text}")
    except requests.exceptions.ConnectionError:
        print("❌ Fejl: Kunne ikke få fat i serveren. Kører din uvicorn-server?")

if __name__ == "__main__":
    # Tag beskeden fra terminalen, eller brug en default
    msg = sys.argv[1] if len(sys.argv) > 1 else "Hej, har I ledige tider?"
    send_simulated_sms(msg)