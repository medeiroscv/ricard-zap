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
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Adicionar handler para arquivo rotativo
try:
    handler = RotatingFileHandler('bridge.log', maxBytes=10485760, backupCount=5)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
except Exception as e:
    logger.warning(f"Não foi possível configurar logging em arquivo: {e}")

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
    # Limpar cache antigo
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
        logger.debug(f"URL: {search_endpoint}")
        logger.debug(f"Headers: {get_chatwoot_headers()}")
        
        response = requests.get(search_endpoint, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot - Status: {response.status_code}")
        logger.debug(f"Resposta bruta: {response.text[:500]}")
        
        response.raise_for_status()
        data = response.json()
        
        logger.info(f"📊 Meta count: {data.get('meta', {}).get('count', 0)}")
        
        if data.get("meta", {}).get("count", 0) > 0:
            for contact in data.get("payload", []):
                contact_phone = contact.get("phone_number", "")
                logger.debug(f"Comparando: {contact_phone} com {search_phone}")
                
                if contact_phone.endswith(search_phone):
                    logger.info(f"✅ Contato encontrado: ID {contact['id']} - Nome: {contact.get('name')} - Telefone: {contact_phone}")
                    return contact
        
        logger.info(f"❌ Nenhum contato encontrado para {phone_number}")
        return None
    except Exception as e:
        logger.error(f"❌ Erro ao buscar contato: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta de erro: {e.response.text}")
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
        logger.info(f"📝 Criando contato: {name} ({phone_number})")
        logger.debug(f"Payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post(contact_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot - Status: {response.status_code}")
        logger.debug(f"Resposta: {response.text}")
        
        response.raise_for_status()
        contact = response.json()["payload"]["contact"]
        logger.info(f"✅ Contato criado: ID {contact['id']}")
        return contact
    except Exception as e:
        logger.error(f"❌ Erro ao criar contato: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta de erro: {e.response.text}")
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
        logger.info(f"💬 Buscando conversas para contato {contact_id}")
        response = requests.get(conv_endpoint, headers=get_chatwoot_headers(), timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot - Status: {response.status_code}")
        logger.debug(f"Resposta: {response.text[:500]}")
        
        response.raise_for_status()
        conversations = response.json().get("payload", [])
        
        if conversations:
            conv_id = conversations[0]['id']
            logger.info(f"✅ Conversa encontrada: ID {conv_id}")
            logger.info(f"   Status: {conversations[0].get('status')}")
            logger.info(f"   Mensagens: {conversations[0].get('message_count', 0)}")
            return conv_id
        
        logger.info(f"🆕 Criando nova conversa para contato {contact_id}")
        create_conv_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {"inbox_id": CHATWOOT_INBOX_ID, "contact_id": contact_id}
        
        logger.debug(f"Payload criação conversa: {json.dumps(payload, indent=2)}")
        
        create_response = requests.post(create_conv_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot - Status: {create_response.status_code}")
        logger.debug(f"Resposta: {create_response.text}")
        
        create_response.raise_for_status()
        new_conv_id = create_response.json()['id']
        logger.info(f"✅ Conversa criada: ID {new_conv_id}")
        return new_conv_id
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar/criar conversa: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta de erro: {e.response.text}")
        return None

def send_message_to_conversation(conversation_id: int, message_content: str):
    """Envia uma mensagem para uma conversa específica no Chatwoot."""
    message_endpoint = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    payload = {"content": message_content, "message_type": "incoming"}
    
    try:
        logger.info(f"📤 Enviando mensagem para conversa {conversation_id}")
        logger.info(f"   Conteúdo: {message_content[:100]}...")
        logger.debug(f"   Payload completo: {json.dumps(payload, indent=2)}")
        logger.debug(f"   URL: {message_endpoint}")
        logger.debug(f"   Headers: {get_chatwoot_headers()}")
        
        response = requests.post(message_endpoint, headers=get_chatwoot_headers(), json=payload, timeout=10)
        
        logger.info(f"📡 Resposta Chatwoot - Status: {response.status_code}")
        logger.info(f"   Response Headers: {dict(response.headers)}")
        logger.info(f"   Response Body: {response.text[:500]}")
        
        if response.status_code == 200:
            response_data = response.json()
            logger.info(f"✅ Mensagem enviada com sucesso!")
            logger.info(f"   ID da mensagem: {response_data.get('id')}")
            logger.info(f"   Criada em: {response_data.get('created_at')}")
            return response_data
        else:
            logger.error(f"❌ Falha ao enviar mensagem. Status: {response.status_code}")
            logger.error(f"   Resposta: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar mensagem: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Resposta de erro: {e.response.text}")
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
        logger.info(f"✅ Mensagem enviada com sucesso para {phone_number}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Erro ao enviar mensagem via WuzAPI para {phone_number}: {e}")
        if e.response is not None:
            logger.error(f"Status: {e.response.status_code}, Corpo: {e.response.text}")

# --- ENDPOINTS ---

@app.post("/webhook/wuzapi")
async def handle_wuzapi_webhook(request: Request):
    """Recebe webhooks do WuzAPI e encaminha para o Chatwoot."""
    try:
        data = await request.json()
        logger.info("📨 Webhook recebido do WuzAPI")
        
        # Log completo para debug
        logger.debug(f"Dados completos: {json.dumps(data, indent=2)}")
        
        # Extrair dados
        raw_data = data.get("jsonData", data)
        event_type = raw_data.get("type")
        
        if event_type != "Message":
            logger.info(f"Ignorando evento: {event_type}")
            return {"status": "ignored", "reason": f"event is {event_type}"}
        
        event_data = raw_data.get("event", {})
        info = event_data.get("Info", event_data)
        
        # Log dos dados do Info
        logger.debug(f"Info: {json.dumps(info, indent=2)}")
        
        sender_raw = info.get('SenderAlt') or info.get('Sender')
        if not sender_raw:
            logger.warning("Não foi possível determinar o remetente")
            return {"status": "ignored", "reason": "no sender"}
        
        # CORREÇÃO: Melhor detecção de grupos
        # Verificar se é grupo por vários indicadores
        chat_jid = info.get("Chat") or info.get("ChatJid") or event_data.get("Chat") or sender_raw
        is_group = False
        
        # Indicadores de grupo
        if "@g.us" in str(chat_jid):
            is_group = True
            logger.info(f"Detectado grupo por @g.us: {chat_jid}")
        elif info.get("IsGroup") is True or info.get("isGroup") is True:
            is_group = True
            logger.info("Detectado grupo por flag IsGroup")
        elif info.get("IsGroupMsg") is True:
            is_group = True
            logger.info("Detectado grupo por IsGroupMsg")
        elif info.get("FromMe") is False and "@g.us" in str(sender_raw):
            is_group = True
            logger.info(f"Detectado grupo por sender: {sender_raw}")
        
        # Se for grupo, ignorar
        if is_group:
            logger.info(f"Ignorando mensagem de grupo: {chat_jid}")
            return {"status": "ignored", "reason": "group chat"}
        
        # Extrair conteúdo da mensagem
        message_data = event_data.get("Message", event_data)
        logger.debug(f"Message data: {json.dumps(message_data, indent=2)}")
        
        # Tentar extrair o texto da mensagem de diferentes formatos
        message_content = None
        
        # Formato 1: conversation direto
        if message_data.get("conversation"):
            message_content = message_data.get("conversation")
        # Formato 2: body
        elif message_data.get("body"):
            message_content = message_data.get("body")
        # Formato 3: extendedTextMessage
        elif message_data.get("extendedTextMessage", {}).get("text"):
            message_content = message_data.get("extendedTextMessage", {}).get("text")
        # Formato 4: dentro de message
        elif isinstance(message_data.get("message"), dict):
            msg_inner = message_data.get("message", {})
            if msg_inner.get("conversation"):
                message_content = msg_inner.get("conversation")
            elif msg_inner.get("extendedTextMessage", {}).get("text"):
                message_content = msg_inner.get("extendedTextMessage", {}).get("text")
        
        message_type = info.get("Type", "text")
        
        if not message_content:
            if message_type != "text":
                message_content = f"[{message_type.capitalize()} recebida]"
                logger.info(f"Mensagem não-texto: {message_type}")
            else:
                logger.warning("Mensagem com conteúdo vazio")
                return {"status": "ignored", "reason": "empty content"}
        
        # Preparar dados do contato
        sender_phone = sender_raw.split('@')[0]
        sender_name = info.get("PushName") or info.get("pushName") or info.get("NotifyName") or sender_phone
        
        logger.info(f"Processando mensagem de {sender_name} ({sender_phone})")
        logger.info(f"Conteúdo: {message_content[:100]}...")
        
        # Buscar avatar
        avatar_url = get_wuzapi_profile_pic(sender_raw)
        
        # Criar contato e conversa
        contact_id = search_or_create_contact(sender_name, sender_phone, avatar_url)
        if not contact_id:
            logger.error("Falha ao criar contato")
            return {"status": "error", "reason": "contact creation failed"}
        
        logger.info(f"✅ Contact ID obtido: {contact_id}")
        
        conversation_id = find_or_create_conversation(contact_id)
        if not conversation_id:
            logger.error("Falha ao criar conversa")
            return {"status": "error", "reason": "conversation creation failed"}
        
        logger.info(f"✅ Conversation ID obtido: {conversation_id}")
        
        # Enviar mensagem
        result = send_message_to_conversation(conversation_id, message_content)
        
        if result:
            logger.info(f"✅ Mensagem entregue ao Chatwoot - Conversa: {conversation_id}")
            return {"status": "success", "conversation_id": conversation_id, "message_id": result.get('id')}
        else:
            logger.error("❌ Falha ao entregar mensagem ao Chatwoot")
            return {"status": "error", "reason": "chatwoot delivery failed"}
            
    except Exception as e:
        logger.error(f"❌ Erro fatal: {e}", exc_info=True)
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
        logger.info("📨 Webhook recebido do Chatwoot")
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
        
        logger.info("✅ Mensagem enviada com sucesso para o WhatsApp")
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"❌ Erro ao processar webhook do Chatwoot: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

@app.post("/webhook-chatwoot")
async def handle_chatwoot_webhook_compat(request: Request):
    """Endpoint compatível para webhooks do Chatwoot."""
    return await handle_chatwoot_webhook(request)

@app.post("/webhook/debug")
async def debug_full_webhook(request: Request):
    """Endpoint de debug que mostra tudo que chega"""
    try:
        # Pega o corpo bruto
        body = await request.body()
        headers = dict(request.headers)
        
        print("\n" + "="*60)
        print("🔍 DEBUG COMPLETO - Webhook Recebido")
        print(f"Headers: {json.dumps(headers, indent=2)}")
        print(f"Body: {body.decode('utf-8')}")
        print("="*60 + "\n")
        
        # Tenta parsear como JSON
        try:
            json_data = await request.json()
            print(f"JSON parseado: {json.dumps(json_data, indent=2)}")
            
            # Tenta identificar se é grupo
            raw_data = json_data.get("jsonData", json_data)
            event_data = raw_data.get("event", {})
            info = event_data.get("Info", event_data)
            
            sender = info.get('SenderAlt') or info.get('Sender')
            chat = info.get('Chat') or info.get('ChatJid')
            
            print(f"\n📱 Remetente: {sender}")
            print(f"💬 Chat: {chat}")
            print(f"👥 É grupo? {'SIM' if '@g.us' in str(chat) else 'NÃO'}")
            
        except:
            print("Não foi possível parsear como JSON")
        
        return {"status": "received", "message": "Webhook recebido para debug"}
    except Exception as e:
        print(f"Erro no debug: {e}")
        return {"error": str(e)}

@app.get("/debug/chatwoot/conversations")
async def debug_chatwoot_conversations():
    """Lista as últimas conversas no Chatwoot para diagnóstico"""
    try:
        url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        params = {"status": "all", "limit": 10}
        
        response = requests.get(url, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            conversations = data.get("payload", [])
            
            result = {
                "total": len(conversations),
                "conversations": []
            }
            
            for conv in conversations[:5]:  # Mostrar últimas 5
                result["conversations"].append({
                    "id": conv.get("id"),
                    "status": conv.get("status"),
                    "contact_id": conv.get("contact_id"),
                    "contact_name": conv.get("contact", {}).get("name"),
                    "contact_phone": conv.get("contact", {}).get("phone_number"),
                    "message_count": conv.get("message_count"),
                    "created_at": conv.get("created_at"),
                    "last_activity_at": conv.get("last_activity_at")
                })
            
            return result
        else:
            return {"error": f"Status {response.status_code}", "response": response.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/chatwoot/contacts")
async def debug_chatwoot_contacts():
    """Lista os últimos contatos no Chatwoot para diagnóstico"""
    try:
        url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
        params = {"sort": "-created_at", "limit": 10}
        
        response = requests.get(url, headers=get_chatwoot_headers(), params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            contacts = data.get("payload", [])
            
            result = {
                "total": len(contacts),
                "contacts": []
            }
            
            for contact in contacts[:10]:
                result["contacts"].append({
                    "id": contact.get("id"),
                    "name": contact.get("name"),
                    "phone_number": contact.get("phone_number"),
                    "email": contact.get("email"),
                    "created_at": contact.get("created_at")
                })
            
            return result
        else:
            return {"error": f"Status {response.status_code}", "response": response.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
async def root():
    """Endpoint de teste."""
    return {
        "status": "online",
        "service": "Ponte Ricard-ZAP",
        "version": "1.0.0",
        "chatwoot_configured": bool(CHATWOOT_URL and CHATWOOT_API_TOKEN),
        "wuzapi_configured": bool(WUZAPI_API_URL and WUZAPI_API_TOKEN),
        "timestamp": datetime.now().isoformat()
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
            if not status["chatwoot_accessible"]:
                status["chatwoot_status_code"] = test_response.status_code
        except Exception as e:
            status["chatwoot_accessible"] = False
            status["chatwoot_error"] = str(e)
    
    # Testar conectividade com WuzAPI
    if status["wuzapi_configured"]:
        try:
            test_response = requests.get(
                f"{WUZAPI_API_URL}/status",
                headers={"token": WUZAPI_API_TOKEN},
                timeout=5
            )
            status["wuzapi_accessible"] = test_response.status_code == 200
            if not status["wuzapi_accessible"]:
                status["wuzapi_status_code"] = test_response.status_code
        except Exception as e:
            status["wuzapi_accessible"] = False
            status["wuzapi_error"] = str(e)
    
    if not status.get("chatwoot_accessible", True) or not status.get("wuzapi_accessible", True):
        status["status"] = "degraded"
    
    return status

@app.get("/debug/env")
async def debug_env():
    """Endpoint para debug das variáveis de ambiente"""
    return {
        "chatwoot_url": CHATWOOT_URL,
        "chatwoot_account_id": CHATWOOT_ACCOUNT_ID,
        "chatwoot_inbox_id": CHATWOOT_INBOX_ID,
        "wuzapi_url": WUZAPI_API_URL,
        "wuzapi_instance": WUZAPI_INSTANCE_NAME,
        "chatwoot_token_masked": f"{CHATWOOT_API_TOKEN[:5]}...{CHATWOOT_API_TOKEN[-5:]}" if CHATWOOT_API_TOKEN else None,
        "wuzapi_token_masked": f"{WUZAPI_API_TOKEN[:5]}...{WUZAPI_API_TOKEN[-5:]}" if WUZAPI_API_TOKEN else None
    }

@app.post("/test/chatwoot")
async def test_chatwoot():
    """Testa a conexão com Chatwoot"""
    results = {}
    
    # Teste 1: Listar contatos
    try:
        url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
        headers = {'api_access_token': CHATWOOT_API_TOKEN}
        response = requests.get(url, headers=headers, timeout=10)
        results["list_contacts"] = {
            "status": response.status_code,
            "success": response.status_code == 200,
            "message": "OK" if response.status_code == 200 else f"Erro {response.status_code}"
        }
    except Exception as e:
        results["list_contacts"] = {"error": str(e)}
    
    # Teste 2: Criar contato de teste
    try:
        contact_url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts"
        payload = {
            "inbox_id": CHATWOOT_INBOX_ID,
            "name": "Teste EasyPanel",
            "phone_number": "+5511999999999"
        }
        response
