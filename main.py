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

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Carrega variáveis de ambiente
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
WUZAPI_INSTANCE_NAME = get_env_var("WUZAPI_INSTANCE_NAME")

if None in [CHATWOOT_URL, CHATWOOT_ACCOUNT_ID, CHATWOOT_INBOX_ID,
            CHATWOOT_API_TOKEN, WUZAPI_API_URL, WUZAPI_API_TOKEN]:
    logger.critical("❌ Variáveis obrigatórias faltando")
    sys.exit(1)

CHATWOOT_URL = CHATWOOT_URL.rstrip('/')
WUZAPI_API_URL = WUZAPI_API_URL.rstrip('/')

def get_chatwoot_headers():
    return {
        "api_access_token": CHATWOOT_API_TOKEN,
        "Content-Type": "application/json"
    }

app = FastAPI(title="Ponte Ricard-ZAP", version="1.1.0")

# ---------------- UTIL ---------------- #

def extract_phone_number(sender_raw: str) -> str:
    phone = sender_raw.split('@')[0]
    if ':' in phone:
        phone = phone.split(':')[0]
    return re.sub(r'\D', '', phone)

def extract_jid_and_lid(sender_raw: str):
    jid = sender_raw
    lid = sender_raw.split('@')[0]
    return jid, lid

def format_phone(phone: str):
    phone = re.sub(r'\D', '', phone)
    if not phone.startswith("55"):
        phone = "55" + phone
    return f"+{phone}"

# ---------------- CONTATO ---------------- #

def search_contact(phone: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {"q": phone}

    try:
        res = requests.get(url, headers=get_chatwoot_headers(), params=params, timeout=10)
        if res.status_code != 200:
            return None

        data = res.json()
        for c in data.get("payload", []):
            c_phone = re.sub(r'\D', '', c.get("phone_number", ""))
            if phone in c_phone or c_phone in phone:
                return c
    except Exception as e:
        logger.error(e)

    return None

def create_contact(name, phone, jid, lid):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"

    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "phone_number": format_phone(phone),
        "custom_attributes": {
            "whatsapp_chat_id": phone,
            "whatsapp_jid": jid,
            "whatsapp_lid": lid,
            "whatsapp_instance": WUZAPI_INSTANCE_NAME
        }
    }

    try:
        res = requests.post(url, headers=get_chatwoot_headers(), json=payload, timeout=10)
        if res.status_code in [200, 201]:
            return res.json().get("payload", {}).get("contact")
    except Exception as e:
        logger.error(e)

    return None

def find_or_create_contact(name, sender_raw):
    phone = extract_phone_number(sender_raw)
    jid, lid = extract_jid_and_lid(sender_raw)

    contact = search_contact(phone)
    if contact:
        return contact["id"]

    new_contact = create_contact(name, phone, jid, lid)
    return new_contact["id"] if new_contact else None

# ---------------- CONVERSA ---------------- #

def find_or_create_conversation(contact_id):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"

    try:
        res = requests.get(url, headers=get_chatwoot_headers(), timeout=10)
        if res.status_code == 200:
            convs = res.json().get("payload", [])
            if convs:
                return convs[0]["id"]

        # cria nova
        create_url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {
            "inbox_id": int(CHATWOOT_INBOX_ID),
            "contact_id": contact_id
        }

        res = requests.post(create_url, headers=get_chatwoot_headers(), json=payload, timeout=10)
        if res.status_code in [200, 201]:
            return res.json().get("id")

    except Exception as e:
        logger.error(e)

    return None

# ---------------- MENSAGENS ---------------- #

def send_message_chatwoot(conversation_id, content):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {
        "content": content,
        "message_type": "incoming"
    }

    try:
        res = requests.post(url, headers=get_chatwoot_headers(), json=payload, timeout=10)
        return res.status_code in [200, 201]
    except Exception as e:
        logger.error(e)
        return False

def send_message_wuzapi(phone, text):
    url = f"{WUZAPI_API_URL}/chat/send/text"
    payload = {
        "number": re.sub(r'\D', '', phone),
        "text": text
    }

    headers = {
        "Content-Type": "application/json",
        "token": WUZAPI_API_TOKEN
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=15)
    except Exception as e:
        logger.error(e)

# ---------------- WEBHOOKS ---------------- #

@app.post("/webhook/wuzapi")
async def wuzapi_webhook(request: Request):
    data = await request.json()

    raw = data.get("jsonData", data)
    if raw.get("type") != "Message":
        return {"status": "ignored"}

    event = raw.get("event", {})
    info = event.get("Info", event)

    sender = info.get("SenderAlt") or info.get("Sender")
    if not sender:
        return {"status": "ignored"}

    message = event.get("Message", {}).get("conversation") or event.get("Message", {}).get("body")
    if not message:
        return {"status": "ignored"}

    name = info.get("PushName") or "Cliente"

    contact_id = find_or_create_contact(name, sender)
    if not contact_id:
        return {"status": "error"}

    conv_id = find_or_create_conversation(contact_id)
    if not conv_id:
        return {"status": "error"}

    send_message_chatwoot(conv_id, message)

    return {"status": "success"}

@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    data = await request.json()

    if data.get("event") != "message_created":
        return {"status": "ignored"}

    if data.get("private"):
        return {"status": "ignored"}

    content = data.get("content")
    contact = data.get("conversation", {}).get("contact", {})
    phone = contact.get("phone_number")

    if not phone:
        return {"status": "error"}

    send_message_wuzapi(phone, content)

    return {"status": "success"}

# ---------------- HEALTH ---------------- #

@app.get("/")
async def root():
    return {"status": "online"}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ---------------- RUN ---------------- #

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
