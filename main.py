import os
import sys
from fastapi import FastAPI, Request, HTTPException
import requests
import json
from dotenv import load_dotenv
import re
from datetime import datetime
from typing import Optional
import logging
import uvicorn

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Carrega as variáveis de ambiente
load_dotenv()

# --- Validação das Variáveis de Ambiente ---
logger.info("Verificando Variáveis de Ambiente")

def get_env_var(name: str, required: bool = True):
    """Obtém e valida variável de ambiente"""
    value = os.getenv(name)
    
    if required and not value:
        logger.error(f"❌ Variável '{name}' não encontrada")
        return None
    
    if value:
        return str(value).strip()
    
    return value

# Carregar variáveis
CHATWOOT_URL = get_env_var("CHATWOOT_URL")
CHATWOOT_ACCOUNT_ID = get_env_var("CHATWOOT_ACCOUNT_ID")
CHATWOOT_INBOX_ID = get_env_var("CHATWOOT_INBOX_ID")
CHATWOOT_API_TOKEN = get_env_var("CHATWOOT_API_TOKEN")
WUZAPI_API_URL = get_env_var("WUZAPI_API_URL")
WUZAPI_API_TOKEN = get_env_var("WUZAPI_API_TOKEN")
WUZAPI_INSTANCE_NAME = get_env_var("WUZAPI_INSTANCE_NAME")

# Verificar se todas as variáveis obrigatórias foram carregadas
required_vars = [CHATWOOT_URL, CHATWOOT_ACCOUNT_ID, CHATWOOT_INBOX_ID, 
                 CHATWOOT_API_TOKEN, WUZAPI_API_URL, WUZAPI_API_TOKEN]

if None in required_vars:
    logger.critical("❌ Variáveis obrigatórias faltando. Verifique a configuração")
    sys.exit(1)

# Limpar URLs
CHATWOOT_URL = CHATWOOT_URL.rstrip('/')
WUZAPI_API_URL = WUZAPI_API_URL.rstrip('/')

logger.info(f"✅ Configuração carregada:")
logger.info(f"  Chatwoot URL: {CHATWOOT_URL}")
logger.info(f"  Account ID: {CHATWOOT_ACCOUNT_ID}")
logger.info(f"  Inbox ID: {CHATWOOT_INBOX_ID}")
logger.info(f"  WuzAPI URL: {WUZAPI_API_URL}")

def get_chatwoot_headers() -> dict:
    """Gera os cabeçalhos para requisições ao Chatwoot."""
    return {
        'api_access_token': CHATWOOT_API_TOKEN,
        'Content-Type': 'application/json'
    }

# Cria a aplicação FastAPI
app = FastAPI(title="Ponte Ricard-ZAP", version="1.0.0")

# --- FUNÇÕES DE INTERAÇÃO COM O CHATWOOT ---

def search_contact(phone_number: str):
    """Busca um contato no Chatwoot pelo número de telefone."""
    search_phone = phone_number.replace('+', '')
    search_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {'q': search_phone}
    
    try:
        logger.info(f"🔍 Buscando contato: {search_phone}")
        response = requests.get(search_endpoint, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Erro na busca: {response.status_code}")
            return None
            
        data = response.json()
        
        if data.get("meta", {}).get("count", 0) > 0:
            for contact in data.get("payload", []):
                contact_phone = contact.get("phone_number", "")
                if contact_phone.endswith(search_phone) or search_phone.endswith(contact_phone.replace('+', '')):
                    logger.info(f"✅ Contato encontrado: ID {contact['id']}")
                    return contact
        
        logger.info(f"❌ Contato não encontrado")
        return None
    except Exception as e:
        logger.error(f"❌ Erro ao buscar contato: {e}")
        return None

def create_contact(name: str, phone_number: str):
    """Cria um novo contato no Chatwoot."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
    
    if not phone_number.startswith('+'):
        phone_number = f"+{phone_number}"
        
    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "phone_number": phone_number,
    }
    
    try:
        logger.info(f"📝 Criando contato: {name} ({phone_number})")
        response = requests.post(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Erro ao criar contato: {response.status_code} - {response.text}")
            return None
            
        contact = response.json()["payload"]["contact"]
        logger.info(f"✅ Contato criado: ID {contact['id']}")
        return contact
    except Exception as e:
        logger.error(f"❌ Erro ao criar contato: {e}")
        return None

def search_or_create_contact(name: str, phone_number: str) -> Optional[int]:
    """Busca um contato e, se não encontrar, cria um novo."""
    contact = search_contact(phone_number)
    
    if contact:
        return contact['id']
    
    new_contact = create_contact(name, phone_number)
    if new_contact:
        return new_contact['id']
    
    return None

def find_or_create_conversation(contact_id: int) -> Optional[int]:
    """Busca uma conversa existente ou cria uma nova."""
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"
    
    try:
        logger.info(f"💬 Buscando conversas para contato {contact_id}")
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        
        if response.status_code == 200:
            conversations = response.json().get("payload", [])
            if conversations:
                conv_id = conversations[0]['id']
                logger.info(f"✅ Conversa encontrada: ID {conv_id}")
                return conv_id
        
        logger.info(f"🆕 Criando nova conversa")
        create_conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {"inbox_id": int(CHATWOOT_INBOX_ID), "contact_id": contact_id}
        
        create_response = requests.post(create_conv_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if create_response.status_code == 200:
            new_conv_id = create_response.json()['id']
            logger.info(f"✅ Conversa criada: ID {new_conv_id}")
            return new_conv_id
        else:
            logger.error(f"Erro ao criar conversa: {create_response.status_code}")
            return None
        
    except Exception as e:
        logger.error(f"❌ Erro na conversa: {e}")
        return None

def send_message_to_conversation(conversation_id: int, message_content: str):
    """Envia uma mensagem para uma conversa específica no Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        logger.info(f"📤 Enviando mensagem para conversa {conversation_id}")
        logger.info(f"   Conteúdo: {message_content[:100]}")
        
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot: Status {response.status_code}")
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada com sucesso!")
            return response.json()
        else:
            logger.error(f"❌ Falha ao enviar: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem: {e}")
        return None

def send_message_via_wuzapi(phone_number: str, message: str):
    """Envia uma mensagem via WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return

    send_url = f"{WUZAPI_API_URL}/chat/send/text"
    payload = {"number": phone_number, "text": message}
    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}

    try:
        logger.info(f"📤 Enviando mensagem via WuzAPI para {phone_number}")
        response = requests.post(send_url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada com sucesso!")
        else:
            logger.error(f"❌ Erro: {response.status_code} - {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")

# --- ENDPOINTS ---

@app.post("/webhook/wuzapi")
async def handle_wuzapi_webhook(request: Request):
    """Recebe webhooks do WuzAPI."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do WuzAPI")
        
        # Log dos dados recebidos para debug
        logger.debug(f"Dados: {json.dumps(data, indent=2)}")
        
        # Extrair dados - formato do WuzAPI
        raw_data = data.get("jsonData", data)
        event_type = raw_data.get("type")
        
        logger.info(f"Event type: {event_type}")
        
        if event_type != "Message":
            logger.info(f"Ignorando evento: {event_type}")
            return {"status": "ignored", "reason": f"event is {event_type}"}
        
        event_data = raw_data.get("event", {})
        info = event_data.get("Info", event_data)
        
        # Pegar remetente
        sender_raw = info.get('SenderAlt') or info.get('Sender')
        if not sender_raw:
            logger.warning("Remetente não encontrado")
            return {"status": "ignored", "reason": "no sender"}
        
        logger.info(f"Sender raw: {sender_raw}")
        
        # CORREÇÃO: Verificar se é grupo de forma mais precisa
        chat_jid = info.get('Chat') or info.get('ChatJid') or sender_raw
        is_group = False
        
        # Verifica se é grupo apenas se tiver @g.us
        if "@g.us" in str(chat_jid):
            is_group = True
            logger.info(f"Detectado grupo por @g.us: {chat_jid}")
        
        # Se for grupo, ignorar
        if is_group:
            logger.info(f"Ignorando mensagem de grupo")
            return {"status": "ignored", "reason": "group chat"}
        
        # Extrair mensagem
        message_data = event_data.get("Message", event_data)
        message_content = message_data.get("conversation") or message_data.get("body")
        
        # Se não encontrou, tenta em extendedTextMessage
        if not message_content and message_data.get("extendedTextMessage"):
            message_content = message_data.get("extendedTextMessage", {}).get("text")
        
        if not message_content:
            logger.warning("Mensagem sem conteúdo")
            return {"status": "ignored", "reason": "empty content"}
        
        # Dados do contato
        sender_phone = sender_raw.split('@')[0]
        sender_name = info.get("PushName") or info.get("pushName") or sender_phone
        
        logger.info(f"Processando: {sender_name} ({sender_phone})")
        logger.info(f"Mensagem: {message_content}")
        
        # Criar contato e conversa
        contact_id = search_or_create_contact(sender_name, sender_phone)
        if not contact_id:
            logger.error("Falha ao criar contato")
            return {"status": "error", "reason": "contact failed"}
        
        logger.info(f"Contact ID: {contact_id}")
        
        conversation_id = find_or_create_conversation(contact_id)
        if not conversation_id:
            logger.error("Falha ao criar conversa")
            return {"status": "error", "reason": "conversation failed"}
        
        logger.info(f"Conversation ID: {conversation_id}")
        
        # Enviar mensagem
        result = send_message_to_conversation(conversation_id, message_content)
        
        if result:
            logger.info(f"✅ Sucesso! Mensagem enviada para conversa {conversation_id}")
            return {"status": "success", "conversation_id": conversation_id}
        else:
            logger.error("❌ Falha ao enviar mensagem")
            return {"status": "error", "reason": "send failed"}
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

@app.post("/webhook/chatwoot")
async def handle_chatwoot_webhook(request: Request):
    """Recebe webhooks do Chatwoot."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do Chatwoot")
        
        # Validar evento
        if data.get("event") != "message_created":
            return {"status": "ignored", "reason": "not message_created"}
        
        # Validar tipo
        if data.get("message_type") != "outgoing":
            return {"status": "ignored", "reason": "not outgoing"}
        
        # Validar remetente
        sender_type = data.get("sender", {}).get("type")
        if sender_type not in ["agent_bot", "user"]:
            return {"status": "ignored", "reason": "not agent"}
        
        content = data.get("content")
        if not content:
            return {"status": "ignored", "reason": "empty content"}
        
        # Buscar número do contato
        conversation = data.get("conversation", {})
        contact_phone = (
            conversation.get("meta", {}).get("sender", {}).get("phone_number") or
            conversation.get("contact", {}).get("phone_number")
        )
        
        if not contact_phone:
            logger.error("Telefone não encontrado")
            return {"status": "error", "reason": "phone not found"}
        
        # Limpar número
        destination = re.sub(r"\D", "", contact_phone)
        
        # Enviar mensagem
        send_message_via_wuzapi(destination, content)
        
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"❌ Erro: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Ponte Ricard-ZAP",
        "version": "1.0.0"
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/debug/env")
async def debug_env():
    return {
        "chatwoot_url": CHATWOOT_URL,
        "chatwoot_account_id": CHATWOOT_ACCOUNT_ID,
        "chatwoot_inbox_id": CHATWOOT_INBOX_ID,
        "wuzapi_url": WUZAPI_API_URL,
        "chatwoot_token_configured": bool(CHATWOOT_API_TOKEN),
        "wuzapi_token_configured": bool(WUZAPI_API_TOKEN)
    }

@app.get("/debug/last_message")
async def debug_last_message():
    """Endpoint para debug - mostra a última mensagem recebida"""
    return {"message": "Use o endpoint /webhook/debug para ver mensagens em tempo real"}

@app.post("/webhook/debug")
async def debug_webhook(request: Request):
    """Endpoint de debug que mostra tudo que chega"""
    try:
        body = await request.body()
        headers = dict(request.headers)
        
        print("\n" + "="*60)
        print("🔍 DEBUG - Webhook Recebido")
        print(f"Headers: {json.dumps(headers, indent=2)}")
        print(f"Body: {body.decode('utf-8')}")
        print("="*60 + "\n")
        
        # Tenta parsear como JSON
        try:
            json_data = await request.json()
            print(f"JSON parseado: {json.dumps(json_data, indent=2)}")
            
            # Extrair informações importantes
            raw_data = json_data.get("jsonData", json_data)
            event_data = raw_data.get("event", {})
            info = event_data.get("Info", event_data)
            
            sender = info.get('SenderAlt') or info.get('Sender')
            chat = info.get('Chat') or info.get('ChatJid')
            message = event_data.get("Message", {}).get("conversation") or event_data.get("Message", {}).get("body")
            
            print(f"\n📱 Remetente: {sender}")
            print(f"💬 Chat: {chat}")
            print(f"📝 Mensagem: {message}")
            print(f"👥 É grupo? {'SIM' if '@g.us' in str(chat) else 'NÃO'}")
            
        except Exception as e:
            print(f"Erro ao parsear JSON: {e}")
        
        return {"status": "received", "message": "Webhook recebido para debug"}
    except Exception as e:
        print(f"Erro no debug: {e}")
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
