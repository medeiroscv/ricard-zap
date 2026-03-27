import os
import sys
from fastapi import FastAPI, Request, HTTPException
import requests
import json
from dotenv import load_dotenv
import re
from datetime import datetime
from typing import Optional, Dict, Any
import logging
import uvicorn
import base64

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

def extract_phone_number(sender_raw: str) -> str:
    """
    Extrai o número de telefone do formato do WuzAPI.
    Exemplos:
    - 553491115553:30@s.whatsapp.net -> 553491115553
    - 553496616325@s.whatsapp.net -> 553496616325
    """
    phone_with_suffix = sender_raw.split('@')[0]
    
    if ':' in phone_with_suffix:
        phone_with_suffix = phone_with_suffix.split(':')[0]
    
    clean_phone = re.sub(r'[^0-9]', '', phone_with_suffix)
    
    return clean_phone

def extract_jid_and_lid(sender_raw: str) -> tuple:
    """
    Extrai o JID (WhatsApp JID) e LID (WhatsApp LID) do formato do WuzAPI.
    Retorna (jid, lid)
    """
    # JID é o identificador completo
    jid = sender_raw
    
    # LID (se existir) - alguns formatos incluem :numero no final
    lid = None
    if ':' in jid:
        parts = jid.split(':')
        jid = parts[0]  # Remove o sufixo para o JID
        lid = f"{parts[0]}:{parts[1].split('@')[0]}" if len(parts) > 1 else None
    
    return jid, lid

def format_phone_for_chatwoot(phone_number: str) -> str:
    """Formata o número de telefone para o padrão do Chatwoot."""
    clean = re.sub(r'[^0-9]', '', phone_number)
    
    if not clean.startswith('55'):
        clean = f"55{clean}"
    
    return f"+{clean}"

def update_contact_whatsapp_fields(contact_id: int, phone_number: str, jid: str, lid: Optional[str] = None):
    """
    Atualiza os campos nativos do WhatsApp no Chatwoot.
    Campos: phone_number, whatsapp_chat_id, whatsapp_jid, whatsapp_lid
    """
    try:
        # Endpoint para atualizar contato
        contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}"
        
        # Formata o número
        formatted_phone = format_phone_for_chatwoot(phone_number)
        
        # Preparar payload com campos do WhatsApp
        payload = {
            "phone_number": formatted_phone,
            "custom_attributes": {
                "whatsapp_chat_id": phone_number,
                "whatsapp_jid": jid,
                "whatsapp_lid": lid or phone_number
            }
        }
        
        logger.info(f"📝 Atualizando campos WhatsApp do contato {contact_id}")
        logger.info(f"   Phone: {formatted_phone}")
        logger.info(f"   JID: {jid}")
        logger.info(f"   LID: {lid or phone_number}")
        
        response = requests.put(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Campos WhatsApp atualizados com sucesso!")
            return True
        else:
            logger.error(f"❌ Erro ao atualizar campos: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erro ao atualizar campos WhatsApp: {e}")
        return False

def search_contact(phone_number: str):
    """Busca um contato no Chatwoot pelo número de telefone."""
    search_phone = re.sub(r'[^0-9]', '', phone_number)
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
                contact_phone = re.sub(r'[^0-9]', '', contact.get("phone_number", ""))
                if contact_phone == search_phone or search_phone in contact_phone or contact_phone in search_phone:
                    logger.info(f"✅ Contato encontrado: ID {contact['id']}")
                    return contact
        
        logger.info(f"❌ Contato não encontrado")
        return None
    except Exception as e:
        logger.error(f"❌ Erro ao buscar contato: {e}")
        return None

def create_contact(name: str, phone_number: str, jid: str, lid: Optional[str] = None):
    """Cria um novo contato no Chatwoot com os campos do WhatsApp."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
    
    formatted_phone = format_phone_for_chatwoot(phone_number)
        
    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "phone_number": formatted_phone,
        "custom_attributes": {
            "whatsapp_chat_id": phone_number,
            "whatsapp_jid": jid,
            "whatsapp_lid": lid or phone_number
        }
    }
    
    try:
        logger.info(f"📝 Criando contato: {name} ({formatted_phone})")
        logger.info(f"   JID: {jid}")
        logger.info(f"   LID: {lid or phone_number}")
        
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

def search_or_create_contact(name: str, phone_number: str, jid: str, lid: Optional[str] = None) -> Optional[int]:
    """Busca um contato e, se não encontrar, cria um novo. Atualiza campos WhatsApp."""
    clean_phone = extract_phone_number(phone_number)
    
    contact = search_contact(clean_phone)
    
    if contact:
        contact_id = contact['id']
        # Atualiza os campos WhatsApp mesmo se o contato já existir
        update_contact_whatsapp_fields(contact_id, clean_phone, jid, lid)
        return contact_id
    
    new_contact = create_contact(name, clean_phone, jid, lid)
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
            
            # Filtrar apenas conversas abertas ou pendentes
            active_conversations = [c for c in conversations if c.get("status") in ["open", "pending"]]
            
            if active_conversations:
                conv_id = active_conversations[0]['id']
                logger.info(f"✅ Conversa ativa encontrada: ID {conv_id}")
                return conv_id
            elif conversations:
                conv_id = conversations[0]['id']
                logger.info(f"✅ Conversa encontrada (inativa): ID {conv_id}")
                return conv_id
        
        logger.info(f"🆕 Criando nova conversa para contato {contact_id}")
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
    """Envia uma mensagem de texto para uma conversa específica no Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        logger.info(f"📤 Enviando mensagem para conversa {conversation_id}")
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada com sucesso!")
            return response.json()
        else:
            logger.error(f"❌ Falha ao enviar: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem: {e}")
        return None

def send_message_via_wuzapi(phone_number: str, message: str, media_url: str = None, media_type: str = None):
    """Envia uma mensagem via WuzAPI, podendo ser texto ou mídia."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return

    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
    
    try:
        # Se for mídia
        if media_url and media_type:
            logger.info(f"📤 Enviando mídia via WuzAPI para {phone_number}")
            
            media_payload = {
                "number": phone_number,
                "media": media_url,
                "caption": message if message else ""
            }
            
            if media_type == "image":
                send_url = f"{WUZAPI_API_URL}/chat/send/image"
            elif media_type == "video":
                send_url = f"{WUZAPI_API_URL}/chat/send/video"
            elif media_type == "audio":
                send_url = f"{WUZAPI_API_URL}/chat/send/audio"
            elif media_type == "document":
                send_url = f"{WUZAPI_API_URL}/chat/send/document"
                media_payload["filename"] = "documento.pdf"
            else:
                send_url = f"{WUZAPI_API_URL}/chat/send/text"
                media_payload = {"number": phone_number, "text": message}
            
            response = requests.post(send_url, headers=headers, json=media_payload, timeout=30)
            
        else:
            # Mensagem de texto
            logger.info(f"📤 Enviando texto via WuzAPI para {phone_number}")
            send_url = f"{WUZAPI_API_URL}/chat/send/text"
            payload = {"number": phone_number, "text": message}
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
        
        # Extrair dados
        raw_data = data.get("jsonData", data)
        event_type = raw_data.get("type")
        
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
        
        # Verificar se é mensagem de grupo
        chat_jid = info.get('Chat') or info.get('ChatJid') or sender_raw
        
        if "@g.us" in str(chat_jid):
            logger.info(f"Ignorando mensagem de grupo")
            return {"status": "ignored", "reason": "group chat"}
        
        # Extrair JID e LID
        jid, lid = extract_jid_and_lid(sender_raw)
        
        # Extrair mensagem
        message_data = event_data.get("Message", event_data)
        
        # Mensagem de texto
        message_content = message_data.get("conversation") or message_data.get("body")
        
        if not message_content and message_data.get("extendedTextMessage"):
            message_content = message_data.get("extendedTextMessage", {}).get("text")
        
        if not message_content:
            logger.warning("Mensagem sem conteúdo")
            return {"status": "ignored", "reason": "empty content"}
        
        # Dados do contato
        sender_phone = extract_phone_number(sender_raw)
        sender_name = info.get("PushName") or info.get("pushName") or sender_phone
        
        logger.info(f"Processando: {sender_name} ({sender_phone})")
        logger.info(f"JID: {jid}")
        logger.info(f"LID: {lid}")
        logger.info(f"Mensagem: {message_content[:100]}")
        
        # Criar contato com campos WhatsApp
        contact_id = search_or_create_contact(sender_name, sender_raw, jid, lid)
        if not contact_id:
            logger.error("Falha ao criar contato")
            return {"status": "error", "reason": "contact failed"}
        
        logger.info(f"Contact ID: {contact_id}")
        
        # Buscar conversa existente
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
    """Recebe webhooks do Chatwoot e envia para o WhatsApp."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do Chatwoot")
        
        # Validar evento
        event = data.get("event")
        if event != "message_created":
            return {"status": "ignored", "reason": f"event is {event}"}
        
        # Verificar se é mensagem do sistema
        if data.get("private"):
            return {"status": "ignored", "reason": "private message"}
        
        # Pegar conteúdo
        content = data.get("content")
        attachments = data.get("attachments", [])
        
        if not content and not attachments:
            return {"status": "ignored", "reason": "empty content"}
        
        # Buscar número do contato
        conversation = data.get("conversation", {})
        
        contact_phone = None
        
        # Tenta encontrar em custom_attributes primeiro
        contact = conversation.get("contact", {})
        custom_attrs = contact.get("custom_attributes", {})
        
        if custom_attrs.get("whatsapp_chat_id"):
            contact_phone = custom_attrs.get("whatsapp_chat_id")
        elif custom_attrs.get("whatsapp_jid"):
            jid = custom_attrs.get("whatsapp_jid")
            contact_phone = jid.split('@')[0] if '@' in jid else jid
        elif contact.get("phone_number"):
            contact_phone = contact.get("phone_number")
        elif conversation.get("meta", {}).get("sender", {}).get("phone_number"):
            contact_phone = conversation.get("meta", {}).get("sender", {}).get("phone_number")
        
        if not contact_phone:
            logger.error("Telefone não encontrado")
            return {"status": "error", "reason": "phone not found"}
        
        # Limpar número para envio
        destination = re.sub(r'\D', '', contact_phone)
        
        logger.info(f"Enviando resposta para {destination}")
        
        # Se tem anexo, processa como mídia
        if attachments:
            attachment = attachments[0]
            media_url = attachment.get("data_url") or attachment.get("url")
            media_type = attachment.get("file_type", "document").split('/')[0]
            filename = attachment.get("file_name", "arquivo")
            
            send_message_via_wuzapi(destination, content or "", media_url, media_type)
        else:
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
