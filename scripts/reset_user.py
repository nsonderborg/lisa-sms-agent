import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Importerer din database setup fra din app
from app.models import Conversation, Message

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///lisa.db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def reset_user(phone: str):
    db = SessionLocal()
    convo = db.query(Conversation).filter(Conversation.phone_number == phone).first()
    if convo:
        # Slet alle SMS'er og derefter selve samtalen
        db.query(Message).filter(Message.conversation_id == convo.id).delete()
        db.delete(convo)
        db.commit()
        print(f"✅ Slettet al historik og state for {phone}. Du er nu et blankt blad!")
    else:
        print(f"ℹ️ Fandt ingen igangværende samtale med nummer {phone}")
    db.close()

if __name__ == "__main__":
    # Tager nummeret fra terminalen, eller bruger dit test-nummer som default
    phone = sys.argv[1] if len(sys.argv) > 1 else "+4500000002"
    reset_user(phone)