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

def format_phone_for_chatwoot(phone_number: str) -> str:
    """Formata o número de telefone para o padrão do Chatwoot."""
    clean = re.sub(r'[^0-9]', '', phone_number)
    
    if not clean.startswith('55'):
        clean = f"55{clean}"
    
    return f"+{clean}"

def extract_media_message(message_data: dict) -> tuple:
    """
    Extrai informações de mídia da mensagem.
    Retorna (tipo, url, legenda, nome_arquivo)
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
    
    # Áudio
    elif message_data.get("audioMessage"):
        media_type = "audio"
        audio = message_data.get("audioMessage", {})
        media_url = audio.get("url")
        filename = "audio.ogg"
    
    # Vídeo
    elif message_data.get("videoMessage"):
        media_type = "video"
        video = message_data.get("videoMessage", {})
        media_url = video.get("url")
        caption = video.get("caption", "")
        filename = "video.mp4"
    
    # Documento
    elif message_data.get("documentMessage"):
        media_type = "document"
        doc = message_data.get("documentMessage", {})
        media_url = doc.get("url")
        filename = doc.get("fileName", "documento.pdf")
        caption = doc.get("caption", "")
    
    # Sticker
    elif message_data.get("stickerMessage"):
        media_type = "sticker"
        sticker = message_data.get("stickerMessage", {})
        media_url = sticker.get("url")
        filename = "sticker.webp"
    
    return media_type, media_url, caption, filename

def download_media_from_wuzapi(media_url: str) -> bytes:
    """Baixa mídia da WuzAPI usando a URL fornecida."""
    try:
        headers = {"token": WUZAPI_API_TOKEN}
        response = requests.get(media_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"❌ Erro ao baixar mídia: {e}")
        return None

def upload_media_to_chatwoot(account_id: int, file_content: bytes, filename: str) -> Optional[str]:
    """Faz upload de mídia para o Chatwoot e retorna a URL."""
    try:
        upload_url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/contacts/upload"
        
        files = {
            'attachment': (filename, file_content, 'application/octet-stream')
        }
        
        headers = {
            'api_access_token': CHATWOOT_API_TOKEN
        }
        
        response = requests.post(upload_url, headers=headers, files=files, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            media_url = data.get("attachment_url")
            logger.info(f"✅ Mídia enviada ao Chatwoot: {media_url}")
            return media_url
        else:
            logger.error(f"❌ Erro no upload: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao fazer upload: {e}")
        return None

def send_media_message_to_chatwoot(conversation_id: int, media_type: str, media_url: str, caption: str = "", filename: str = ""):
    """Envia uma mensagem com mídia para o Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    
    # Para Chatwoot, enviamos a URL da mídia no conteúdo
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
            logger.info(f"✅ Mensagem com mídia enviada com sucesso!")
            return response.json()
        else:
            logger.error(f"❌ Falha ao enviar mídia: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem com mídia: {e}")
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
            
            # Para envio de mídia, precisamos baixar da URL do Chatwoot e enviar para WuzAPI
            # Ou usar o endpoint específico da WuzAPI para envio de mídia
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

# --- FUNÇÕES DE INTERAÇÃO COM O CHATWOOT ---

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

def create_contact(name: str, phone_number: str):
    """Cria um novo contato no Chatwoot."""
    contact_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
    
    formatted_phone = format_phone_for_chatwoot(phone_number)
        
    payload = {
        "inbox_id": int(CHATWOOT_INBOX_ID),
        "name": name,
        "phone_number": formatted_phone,
    }
    
    try:
        logger.info(f"📝 Criando contato: {name} ({formatted_phone})")
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
    clean_phone = extract_phone_number(phone_number)
    
    contact = search_contact(clean_phone)
    
    if contact:
        return contact['id']
    
    new_contact = create_contact(name, clean_phone)
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
    """Envia uma mensagem de texto para uma conversa específica no Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensagem enviada com sucesso!")
            return response.json()
        else:
            logger.error(f"❌ Falha ao enviar: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem: {e}")
        return None

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
        
        # Extrair mensagem
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
        
        # Dados do contato
        sender_phone = extract_phone_number(sender_raw)
        sender_name = info.get("PushName") or info.get("pushName") or sender_phone
        
        logger.info(f"Processando: {sender_name} ({sender_phone})")
        logger.info(f"Mensagem: {message_content[:100]}")
        
        # Criar contato e conversa
        contact_id = search_or_create_contact(sender_name, sender_phone)
        if not contact_id:
            logger.error("Falha ao criar contato")
            return {"status": "error", "reason": "contact failed"}
        
        conversation_id = find_or_create_conversation(contact_id)
        if not conversation_id:
            logger.error("Falha ao criar conversa")
            return {"status": "error", "reason": "conversation failed"}
        
        # Enviar mensagem (texto ou mídia)
        if media_type and media_url:
            result = send_media_message_to_chatwoot(conversation_id, media_type, media_url, message_content, filename)
        else:
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
        logger.debug(json.dumps(data, indent=2))
        
        # Validar evento
        if data.get("event") != "message_created":
            return {"status": "ignored", "reason": "not message_created"}
        
        # Validar tipo - ACEITAR outgoing E incoming
        message_type = data.get("message_type")
        
        # Ignorar apenas se for mensagem do sistema ou privada
        if data.get("private"):
            return {"status": "ignored", "reason": "private message"}
        
        # Pegar conteúdo
        content = data.get("content")
        if not content:
            # Verificar se tem anexo
            attachments = data.get("attachments", [])
            if attachments:
                content = f"[Anexo: {attachments[0].get('file_name', 'arquivo')}]"
                media_url = attachments[0].get("data_url") or attachments[0].get("url")
                media_type = attachments[0].get("file_type", "document").split('/')[0]
            else:
                return {"status": "ignored", "reason": "empty content"}
        else:
            media_url = None
            media_type = None
        
        # Buscar número do contato
        conversation = data.get("conversation", {})
        contact_phone = (
            conversation.get("meta", {}).get("sender", {}).get("phone_number") or
            conversation.get("contact", {}).get("phone_number")
        )
        
        if not contact_phone:
            logger.error("Telefone não encontrado")
            return {"status": "error", "reason": "phone not found"}
        
        # Limpar número para envio
        destination = re.sub(r'\D', '', contact_phone)
        
        logger.info(f"Enviando resposta para {destination}: {content[:100]}")
        
        # Enviar mensagem via WuzAPI
        if media_url and media_type:
            send_message_via_wuzapi(destination, content, media_url, media_type)
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
