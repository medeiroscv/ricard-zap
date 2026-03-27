import os
import sys
from fastapi import FastAPI, Request
import requests
import json
from dotenv import load_dotenv
import re
import logging
import uvicorn
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

def get_env_var(name: str, required: bool = True):
    value = os.getenv(name)
    if required and not value:
        logger.error(f"❌ Variável '{name}' não encontrada")
        return None
    return str(value).strip() if value else value

CHATWOOT_URL = get_env_var("CHATWOOT_URL")
CHATWOOT_ACCOUNT_ID = get_env_var("CHATWOOT_ACCOUNT_ID")
CHATWOOT_INBOX_ID = get_env_var("CHATWOOT_INBOX_ID")
CHATWOOT_API_TOKEN = get_env_var("CHATWOOT_API_TOKEN")
WUZAPI_API_URL = get_env_var("WUZAPI_API_URL")
WUZAPI_API_TOKEN = get_env_var("WUZAPI_API_TOKEN")

if None in [CHATWOOT_URL, CHATWOOT_ACCOUNT_ID, CHATWOOT_INBOX_ID,
            CHATWOOT_API_TOKEN, WUZAPI_API_URL, WUZAPI_API_TOKEN]:
    sys.exit(1)

app = FastAPI()

def get_headers():
    return {
        "api_access_token": CHATWOOT_API_TOKEN,
        "Content-Type": "application/json"
    }

# ---------------- NORMALIZAÇÃO ---------------- #

def normalize_identifier(sender_raw: str):
    if "@s.whatsapp.net" in sender_raw:
        number = sender_raw.split("@")[0].split(":")[0]
        number = re.sub(r"\D", "", number)
        return number, sender_raw

    return sender_raw, sender_raw

# ---------------- CONTATO ---------------- #

def search_contact(identifier):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {"q": identifier}

    try:
        res = requests.get(url, headers=get_headers(), params=params)
        if res.status_code == 200:
            for c in res.json().get("payload", []):
                if str(c.get("identifier")) == str(identifier):
                    return c
    except Exception as e:
        logger.error(e)

    return None

def create_contact(name, identifier, phone=None):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"

    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "identifier": identifier
    }

    if phone:
        payload["phone_number"] = f"+{phone}"

    res = requests.post(url, headers=get_headers(), json=payload)

    if res.status_code in [200, 201]:
        return res.json()["payload"]["contact"]

    logger.error(res.text)
    return None

def find_or_create_contact(name, sender_raw):
    identifier, jid = normalize_identifier(sender_raw)

    contact = search_contact(identifier)

    if contact:
        return contact["id"]

    new_contact = create_contact(name, identifier, identifier if identifier.isdigit() else None)

    return new_contact["id"] if new_contact else None

# ---------------- CONVERSA ---------------- #

def get_or_create_conversation(contact_id):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"

    res = requests.get(url, headers=get_headers())

    if res.status_code == 200:
        convs = res.json().get("payload", [])
        if convs:
            return convs[0]["id"]

    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"

    res = requests.post(url, headers=get_headers(), json={
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "contact_id": contact_id
    })

    if res.status_code in [200, 201]:
        return res.json()["id"]

    return None

# ---------------- ENVIO ---------------- #

def send_to_chatwoot(conv_id, msg):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conv_id}/messages"

    requests.post(url, headers=get_headers(), json={
        "content": msg,
        "message_type": "incoming"
    })

def send_to_whatsapp(identifier, msg):
    url = f"{WUZAPI_API_URL}/chat/send/text"

    requests.post(url, json={
        "number": identifier,
        "text": msg
    }, headers={"token": WUZAPI_API_TOKEN})

# ---------------- WEBHOOKS ---------------- #

@app.post("/webhook/wuzapi")
async def wuzapi(request: Request):
    data = await request.json()

    # 🔥 LOG PRA DEBUG
    logger.info(f"🔥 RAW DATA: {json.dumps(data)[:500]}")

    raw = data.get("jsonData", data)

    if raw.get("type") != "Message":
        return {"status": "ignored"}

    event = raw.get("event", {})
    info = event.get("Info", {})

    sender = info.get("SenderAlt") or info.get("Sender")

    if not sender:
        return {"status": "ignored"}

    # 🔥 FILTRO DE GRUPO CONFIÁVEL
    chat = info.get("Chat") or info.get("ChatJid") or ""
    if "@g.us" in str(chat):
        return {"status": "ignored"}

    # 🔥 PARSER UNIVERSAL (CORREÇÃO PRINCIPAL)
    message_data = event.get("Message", {})

    msg = None

    if "conversation" in message_data:
        msg = message_data["conversation"]

    elif "extendedTextMessage" in message_data:
        msg = message_data["extendedTextMessage"].get("text")

    elif "imageMessage" in message_data:
        msg = message_data["imageMessage"].get("caption", "[imagem]")

    elif "videoMessage" in message_data:
        msg = message_data["videoMessage"].get("caption", "[vídeo]")

    elif "audioMessage" in message_data:
        msg = "[áudio]"

    elif "documentMessage" in message_data:
        msg = "[documento]"

    # 🔥 fallback pra não perder mensagem
    if not msg:
        msg = json.dumps(message_data)[:200]

    name = info.get("PushName") or "Cliente"

    contact_id = find_or_create_contact(name, sender)
    conv_id = get_or_create_conversation(contact_id)

    send_to_chatwoot(conv_id, msg)

    return {"status": "ok"}

@app.post("/webhook/chatwoot")
async def chatwoot(request: Request):
    data = await request.json()

    if data.get("event") != "message_created":
        return {"status": "ignored"}

    if data.get("private"):
        return {"status": "ignored"}

    msg = data.get("content")

    contact = data.get("conversation", {}).get("contact", {})
    identifier = contact.get("identifier")

    if not identifier:
        return {"status": "error"}

    send_to_whatsapp(identifier, msg)

    return {"status": "ok"}

# ---------------- RUN ---------------- #

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
