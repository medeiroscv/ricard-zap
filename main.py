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

# Adicionar handler para arquivo rotativo
try:
    handler = RotatingFileHandler('bridge.log', maxBytes=10485760, backupCount=5)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
except Exception as e:
    logger.warning(f"Não foi possível configurar logging em arquivo: {e}")

# Carrega as variáveis de ambiente do arquivo .env no início de tudo
load_dotenv()

# --- Diagnóstico e Validação das Variáveis de Ambiente ---
logger.info("Verificando Variáveis de Ambiente na Inicialização")

VARS_TO_CHECK = {
    "CHATWOOT_URL": os.getenv("CHATWOOT_URL"),
    "CHATWOOT_ACCOUNT_ID": os.getenv("CHATWOOT_ACCOUNT_ID"),
    "CHATWOOT_INBOX_ID": os.getenv("CHATWOOT_INBOX_ID"),
    "CHATWOOT_API_TOKEN": os.getenv("CHATWOOT_API_TOKEN"),
    "WUZAPI_API_URL": os.getenv("WUZAPI_API_URL"),
    "WUZAPI_API_TOKEN": os.getenv("WUZAPI_API_TOKEN"),
    "WUZAPI_INSTANCE_NAME": os.getenv("WUZAPI_INSTANCE_NAME"),
}

missing_vars = []
for var_name, value in VARS_TO_CHECK.items():
    if not value:
        logger.error(f"Variável de ambiente '{var_name}' NÃO ENCONTRADA.")
        missing_vars.append(var_name)
    else:
        if "TOKEN" in var_name:
            logger.info(f"Variável '{var_name}' carregada (termina com '...{value[-4:]}').")
        else:
            logger.info(f"Variável '{var_name}' carregada com o valor: {value}")

if missing_vars:
    logger.critical(f"Aplicação não pode iniciar. Variáveis faltando: {', '.join(missing_vars)}")
    sys.exit(1)

# Atribuição das variáveis após a validação bem-sucedida
CHATWOOT_URL = VARS_TO_CHECK["CHATWOOT_URL"]
CHATWOOT_ACCOUNT_ID = VARS_TO_CHECK["CHATWOOT_ACCOUNT_ID"]
CHATWOOT_INBOX_ID = VARS_TO_CHECK["CHATWOOT_INBOX_ID"]
CHATWOOT_API_TOKEN = VARS_TO_CHECK["CHATWOOT_API_TOKEN"]
WUZAPI_API_URL = VARS_TO_CHECK["WUZAPI_API_URL"]
WUZAPI_API_TOKEN = VARS_TO_CHECK["WUZAPI_API_TOKEN"]
WUZAPI_INSTANCE_NAME = VARS_TO_CHECK["WUZAPI_INSTANCE_NAME"]

# Cache simples para evitar duplicatas de mensagens
message_cache: Dict[str, datetime] = {}
CACHE_TTL = 3600  # 1 hora

def is_duplicate_message(message_id: str) -> bool:
    """Verifica se a mensagem já foi processada."""
    now = datetime.now()
    # Limpar cache antigo
    for mid, timestamp in list(message_cache.items()):
        if now - timestamp > timedelta(seconds=CACHE_TTL):
            del message_cache[mid]
    
    if message_id in message_cache:
        logger.warning(f"Mensagem duplicada detectada: {message_id}")
        return True
    
    message_cache[message_id] = now
    return False

def get_chatwoot_headers(is_file_upload: bool = False) -> dict:
    """Gera os cabeçalhos padrão para as requisições à API do Chatwoot."""
    headers = {
        'api_access_token': CHATWOOT_API_TOKEN
    }
    if not is_file_upload:
        headers['Content-Type'] = 'application/json'
    return headers

# Cria a aplicação FastAPI
app = FastAPI(title="Ponte Ricard-ZAP", version="1.0.0")

# Função para enviar mensagens de texto via WuzAPI
def send_message_via_wuzapi(phone_number: str, message: str):
    """Envia uma mensagem de texto para um número de telefone usando a WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("Variáveis da WuzAPI não configuradas para enviar mensagem.")
        return

    send_url = f"{WUZAPI_API_URL}/chat/send/text"
    payload = {
        "number": phone_number,
        "text": message
    }
    headers = {
        "Content-Type": "application/json",
        "token": WUZAPI_API_TOKEN
    }

    try:
        logger.info(f"Enviando mensagem para {phone_number}")
        response = requests.post(send_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(f"Mensagem enviada com sucesso para {phone_number}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro ao enviar mensagem via WuzAPI para {phone_number}: {e}")
        if e.response is not None:
            logger.error(f"Status: {e.response.status_code}, Corpo: {e.response.text}")

# --- FUNÇÕES DE INTERAÇÃO COM O CHATWOOT ---

def search_contact(phone_number: str):
    """Busca um contato no Chatwoot pelo número de telefone."""
    search_phone = phone_number.replace('+', '')
    search_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {'q': search_phone}
    
    try:
        logger.debug(f"Buscando contato: {search_phone}")
        response = requests.get(search_endpoint, headers=get_chatwoot_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data["meta"]["count"] > 0:
            for contact in data["payload"]:
                if contact.get("phone_number", "").endswith(search_phone):
                    logger.info(f"Contato encontrado: ID {contact['id']} para {phone_number}")
                    return contact
        
        logger.info(f"Nenhum contato encontrado para {phone_number}")
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar contato {phone_number}: {e}")
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
        logger.error(f"Erro ao criar contato {name}: {e}")
        return None

def search_or_create_contact(name: str, phone_number: str, avatar_url: Optional[str] = None) -> Optional[int]:
    """Busca um contato e, se não encontrar, cria um novo. Retorna o ID."""
    contact = search_contact(phone_number)
    
    if contact:
        contact_id = contact['id']
        if avatar_url and contact.get("avatar_url") != avatar_url:
            logger.info(f"Atualizando avatar do contato {contact_id}")
            update_contact_avatar(contact_id, avatar_url)
        return contact_id
    
    new_contact = create_contact(name, phone_number, avatar_url)
    if new_contact:
        contact_id = new_contact['id']
        if avatar_url:
            update_contact_avatar(contact_id, avatar_url)
        return contact_id
    
    return None

def find_or_create_conversation(contact_id: int) -> Optional[int]:
    """Busca uma conversa existente para o contato ou cria uma nova."""
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"
    
    try:
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        response.raise_for_status()
        conversations = response.json()["payload"]
        
        if conversations:
            conv_id = conversations[0]['id']
            logger.info(f"Conversa encontrada: ID {conv_id}")
            return conv_id
        
        logger.info(f"Criando nova conversa para contato {contact_id}")
        create_conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {"inbox_id": CHATWOOT_INBOX_ID, "contact_id": contact_id}
        create_response = requests.post(create_conv_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        create_response.raise_for_status()
        new_conv_id = create_response.json()['id']
        logger.info(f"Conversa criada: ID {new_conv_id}")
        return new_conv_id
        
    except Exception as e:
        logger.error(f"Erro ao buscar/criar conversa para contato {contact_id}: {e}")
        return None

def send_message_to_conversation(conversation_id: int, message_content: str):
    """Envia uma mensagem para uma conversa específica no Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        logger.debug(f"Enviando mensagem para conversa {conversation_id}")
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Mensagem enviada para conversa {conversation_id}")
        return response.json()
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem para conversa {conversation_id}: {e}")
        return None

def get_conversation_phone_number(conversation_id: int) -> Optional[str]:
    """Obtém o número de telefone de uma conversa."""
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}"
    
    try:
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()

        # Tentativas de encontrar o telefone em diferentes locais da resposta
        phone_locations = [
            data.get("meta", {}).get("sender", {}).get("phone_number"),
            data.get("meta", {}).get("contact", {}).get("phone_number"),
            data.get("contact", {}).get("phone_number"),
            data.get("sender", {}).get("phone_number")
        ]
        
        for phone in phone_locations:
            if phone:
                logger.info(f"Telefone encontrado para conversa {conversation_id}: {phone}")
                return phone

        logger.warning(f"Telefone não encontrado para conversa {conversation_id}")
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar telefone da conversa {conversation_id}: {e}")
        return None

def update_contact_avatar(contact_id: int, avatar_url: str):
    """Atualiza o avatar de um contato."""
    if not avatar_url:
        return
    
    try:
        avatar_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}"
        payload = {"avatar_url": avatar_url}
        response = requests.put(avatar_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Avatar atualizado para contato {contact_id}")
    except Exception as e:
        logger.error(f"Erro ao atualizar avatar para contato {contact_id}: {e}")

def get_wuzapi_profile_pic(phone_number_raw: str) -> Optional[str]:
    """Busca a URL da foto de perfil de um contato na WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.warning("Variáveis da WuzAPI não configuradas para buscar foto")
        return None
    
    try:
        base_number = phone_number_raw.split("@")[0].replace("+", "")
        headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
        avatar_url = None

        # Tenta endpoint principal
        user_avatar_url = f"{WUZAPI_API_URL}/user/avatar"
        logger.debug(f"Buscando avatar via /user/avatar para: {base_number}")
        response = requests.post(user_avatar_url, headers=headers, json={"phone": base_number}, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            avatar_url = data.get("results", {}).get("url") or data.get("profileImage")

        # Fallback para endpoint legado
        if not avatar_url:
            legacy_url = f"{WUZAPI_API_URL}/chat/getProfilePic"
            logger.debug(f"Tentando endpoint legado para: {phone_number_raw}")
            legacy_response = requests.get(legacy_url, headers=headers, params={"number": phone_number_raw}, timeout=10)
            
            if legacy_response.status_code == 200:
                legacy_data = legacy_response.json()
                avatar_url = legacy_data.get("profileImage") or legacy_data.get("url")

        if avatar_url:
            logger.info(f"Avatar encontrado: {avatar_url}")
            return avatar_url

        logger.debug("Avatar não encontrado")
        return None
    except requests.exceptions.RequestException as e:
        if e.response and e.response.status_code != 404:
            logger.error(f"Erro ao buscar avatar: {e}")
        return None

def extract_message_info(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extrai e normaliza as informações da mensagem do webhook."""
    # Compatibilidade com formatos aninhados
    raw_data = data.get("jsonData", data)
    event_type = raw_data.get("type")
    
    if event_type != "Message":
        return {"valid": False, "reason": f"Event type is {event_type}"}
    
    event_data = raw_data.get("event", {})
    info = event_data.get("Info", event_data)
    
    sender_raw = info.get('SenderAlt') or info.get('Sender')
    if not sender_raw:
        return {"valid": False, "reason": "Could not determine sender"}
    
    # Ignora status broadcast
    if "@broadcast" in sender_raw:
        return {"valid": False, "reason": "status broadcast"}
    
    chat_jid = info.get("Chat") or info.get("ChatJid") or event_data.get("Chat") or sender_raw
    is_group = "@g.us" in chat_jid or info.get("IsGroup") is True or info.get("isGroup") is True
    
    if is_group:
        return {"valid": False, "reason": "group chats not supported"}
    
    message_data = event_data.get("Message", event_data)
    message_content = message_data.get("conversation") or message_data.get("body")
    message_type = info.get("Type", "text")
    
    # Gerar ID único para deduplicação
    message_id = f"{sender_raw}_{message_data.get('id', message_data.get('timestamp', ''))}"
    
    return {
        "valid": True,
        "sender_raw": sender_raw,
        "sender_phone": sender_raw.split('@')[0],
        "sender_name": info.get("PushName") or info.get("pushName", sender_raw.split('@')[0]),
        "message_content": message_content,
        "message_type": message_type,
        "message_id": message_id,
        "raw_message": message_data
    }

# --- ENDPOINTS ---

@app.post("/webhook/wuzapi")
async def handle_wuzapi_webhook(request: Request):
    """Recebe webhooks do WuzAPI e encaminha para o Chatwoot."""
    try:
        data = await request.json()
        logger.info("Webhook recebido do WuzAPI")
        logger.debug(json.dumps(data, indent=2))
        
        # Extrair informações da mensagem
        msg_info = extract_message_info(data)
        
        if not msg_info["valid"]:
            logger.info(f"Ignorando mensagem: {msg_info['reason']}")
            return {"status": "ignored", "reason": msg_info["reason"]}
        
        # Verificar duplicata
        if is_duplicate_message(msg_info["message_id"]):
            return {"status": "ignored", "reason": "duplicate message"}
        
        # Processar conteúdo vazio
        if not msg_info["message_content"]:
            if msg_info["message_type"] != "text":
                msg_info["message_content"] = f"[{msg_info['message_type'].capitalize()} recebida]"
            else:
                logger.info("Mensagem com conteúdo vazio ignorada")
                return {"status": "ignored", "reason": "empty message content"}
        
        # Buscar avatar
        avatar_url = get_wuzapi_profile_pic(msg_info["sender_raw"])
        
        # Buscar ou criar contato
        contact_id = search_or_create_contact(
            msg_info["sender_name"], 
            msg_info["sender_phone"], 
            avatar_url
        )
        
        if not contact_id:
            raise HTTPException(status_code=500, detail="Falha ao buscar/criar contato")
        
        # Buscar ou criar conversa
        conversation_id = find_or_create_conversation(contact_id)
        
        if not conversation_id:
            raise HTTPException(status_code=500, detail="Falha ao buscar/criar conversa")
        
        # Enviar mensagem
        send_message_to_conversation(conversation_id, msg_info["message_content"])
        
        logger.info("Webhook processado com sucesso")
        return {"status": "success"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao processar webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

@app.post("/webhook-wuzapi")
async def handle_wuzapi_webhook_compat(request: Request):
    """Endpoint compatível para webhooks do WuzAPI."""
    return await handle_wuzapi_webhook(request)

@app.post("/webhook/chatwoot")
async def handle_chatwoot_webhook(request: Request):
    """Recebe webhooks do Chatwoot e envia para o WhatsApp via WuzAPI."""
    try:
        data = await request.json()
        logger.info("Webhook recebido do Chatwoot")
        logger.debug(json.dumps(data, indent=2))
        
        # Validar evento
        event_name = data.get("event")
        if event_name and event_name != "message_created":
            logger.info(f"Ignorando evento: {event_name}")
            return {"status": "ignored", "reason": f"event is {event_name}"}
        
        # Validar tipo de mensagem
        if data.get("private") or data.get("message_type") != "outgoing":
            logger.info("Ignorando mensagem privada ou não outgoing")
            return {"status": "ignored", "reason": "private or not outgoing"}
        
        # Validar remetente
        sender_type = data.get("sender", {}).get("type")
        if sender_type not in ["agent_bot", "user"]:
            logger.info(f"Ignorando remetente: {sender_type}")
            return {"status": "ignored", "reason": "sender is not an agent"}
        
        # Extrair informações
        content = data.get("content")
        if not content:
            logger.info("Conteúdo vazio ignorado")
            return {"status": "ignored", "reason": "empty content"}
        
        conversation = data.get("conversation", {})
        conversation_id = conversation.get("id") or data.get("conversation_id")
        
        # Buscar número de telefone
        contact_phone = (
            conversation.get("meta", {}).get("sender", {}).get("phone_number") or
            conversation.get("contact", {}).get("phone_number") or
            data.get("sender", {}).get("phone_number")
        )
        
        if not contact_phone and conversation_id:
            contact_phone = get_conversation_phone_number(conversation_id)
        
        if not contact_phone:
            logger.error("Número de telefone não encontrado")
            return {"status": "error", "reason": "phone number not found"}
        
        # Validar grupo
        if "@" in contact_phone:
            logger.info("Ignorando grupo")
            return {"status": "ignored", "reason": "group chats not supported"}
        
        # Limpar número
        destination = re.sub(r"\D", "", contact_phone)
        
        # Enviar mensagem
        send_message_via_wuzapi(phone_number=destination, message=content)
        
        logger.info("Mensagem enviada com sucesso para o WhatsApp")
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Erro ao processar webhook do Chatwoot: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

@app.post("/webhook-chatwoot")
async def handle_chatwoot_webhook_compat(request: Request):
    """Endpoint compatível para webhooks do Chatwoot."""
    return await handle_chatwoot_webhook(request)

@app.get("/")
def read_root():
    """Endpoint de teste."""
    return {
        "message": "Ponte Ricard-ZAP -> Chatwoot está no ar!",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health_check():
    """Endpoint para verificar saúde da aplicação."""
    status = {
        "status": "healthy",
        "chatwoot_configured": bool(CHATWOOT_URL and CHATWOOT_API_TOKEN),
        "wuzapi_configured": bool(WUZAPI_API_URL and WUZAPI_API_TOKEN),
        "timestamp": datetime.now().isoformat()
    }
    
    # Testar conectividade com Chatwoot
    if status["chatwoot_configured"]:
        try:
            test_response = requests.get(
                f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts",
                headers=get_chatwoot_headers(),
                timeout=5
            )
            status["chatwoot_accessible"] = test_response.status_code == 200
        except:
            status["chatwoot_accessible"] = False
    
    # Testar conectividade com WuzAPI
    if status["wuzapi_configured"]:
        try:
            test_response = requests.get(
                f"{WUZAPI_API_URL}/status",
                headers={"token": WUZAPI_API_TOKEN},
                timeout=5
            )
            status["wuzapi_accessible"] = test_response.status_code == 200
        except:
            status["wuzapi_accessible"] = False
    
    if not all([status.get("chatwoot_accessible", True), status.get("wuzapi_accessible", True)]):
        status["status"] = "degraded"
    
    return status
