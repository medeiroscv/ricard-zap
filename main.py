import os
import sys
from fastapi import FastAPI, Request, HTTPException
import requests
import json
from dotenv import load_dotenv
import re
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
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

def get_chatwoot_headers(is_file_upload: bool = False) -> dict:
    """Gera os cabeçalhos para requisições ao Chatwoot."""
    headers = {
        'api_access_token': CHATWOOT_API_TOKEN
    }
    if not is_file_upload:
        headers['Content-Type'] = 'application/json'
    return headers

# Cria a aplicação FastAPI
app = FastAPI(title="Ponte Ricard-ZAP", version="1.0.0")

# ============================================================
# FUNÇÕES PARA LID (LOCAL IDENTIFIER)
# ============================================================

def is_lid_identifier(identifier: str) -> bool:
    """Verifica se o identificador é um LID (termina com @lid)"""
    return identifier.endswith('@lid') if identifier else False

def extract_phone_number(sender_raw: str) -> str:
    """
    Extrai o número de telefone do formato do WuzAPI.
    Exemplos:
    - 553491115553:30@s.whatsapp.net -> 553491115553
    - 553496616325@s.whatsapp.net -> 553496616325
    """
    if not sender_raw:
        return ""
    
    if is_lid_identifier(sender_raw):
        return sender_raw
    
    phone_with_suffix = sender_raw.split('@')[0]
    
    # Remove sufixo como :30, :1, etc
    if ':' in phone_with_suffix:
        phone_with_suffix = phone_with_suffix.split(':')[0]
    
    # Remove qualquer caractere não numérico
    clean_phone = re.sub(r'[^0-9]', '', phone_with_suffix)
    
    return clean_phone

def extract_real_number_from_message(data: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extrai o número real do contato quando disponível."""
    real_number = None
    lid = None
    jid_completo = None
    
    if not data:
        return real_number, lid, jid_completo
    
    if data.get('sender'):
        sender = data.get('sender')
        if '@s.whatsapp.net' in sender:
            real_number = extract_phone_number(sender)
            jid_completo = sender
        elif '@lid' in sender:
            lid = sender
    
    if data.get('SenderPn'):
        real_number = data.get('SenderPn')
        jid_completo = f"{real_number}@s.whatsapp.net" if real_number else None
    
    if data.get('remoteJid'):
        remote = data.get('remoteJid')
        if '@lid' in remote:
            lid = remote
        elif '@s.whatsapp.net' in remote and not real_number:
            real_number = extract_phone_number(remote)
            jid_completo = remote
    
    raw_data = data.get('jsonData', data)
    event_data = raw_data.get('event', {})
    info = event_data.get('Info', event_data)
    
    if not real_number:
        sender_alt = info.get('SenderAlt')
        if sender_alt and '@s.whatsapp.net' in sender_alt:
            real_number = extract_phone_number(sender_alt)
            jid_completo = sender_alt
    
    return real_number, lid, jid_completo

def extract_jid_and_lid(sender_raw: str, data: dict = None) -> Tuple[str, Optional[str]]:
    """Extrai o JID e LID do formato do WuzAPI."""
    if is_lid_identifier(sender_raw) and data:
        real_number, lid, jid_completo = extract_real_number_from_message(data)
        if jid_completo:
            logger.info(f"🔄 LID detectado: {sender_raw} -> JID para envio: {jid_completo}")
            return jid_completo, lid
    
    jid = sender_raw
    lid = None
    if '@lid' in jid:
        lid = jid
    elif ':' in jid:
        parts = jid.split(':')
        if len(parts) > 1:
            lid = f"{parts[0]}:{parts[1].split('@')[0]}"
    
    return jid, lid

def format_phone_for_chatwoot(phone_number: str) -> str:
    """Formata o número de telefone para o padrão do Chatwoot."""
    if not phone_number:
        return ""
    
    if is_lid_identifier(phone_number):
        return phone_number
    
    clean = re.sub(r'[^0-9]', '', phone_number)
    
    if not clean.startswith('55'):
        clean = f"55{clean}"
    
    return f"+{clean}"

def clean_number_for_wuzapi(phone_number: str) -> str:
    """
    Limpa o número de telefone para envio via WuzAPI.
    Remove sufixos como :30 e mantém apenas números.
    Ex: 553491115553:30@s.whatsapp.net -> 553491115553
    """
    if not phone_number:
        return ""
    
    # Se for LID, mantém como está
    if is_lid_identifier(phone_number):
        return phone_number
    
    # Extrai apenas números
    clean = re.sub(r'[^0-9]', '', phone_number)
    
    return clean

# ============================================================
# FUNÇÕES DE INTERAÇÃO COM O CHATWOOT
# ============================================================

def search_contact(identifier: str):
    """Busca um contato no Chatwoot pelo identificador."""
    if not identifier:
        return None
    
    search_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search"
    params = {'q': identifier}
    
    try:
        logger.info(f"🔍 Buscando contato: {identifier}")
        response = requests.get(search_endpoint, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Erro na busca: {response.status_code}")
            return None
            
        data = response.json()
        
        if data.get("meta", {}).get("count", 0) > 0:
            for contact in data.get("payload", []):
                contact_phone = contact.get("phone_number", "")
                custom = contact.get("custom_attributes", {})
                
                if (contact_phone == identifier or
                    custom.get("whatsapp_lid") == identifier or
                    custom.get("whatsapp_jid") == identifier or
                    custom.get("whatsapp_chat_id") == identifier):
                    logger.info(f"✅ Contato encontrado: ID {contact['id']}")
                    return contact
        
        return None
    except Exception as e:
        logger.error(f"❌ Erro ao buscar contato: {e}")
        return None

def create_whatsapp_contact(name: str, phone_number: str, jid: str, lid: Optional[str] = None, 
                            real_number: Optional[str] = None, has_phone_consent: bool = False):
    """Cria um contato no Chatwoot com os campos nativos do WhatsApp."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
    
    primary_identifier = real_number if real_number else phone_number
    formatted_phone = format_phone_for_chatwoot(primary_identifier)
    
    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "phone_number": formatted_phone,
        "custom_attributes": {
            "whatsapp_chat_id": primary_identifier,
            "whatsapp_jid": jid,
            "whatsapp_lid": lid or primary_identifier,
            "whatsapp_instance": WUZAPI_INSTANCE_NAME,
            "is_lid_contact": is_lid_identifier(jid) or is_lid_identifier(lid),
            "whatsapp_has_phone_consent": has_phone_consent
        }
    }
    
    if real_number and real_number != lid:
        payload["custom_attributes"]["whatsapp_real_number"] = real_number
    
    try:
        logger.info(f"📝 Criando contato: {name}")
        logger.info(f"   JID: {jid}")
        logger.info(f"   LID: {lid}")
        
        response = requests.post(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            contact = response.json()["payload"]["contact"]
            logger.info(f"✅ Contato criado: ID {contact['id']}")
            return contact
        else:
            logger.error(f"❌ Erro ao criar: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None

def update_whatsapp_contact(contact_id: int, name: str, phone_number: str, jid: str, 
                            lid: Optional[str] = None, real_number: Optional[str] = None,
                            has_phone_consent: bool = False):
    """Atualiza um contato existente."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}"
    
    primary_identifier = real_number if real_number else phone_number
    formatted_phone = format_phone_for_chatwoot(primary_identifier)
    
    payload = {
        "name": name,
        "phone_number": formatted_phone,
        "custom_attributes": {
            "whatsapp_chat_id": primary_identifier,
            "whatsapp_jid": jid,
            "whatsapp_lid": lid or primary_identifier,
            "whatsapp_instance": WUZAPI_INSTANCE_NAME,
            "is_lid_contact": is_lid_identifier(jid) or is_lid_identifier(lid),
            "whatsapp_has_phone_consent": has_phone_consent
        }
    }
    
    if real_number and real_number != lid:
        payload["custom_attributes"]["whatsapp_real_number"] = real_number
    
    try:
        response = requests.put(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Contato atualizado: ID {contact_id}")
            return True
        else:
            logger.error(f"❌ Erro ao atualizar: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return False

def find_or_create_whatsapp_contact(name: str, sender_raw: str, data: dict = None) -> Optional[int]:
    """Busca ou cria contato."""
    if not sender_raw:
        return None
    
    real_number, lid, jid_completo = extract_real_number_from_message(data) if data else (None, None, None)
    has_phone_consent = real_number is not None
    
    if is_lid_identifier(sender_raw):
        identifier = sender_raw
        jid = jid_completo if jid_completo else sender_raw
    else:
        identifier = extract_phone_number(sender_raw)
        jid = sender_raw
    
    contact = search_contact(identifier)
    
    if contact:
        contact_id = contact['id']
        update_whatsapp_contact(contact_id, name, identifier, jid, lid, real_number, has_phone_consent)
        return contact_id
    
    new_contact = create_whatsapp_contact(name, identifier, jid, lid, real_number, has_phone_consent)
    if new_contact:
        return new_contact['id']
    
    return None

def find_or_create_conversation(contact_id: int) -> Optional[int]:
    """Busca conversa existente ou cria nova."""
    if not contact_id:
        return None
    
    conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations"
    
    try:
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        
        if response.status_code == 200:
            conversations = response.json().get("payload", [])
            active = [c for c in conversations if c.get("status") in ["open", "pending"]]
            
            if active:
                return active[0]['id']
            elif conversations:
                return conversations[0]['id']
        
        create_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {"inbox_id": int(CHATWOOT_INBOX_ID), "contact_id": contact_id}
        
        create_response = requests.post(create_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if create_response.status_code == 200:
            return create_response.json()['id']
        
        return None
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None

def send_message_to_conversation(conversation_id: int, message_content: str):
    """Envia mensagem para o Chatwoot."""
    if not conversation_id or not message_content:
        return None
    
    endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        response = requests.post(endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada para conversa {conversation_id}")
            return response.json()
        else:
            logger.error(f"❌ Falha: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None

def send_message_via_wuzapi(phone_number: str, message: str, media_url: str = None, media_type: str = None):
    """Envia mensagem via WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return False

    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
    
    try:
        # Limpa o número corretamente para envio
        if is_lid_identifier(phone_number):
            destination = phone_number
        else:
            # Extrai apenas números, removendo sufixos como :30
            destination = clean_number_for_wuzapi(phone_number)
        
        logger.info(f"📤 Enviando para: {destination}")
        logger.info(f"   Mensagem: {message[:100] if message else '[Mídia]'}")
        
        if media_url and media_type:
            media_payload = {"number": destination, "media": media_url, "caption": message or ""}
            
            endpoints = {
                "image": f"{WUZAPI_API_URL}/chat/send/image",
                "video": f"{WUZAPI_API_URL}/chat/send/video",
                "audio": f"{WUZAPI_API_URL}/chat/send/audio",
                "document": f"{WUZAPI_API_URL}/chat/send/document"
            }
            
            send_url = endpoints.get(media_type, f"{WUZAPI_API_URL}/chat/send/text")
            response = requests.post(send_url, headers=headers, json=media_payload, timeout=30)
        else:
            send_url = f"{WUZAPI_API_URL}/chat/send/text"
            payload = {"number": destination, "text": message}
            response = requests.post(send_url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada com sucesso!")
            return True
        else:
            logger.error(f"❌ Erro: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return False

# ============================================================
# FUNÇÃO PARA EXTRAIR DESTINATÁRIO DO WEBHOOK DO CHATWOOT
# ============================================================

def extract_destination_from_chatwoot_webhook(data: dict) -> Optional[str]:
    """
    Extrai o destinatário (JID para envio) do webhook do Chatwoot.
    Busca em múltiplos locais da estrutura do webhook.
    """
    # 1. Buscar no meta.sender.custom_attributes (local onde os dados estão no log)
    conversation = data.get("conversation", {})
    meta = conversation.get("meta", {})
    sender_meta = meta.get("sender", {})
    custom_attrs = sender_meta.get("custom_attributes", {})
    
    if custom_attrs.get("whatsapp_jid"):
        logger.info(f"✅ Destinatário encontrado em meta.sender.custom_attributes.whatsapp_jid: {custom_attrs.get('whatsapp_jid')}")
        return custom_attrs.get("whatsapp_jid")
    
    if custom_attrs.get("whatsapp_lid"):
        logger.info(f"✅ Destinatário encontrado em meta.sender.custom_attributes.whatsapp_lid: {custom_attrs.get('whatsapp_lid')}")
        return custom_attrs.get("whatsapp_lid")
    
    # 2. Buscar no contact.custom_attributes (estrutura alternativa)
    contact = conversation.get("contact", {})
    custom = contact.get("custom_attributes", {})
    
    if custom.get("whatsapp_jid"):
        logger.info(f"✅ Destinatário encontrado em contact.custom_attributes.whatsapp_jid: {custom.get('whatsapp_jid')}")
        return custom.get("whatsapp_jid")
    
    if custom.get("whatsapp_lid"):
        logger.info(f"✅ Destinatário encontrado em contact.custom_attributes.whatsapp_lid: {custom.get('whatsapp_lid')}")
        return custom.get("whatsapp_lid")
    
    # 3. Buscar no phone_number do contato
    if contact.get("phone_number"):
        phone = contact.get("phone_number")
        logger.info(f"✅ Destinatário encontrado em contact.phone_number: {phone}")
        return phone
    
    # 4. Buscar no meta.sender.phone_number
    if sender_meta.get("phone_number"):
        phone = sender_meta.get("phone_number")
        logger.info(f"✅ Destinatário encontrado em meta.sender.phone_number: {phone}")
        return phone
    
    # 5. Buscar pela conversa via API
    conversation_id = conversation.get("id") or data.get("conversation_id")
    if conversation_id:
        logger.info(f"🔄 Buscando detalhes da conversa {conversation_id} via API")
        conv_detail_url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}"
        try:
            conv_response = requests.get(conv_detail_url, headers=get_chatwoot_headers(), timeout=10)
            if conv_response.status_code == 200:
                conv_data = conv_response.json()
                conv_contact = conv_data.get("contact", {})
                conv_custom = conv_contact.get("custom_attributes", {})
                
                if conv_custom.get("whatsapp_jid"):
                    return conv_custom.get("whatsapp_jid")
                if conv_custom.get("whatsapp_lid"):
                    return conv_custom.get("whatsapp_lid")
                if conv_contact.get("phone_number"):
                    return conv_contact.get("phone_number")
        except Exception as e:
            logger.error(f"Erro ao buscar conversa: {e}")
    
    return None

# ============================================================
# ENDPOINTS
# ============================================================

@app.post("/webhook/wuzapi")
async def handle_wuzapi_webhook(request: Request):
    """Recebe webhooks do WuzAPI."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do WuzAPI")
        
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
        
        chat_jid = info.get('Chat') or info.get('ChatJid') or sender_raw
        if "@g.us" in str(chat_jid):
            return {"status": "ignored", "reason": "group chat"}
        
        message_data = event_data.get("Message", event_data)
        message_content = message_data.get("conversation") or message_data.get("body")
        
        if not message_content and message_data.get("extendedTextMessage"):
            message_content = message_data.get("extendedTextMessage", {}).get("text")
        
        if not message_content:
            return {"status": "ignored", "reason": "empty content"}
        
        sender_name = info.get("PushName") or info.get("pushName") or extract_phone_number(sender_raw)
        
        logger.info(f"📱 {sender_name} - {sender_raw}")
        logger.info(f"💬 {message_content[:100]}")
        
        contact_id = find_or_create_whatsapp_contact(sender_name, sender_raw, data)
        if not contact_id:
            return {"status": "error", "reason": "contact failed"}
        
        conversation_id = find_or_create_conversation(contact_id)
        if not conversation_id:
            return {"status": "error", "reason": "conversation failed"}
        
        result = send_message_to_conversation(conversation_id, message_content)
        
        if result:
            return {"status": "success"}
        return {"status": "error", "reason": "send failed"}
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

@app.post("/webhook/chatwoot")
async def handle_chatwoot_webhook(request: Request):
    """Recebe webhooks do Chatwoot e envia para o WhatsApp."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do Chatwoot")
        
        # ============================================================
        # VALIDAÇÕES
        # ============================================================
        
        event = data.get("event")
        logger.info(f"Evento: {event}")
        
        # Só processa eventos de mensagem criada
        if event != "message_created":
            logger.info(f"Ignorando evento: {event}")
            return {"status": "ignored", "reason": f"event is {event}"}
        
        # Verificar se é mensagem privada
        if data.get("private"):
            logger.info("Ignorando mensagem privada")
            return {"status": "ignored", "reason": "private message"}
        
        # Verificar tipo de mensagem
        message_type = data.get("message_type")
        logger.info(f"Tipo de mensagem: {message_type}")
        
        # Aceitar apenas mensagens outgoing (enviadas pelo agente)
        if message_type != "outgoing":
            logger.info(f"Ignorando tipo: {message_type}")
            return {"status": "ignored", "reason": f"invalid message_type: {message_type}"}
        
        # ============================================================
        # EXTRAIR CONTEÚDO
        # ============================================================
        
        content = data.get("content")
        attachments = data.get("attachments", [])
        
        logger.info(f"Conteúdo: {content[:100] if content else 'None'}")
        logger.info(f"Anexos: {len(attachments)}")
        
        if not content and not attachments:
            logger.info("Mensagem sem conteúdo")
            return {"status": "ignored", "reason": "empty content"}
        
        # ============================================================
        # EXTRAIR DESTINATÁRIO
        # ============================================================
        
        destination = extract_destination_from_chatwoot_webhook(data)
        
        if not destination:
            logger.error("❌ NÃO FOI POSSÍVEL ENCONTRAR O DESTINATÁRIO")
            return {"status": "error", "reason": "destination not found"}
        
        # ============================================================
        # ENVIAR MENSAGEM
        # ============================================================
        
        logger.info(f"🎯 Enviando para: {destination}")
        
        success = False
        if attachments:
            att = attachments[0]
            media_url = att.get("data_url") or att.get("url")
            media_type = att.get("file_type", "document").split('/')[0]
            success = send_message_via_wuzapi(destination, content or "", media_url, media_type)
        else:
            success = send_message_via_wuzapi(destination, content)
        
        if success:
            logger.info("✅ Mensagem enviada com sucesso!")
            return {"status": "success", "destination": destination}
        else:
            logger.error("❌ Falha ao enviar mensagem")
            return {"status": "error", "reason": "send failed"}
        
    except Exception as e:
        logger.error(f"❌ Erro: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

# ============================================================
# ENDPOINTS DE DIAGNÓSTICO
# ============================================================

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
        "wuzapi_instance": WUZAPI_INSTANCE_NAME,
        "chatwoot_token_configured": bool(CHATWOOT_API_TOKEN),
        "wuzapi_token_configured": bool(WUZAPI_API_TOKEN)
    }

@app.get("/debug/contacts")
async def debug_contacts():
    """Lista os últimos contatos criados"""
    try:
        url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
        params = {"sort": "-created_at", "limit": 10}
        response = requests.get(url, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        if response.status_code == 200:
            contacts = response.json().get("payload", [])
            result = []
            for c in contacts[:5]:
                result.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "phone": c.get("phone_number"),
                    "custom": c.get("custom_attributes", {})
                })
            return {"contacts": result}
        return {"error": f"Status {response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
