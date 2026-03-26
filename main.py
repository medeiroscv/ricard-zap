import os
import sys
from fastapi import FastAPI, Request, HTTPException
import requests
import json
from dotenv import load_dotenv
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging
from logging.handlers import RotatingFileHandler

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Carrega as variáveis de ambiente
load_dotenv()

# --- Validação e Conversão de Tipos ---
logger.info("Verificando Variáveis de Ambiente")

def get_env_var(name: str, required: bool = True, var_type: str = "str"):
    """Obtém e valida variável de ambiente"""
    value = os.getenv(name)
    
    if required and not value:
        logger.error(f"❌ Variável '{name}' não encontrada")
        return None
    
    if value:
        if var_type == "int":
            try:
                return int(value)
            except ValueError:
                logger.error(f"❌ Variável '{name}' deve ser um número inteiro")
                return None
        elif var_type == "str":
            return str(value).strip()
    
    return value

# Carregar variáveis
CHATWOOT_URL = get_env_var("CHATWOOT_URL")
CHATWOOT_ACCOUNT_ID = get_env_var("CHATWOOT_ACCOUNT_ID", var_type="int")
CHATWOOT_INBOX_ID = get_env_var("CHATWOOT_INBOX_ID", var_type="int")
CHATWOOT_API_TOKEN = get_env_var("CHATWOOT_API_TOKEN")
WUZAPI_API_URL = get_env_var("WUZAPI_API_URL")
WUZAPI_API_TOKEN = get_env_var("WUZAPI_API_TOKEN")
WUZAPI_INSTANCE_NAME = get_env_var("WUZAPI_INSTANCE_NAME")

# Verificar se todas as variáveis obrigatórias foram carregadas
required_vars = [CHATWOOT_URL, CHATWOOT_ACCOUNT_ID, CHATWOOT_INBOX_ID, 
                 CHATWOOT_API_TOKEN, WUZAPI_API_URL, WUZAPI_API_TOKEN]

if None in required_vars:
    logger.critical("❌ Variáveis obrigatórias faltando. Verifique a configuração no EasyPanel")
    sys.exit(1)

# Limpar URLs (remover barras no final)
CHATWOOT_URL = CHATWOOT_URL.rstrip('/')
WUZAPI_API_URL = WUZAPI_API_URL.rstrip('/')

logger.info(f"✅ Configuração carregada:")
logger.info(f"  Chatwoot URL: {CHATWOOT_URL}")
logger.info(f"  Account ID: {CHATWOOT_ACCOUNT_ID}")
logger.info(f"  Inbox ID: {CHATWOOT_INBOX_ID}")
logger.info(f"  WuzAPI URL: {WUZAPI_API_URL}")
logger.info(f"  Instance: {WUZAPI_INSTANCE_NAME}")

# Cache para deduplicação
message_cache: Dict[str, datetime] = {}
CACHE_TTL = 3600

def is_duplicate_message(message_id: str) -> bool:
    """Verifica se a mensagem já foi processada."""
    now = datetime.now()
    for mid, timestamp in list(message_cache.items()):
        if now - timestamp > timedelta(seconds=CACHE_TTL):
            del message_cache[mid]
    
    if message_id in message_cache:
        logger.warning(f"Mensagem duplicada: {message_id}")
        return True
    
    message_cache[message_id] = now
    return False

def get_chatwoot_headers() -> dict:
    """Gera os cabeçalhos para requisições ao Chatwoot."""
    return {
        'api_access_token': CHATWOOT_API_TOKEN,
        'Content-Type': 'application/json'
    }

app = FastAPI(title="Ponte Ricard-ZAP", version="1.0.0")

# Funções existentes (search_contact, create_contact, etc) permanecem iguais
# Mas com os tipos corrigidos

def search_contact(phone_number: str):
    """Busca um contato no Chatwoot pelo número de telefone."""
    search_phone = phone_number.replace('+', '')
    search_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {'q': search_phone}
    
    try:
        logger.info(f"Buscando contato: {search_phone}")
        response = requests.get(search_endpoint, headers=get_chatwoot_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("meta", {}).get("count", 0) > 0:
            for contact in data.get("payload", []):
                if contact.get("phone_number", "").endswith(search_phone):
                    logger.info(f"Contato encontrado: ID {contact['id']}")
                    return contact
        
        logger.info(f"Contato não encontrado: {phone_number}")
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar contato: {e}")
        return None

def create_contact(name: str, phone_number: str, avatar_url: Optional[str] = None):
    """Cria um novo contato no Chatwoot."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
    
    if '@' not in phone_number and not phone_number.startswith('+'):
        phone_number = f"+{phone_number}"
        
    payload = {
        "inbox_id": CHATWOOT_INBOX_ID,
        "name": name,
        "phone_number": phone_number,
    }
    if avatar_url:
        payload["avatar_url"] = avatar_url
    
    try:
        logger.info(f"Criando contato: {name} ({phone_number})")
        response = requests.post(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        response.raise_for_status()
        contact = response.json()["payload"]["contact"]
        logger.info(f"Contato criado: ID {contact['id']}")
        return contact
    except Exception as e:
        logger.error(f"Erro ao criar contato: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta: {e.response.text}")
        return None

def search_or_create_contact(name: str, phone_number: str, avatar_url: Optional[str] = None) -> Optional[int]:
    """Busca ou cria um contato."""
    contact = search_contact(phone_number)
    
    if contact:
        contact_id = contact['id']
        if avatar_url and contact.get("avatar_url") != avatar_url:
            logger.info(f"Atualizando avatar: {contact_id}")
            update_contact_avatar(contact_id, avatar_url)
        return contact_id
    
    new_contact = create_contact(name, phone_number, avatar_url)
    return new_contact['id'] if new_contact else None

def find_or_create_conversation(contact_id: int) -> Optional[int]:
    """Busca ou cria uma conversa."""
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"
    
    try:
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        response.raise_for_status()
        conversations = response.json().get("payload", [])
        
        if conversations:
            conv_id = conversations[0]['id']
            logger.info(f"Conversa encontrada: {conv_id}")
            return conv_id
        
        logger.info(f"Criando conversa para contato {contact_id}")
        create_conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {"inbox_id": CHATWOOT_INBOX_ID, "contact_id": contact_id}
        create_response = requests.post(create_conv_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        create_response.raise_for_status()
        conv_id = create_response.json()['id']
        logger.info(f"Conversa criada: {conv_id}")
        return conv_id
        
    except Exception as e:
        logger.error(f"Erro na conversa: {e}")
        return None

def send_message_to_conversation(conversation_id: int, message_content: str):
    """Envia mensagem para o Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        logger.info(f"Enviando mensagem para conversa {conversation_id}")
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info("✅ Mensagem enviada com sucesso!")
        return response.json()
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta: {e.response.text}")
        return None

def get_conversation_phone_number(conversation_id: int) -> Optional[str]:
    """Obtém número de telefone da conversa."""
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}"
    
    try:
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()
        
        phone = (data.get("meta", {}).get("sender", {}).get("phone_number") or
                data.get("meta", {}).get("contact", {}).get("phone_number") or
                data.get("contact", {}).get("phone_number"))
        
        if phone:
            logger.info(f"Telefone encontrado: {phone}")
        return phone
    except Exception as e:
        logger.error(f"Erro ao buscar telefone: {e}")
        return None

def update_contact_avatar(contact_id: int, avatar_url: str):
    """Atualiza avatar do contato."""
    if not avatar_url:
        return
    
    try:
        avatar_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}"
        payload = {"avatar_url": avatar_url}
        response = requests.put(avatar_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Avatar atualizado: {contact_id}")
    except Exception as e:
        logger.error(f"Erro ao atualizar avatar: {e}")

def get_wuzapi_profile_pic(phone_number_raw: str) -> Optional[str]:
    """Busca foto de perfil na WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        return None
    
    try:
        base_number = phone_number_raw.split("@")[0].replace("+", "")
        headers = {"token": WUZAPI_API_TOKEN}
        
        # Tenta buscar avatar
        avatar_url = f"{WUZAPI_API_URL}/user/avatar"
        response = requests.post(avatar_url, headers=headers, json={"phone": base_number}, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return data.get("results", {}).get("url") or data.get("profileImage")
        
        return None
    except Exception as e:
        logger.debug(f"Erro ao buscar avatar: {e}")
        return None

def send_message_via_wuzapi(phone_number: str, message: str):
    """Envia mensagem via WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return
    
    send_url = f"{WUZAPI_API_URL}/chat/send/text"
    payload = {"number": phone_number, "text": message}
    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
    
    try:
        logger.info(f"Enviando mensagem para {phone_number}")
        response = requests.post(send_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("✅ Mensagem enviada via WuzAPI")
    except Exception as e:
        logger.error(f"❌ Erro WuzAPI: {e}")

# --- ENDPOINTS ---

@app.post("/webhook/wuzapi")
async def handle_wuzapi_webhook(request: Request):
    """Recebe webhooks do WuzAPI."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do WuzAPI")
        
        # Extrair dados
        raw_data = data.get("jsonData", data)
        event_type = raw_data.get("type")
        
        if event_type != "Message":
            logger.info(f"Ignorando evento: {event_type}")
            return {"status": "ignored", "reason": f"event is {event_type}"}
        
        event_data = raw_data.get("event", {})
        info = event_data.get("Info", event_data)
        
        sender_raw = info.get('SenderAlt') or info.get('Sender')
        if not sender_raw:
            return {"status": "ignored", "reason": "no sender"}
        
        # Verificar grupo
        chat_jid = info.get("Chat") or info.get("ChatJid") or sender_raw
        if "@g.us" in chat_jid:
            logger.info("Ignorando mensagem de grupo")
            return {"status": "ignored", "reason": "group chat"}
        
        # Extrair conteúdo
        message_data = event_data.get("Message", event_data)
        message_content = message_data.get("conversation") or message_data.get("body")
        
        if not message_content:
            message_type = info.get("Type", "text")
            if message_type != "text":
                message_content = f"[{message_type.capitalize()} recebida]"
            else:
                return {"status": "ignored", "reason": "empty content"}
        
        # Preparar dados do contato
        sender_phone = sender_raw.split('@')[0]
        sender_name = info.get("PushName") or info.get("pushName", sender_phone)
        
        logger.info(f"Processando mensagem de {sender_name} ({sender_phone})")
        
        # Buscar avatar
        avatar_url = get_wuzapi_profile_pic(sender_raw)
        
        # Criar contato e conversa
        contact_id = search_or_create_contact(sender_name, sender_phone, avatar_url)
        if not contact_id:
            logger.error("Falha ao criar contato")
            return {"status": "error", "reason": "contact creation failed"}
        
        conversation_id = find_or_create_conversation(contact_id)
        if not conversation_id:
            logger.error("Falha ao criar conversa")
            return {"status": "error", "reason": "conversation creation failed"}
        
        # Enviar mensagem
        result = send_message_to_conversation(conversation_id, message_content)
        
        if result:
            logger.info("✅ Mensagem entregue ao Chatwoot")
            return {"status": "success"}
        else:
            logger.error("❌ Falha ao entregar mensagem ao Chatwoot")
            return {"status": "error", "reason": "chatwoot delivery failed"}
            
    except Exception as e:
        logger.error(f"❌ Erro fatal: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Endpoints de teste
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Ponte Ricard-ZAP",
        "chatwoot_configured": bool(CHATWOOT_URL and CHATWOOT_API_TOKEN),
        "wuzapi_configured": bool(WUZAPI_API_URL and WUZAPI_API_TOKEN)
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/debug/env")
async def debug_env():
    """Debug das variáveis de ambiente"""
    return {
        "chatwoot_url": CHATWOOT_URL,
        "chatwoot_account_id": CHATWOOT_ACCOUNT_ID,
        "chatwoot_inbox_id": CHATWOOT_INBOX_ID,
        "wuzapi_url": WUZAPI_API_URL,
        "wuzapi_instance": WUZAPI_INSTANCE_NAME,
        "chatwoot_token_masked": f"{CHATWOOT_API_TOKEN[:5]}...{CHATWOOT_API_TOKEN[-5:]}" if CHATWOOT_API_TOKEN else None
    }

@app.post("/test/chatwoot")
async def test_chatwoot():
    """Testa conexão com Chatwoot"""
    try:
        url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
        headers = {'api_access_token': CHATWOOT_API_TOKEN}
        response = requests.get(url, headers=headers, timeout=10)
        
        return {
            "status": response.status_code,
            "success": response.status_code == 200,
            "message": "Conexão OK" if response.status_code == 200 else "Erro de conexão"
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/webhook/chatwoot")
async def handle_chatwoot_webhook(request: Request):
    """Recebe webhooks do Chatwoot"""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do Chatwoot")
        
        # Validações básicas
        if data.get("event") != "message_created":
            return {"status": "ignored", "reason": "not a message_created event"}
        
        if data.get("message_type") != "outgoing":
            return {"status": "ignored", "reason": "not an outgoing message"}
        
        content = data.get("content")
        if not content:
            return {"status": "ignored", "reason": "empty content"}
        
        # Buscar número de telefone
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
        
        # Enviar para WhatsApp
        send_message_via_wuzapi(destination, content)
        
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Erro: {e}")
        return {"status": "error", "detail": str(e)}
