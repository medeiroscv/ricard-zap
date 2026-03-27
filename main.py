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
import mimetypes

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
    """
    if not phone_number:
        return ""
    
    if is_lid_identifier(phone_number):
        return phone_number
    
    clean = re.sub(r'[^0-9]', '', phone_number)
    
    if len(clean) == 15 and clean.endswith('30'):
        clean = clean[:-2]
        logger.info(f"   Removido sufixo '30' do número: {clean}")
    elif len(clean) == 14 and clean.endswith('30'):
        clean = clean[:-2]
        logger.info(f"   Removido sufixo '30' do número: {clean}")
    
    return clean

# ============================================================
# FUNÇÕES PARA MÍDIAS
# ============================================================

def extract_media_message(message_data: dict) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extrai informações de mídia da mensagem do WuzAPI.
    Retorna: (media_type, media_url, caption, filename)
    """
    media_type = None
    media_url = None
    caption = None
    filename = None
    
    # Imagem
    if message_data.get("imageMessage"):
        media_type = "image"
        img = message_data.get("imageMessage", {})
        media_url = img.get("url")
        caption = img.get("caption", "")
        filename = "imagem.jpg"
        logger.info(f"📷 Imagem detectada")
    
    # Áudio
    elif message_data.get("audioMessage"):
        media_type = "audio"
        audio = message_data.get("audioMessage", {})
        media_url = audio.get("url")
        filename = "audio.ogg"
        logger.info(f"🎵 Áudio detectado")
    
    # Vídeo
    elif message_data.get("videoMessage"):
        media_type = "video"
        video = message_data.get("videoMessage", {})
        media_url = video.get("url")
        caption = video.get("caption", "")
        filename = "video.mp4"
        logger.info(f"🎬 Vídeo detectado")
    
    # Documento
    elif message_data.get("documentMessage"):
        media_type = "document"
        doc = message_data.get("documentMessage", {})
        media_url = doc.get("url")
        filename = doc.get("fileName", "documento.pdf")
        caption = doc.get("caption", "")
        logger.info(f"📄 Documento detectado: {filename}")
    
    # Sticker
    elif message_data.get("stickerMessage"):
        media_type = "sticker"
        sticker = message_data.get("stickerMessage", {})
        media_url = sticker.get("url")
        filename = "sticker.webp"
        logger.info(f"🏷️ Sticker detectado")
    
    return media_type, media_url, caption, filename

def download_media_from_wuzapi(media_url: str) -> Optional[bytes]:
    """Baixa mídia da WuzAPI usando a URL fornecida."""
    try:
        headers = {"token": WUZAPI_API_TOKEN}
        logger.info(f"📥 Baixando mídia...")
        response = requests.get(media_url, headers=headers, timeout=30)
        response.raise_for_status()
        logger.info(f"✅ Mídia baixada: {len(response.content)} bytes")
        return response.content
    except Exception as e:
        logger.error(f"❌ Erro ao baixar mídia: {e}")
        return None

def upload_media_to_chatwoot(account_id: int, file_content: bytes, filename: str) -> Optional[str]:
    """Faz upload de mídia para o Chatwoot e retorna a URL."""
    try:
        upload_url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/contacts/upload"
        
        # Determina o tipo MIME
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        files = {
            'attachment': (filename, file_content, mime_type)
        }
        
        headers = {
            'api_access_token': CHATWOOT_API_TOKEN
        }
        
        logger.info(f"📤 Enviando mídia para Chatwoot: {filename}")
        response = requests.post(upload_url, headers=headers, files=files, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            media_url = data.get("attachment_url")
            logger.info(f"✅ Mídia enviada ao Chatwoot")
            return media_url
        else:
            logger.error(f"❌ Erro no upload: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao fazer upload: {e}")
        return None

def send_media_message_to_chatwoot(conversation_id: int, media_type: str, media_url: str, caption: str = "", filename: str = ""):
    """Envia uma mensagem com mídia para o Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    
    # Formata a mensagem com a mídia
    if media_type == "image":
        content = f"![{caption}]({media_url})"
        if caption:
            content = f"{caption}\n\n![imagem]({media_url})"
    elif media_type == "video":
        content = f"[Vídeo: {filename}]({media_url})"
        if caption:
            content = f"{caption}\n\n[Vídeo: {filename}]({media_url})"
    elif media_type == "audio":
        content = f"[Áudio: {filename}]({media_url})"
    elif media_type == "document":
        content = f"[Documento: {filename}]({media_url})"
        if caption:
            content = f"{caption}\n\n[Documento: {filename}]({media_url})"
    elif media_type == "sticker":
        content = f"[Sticker]({media_url})"
    else:
        content = f"[{media_type.capitalize()} recebida: {filename}]({media_url})"
    
    payload = {"content": content, "message_type": "incoming"}
    
    try:
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem com mídia enviada")
            return response.json()
        else:
            logger.error(f"❌ Falha ao enviar mídia: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem com mídia: {e}")
        return None

def send_media_via_wuzapi(phone_number: str, media_url: str, media_type: str, caption: str = ""):
    """Envia mídia via WuzAPI."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return False

    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
    
    try:
        destination = clean_number_for_wuzapi(phone_number)
        
        logger.info(f"📤 Enviando mídia via WuzAPI para: {destination}")
        logger.info(f"   Tipo: {media_type}")
        
        payload = {
            "number": destination,
            "media": media_url,
            "caption": caption
        }
        
        # Define o endpoint correto baseado no tipo
        endpoints = {
            "image": f"{WUZAPI_API_URL}/chat/send/image",
            "video": f"{WUZAPI_API_URL}/chat/send/video",
            "audio": f"{WUZAPI_API_URL}/chat/send/audio",
            "document": f"{WUZAPI_API_URL}/chat/send/document"
        }
        
        send_url = endpoints.get(media_type, f"{WUZAPI_API_URL}/chat/send/text")
        
        # Para documentos, adiciona filename
        if media_type == "document":
            payload["filename"] = "documento.pdf"
        
        response = requests.post(send_url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            logger.info(f"✅ Mídia enviada com sucesso!")
            return True
        else:
            logger.error(f"❌ Erro: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return False

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
    """Envia mensagem via WuzAPI (texto ou mídia)."""
    if not all([WUZAPI_API_URL, WUZAPI_API_TOKEN]):
        logger.error("WuzAPI não configurada")
        return False

    # Se for mídia, usa a função específica
    if media_url and media_type:
        return send_media_via_wuzapi(phone_number, media_url, media_type, message)

    headers = {"Content-Type": "application/json", "token": WUZAPI_API_TOKEN}
    
    try:
        destination = clean_number_for_wuzapi(phone_number)
        
        logger.info(f"📤 Enviando texto para: {destination}")
        logger.info(f"   Mensagem: {message[:100]}")
        
        send_url = f"{WUZAPI_API_URL}/chat/send/text"
        
        # Diferentes formatos de payload para tentar
        payloads_to_try = [
            {"number": destination, "text": message},
            {"phone": destination, "text": message},
            {"to": destination, "text": message},
            {"number": destination, "body": message},
            {"phone": destination, "body": message}
        ]
        
        response = None
        
        for payload in payloads_to_try:
            try:
                response = requests.post(send_url, headers=headers, json=payload, timeout=15)
                if response.status_code == 200:
                    break
            except Exception:
                continue
        
        if response and response.status_code == 200:
            logger.info(f"✅ Texto enviado com sucesso!")
            return True
        else:
            logger.error(f"❌ Falha ao enviar texto")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return False

# ============================================================
# FUNÇÃO PARA EXTRAIR DESTINATÁRIO DO WEBHOOK DO CHATWOOT
# ============================================================

def extract_destination_from_chatwoot_webhook(data: dict) -> Optional[str]:
    """Extrai o destinatário do webhook do Chatwoot."""
    conversation = data.get("conversation", {})
    meta = conversation.get("meta", {})
    sender_meta = meta.get("sender", {})
    custom_attrs = sender_meta.get("custom_attributes", {})
    
    if custom_attrs.get("whatsapp_jid"):
        return custom_attrs.get("whatsapp_jid")
    
    if custom_attrs.get("whatsapp_lid"):
        return custom_attrs.get("whatsapp_lid")
    
    contact = conversation.get("contact", {})
    custom = contact.get("custom_attributes", {})
    
    if custom.get("whatsapp_jid"):
        return custom.get("whatsapp_jid")
    
    if custom.get("whatsapp_lid"):
        return custom.get("whatsapp_lid")
    
    if contact.get("phone_number"):
        return contact.get("phone_number")
    
    if sender_meta.get("phone_number"):
        return sender_meta.get("phone_number")
    
    conversation_id = conversation.get("id") or data.get("conversation_id")
    if conversation_id:
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
        except Exception:
            pass
    
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
        
        # Verificar se é mídia
        media_type, media_url, caption, filename = extract_media_message(message_data)
        
        message_content = None
        
        if media_type:
            # É uma mensagem com mídia
            logger.info(f"📷 Mídia recebida: {media_type}")
            message_content = caption or f"[{media_type.upper()}]"
            
            # Baixar mídia da WuzAPI
            file_content = download_media_from_wuzapi(media_url)
            
            if file_content:
                # Upload para o Chatwoot
                uploaded_url = upload_media_to_chatwoot(int(CHATWOOT_ACCOUNT_ID), file_content, filename)
                
                if uploaded_url:
                    media_url = uploaded_url
        else:
            # Mensagem de texto
            message_content = message_data.get("conversation") or message_data.get("body")
            
            if not message_content and message_data.get("extendedTextMessage"):
                message_content = message_data.get("extendedTextMessage", {}).get("text")
        
        if not message_content:
            logger.warning("Mensagem sem conteúdo")
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
        
        # Enviar mensagem (texto ou mídia)
        if media_type and media_url:
            result = send_media_message_to_chatwoot(conversation_id, media_type, media_url, message_content, filename)
        else:
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
        
        event = data.get("event")
        if event != "message_created":
            return {"status": "ignored", "reason": f"event is {event}"}
        
        if data.get("private"):
            return {"status": "ignored", "reason": "private message"}
        
        message_type = data.get("message_type")
        if message_type != "outgoing":
            return {"status": "ignored", "reason": f"invalid message_type: {message_type}"}
        
        content = data.get("content")
        attachments = data.get("attachments", [])
        
        if not content and not attachments:
            return {"status": "ignored", "reason": "empty content"}
        
        destination = extract_destination_from_chatwoot_webhook(data)
        
        if not destination:
            logger.error("❌ Destinatário não encontrado")
            return {"status": "error", "reason": "destination not found"}
        
        logger.info(f"🎯 Enviando para: {destination}")
        
        success = False
        
        # Verificar se tem anexos (mídia)
        if attachments:
            for att in attachments:
                media_url = att.get("data_url") or att.get("url")
                media_type = att.get("file_type", "document").split('/')[0]
                caption = content or ""
                
                logger.info(f"📎 Anexo detectado: {media_type}")
                success = send_media_via_wuzapi(destination, media_url, media_type, caption)
                break
        else:
            # Mensagem de texto
            success = send_message_via_wuzapi(destination, content)
        
        if success:
            logger.info("✅ Mensagem enviada com sucesso!")
            return {"status": "success"}
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
