"""
GridPost - Backend de Pagamentos
=================================
Responsabilidade ÚNICA: criar a cobrança no Mercado Pago (Checkout Pro) e
confirmar o pagamento via webhook de forma segura. A publicação do mosaico
continua no Make (não é tocada aqui).

Fluxo:
  1. Frontend chama  POST /criar-pagamento  -> recebe o link (init_point) e redireciona o cliente
  2. Cliente paga (Pix, cartão, boleto, app ou navegador - o Checkout Pro resolve tudo)
  3. Mercado Pago chama  POST /webhook  -> validamos assinatura, buscamos o pagamento,
     e se status == approved, gravamos no Supabase
  4. Cliente é redirecionado para a página de upload (back_urls.success)
  5. A página de upload consulta  GET /status/{ref}  e só libera o upload se estiver "approved"

Variáveis de ambiente necessárias (configurar no Render):
  MP_ACCESS_TOKEN        Access Token de PRODUÇÃO do Mercado Pago (APP_USR-...)
  MP_WEBHOOK_SECRET      Chave secreta do webhook (Suas integrações > Webhooks). Opcional no começo.
  SUPABASE_URL           https://dmadzxqnotqcfkdxkpzp.supabase.co
  SUPABASE_SERVICE_KEY   service_role key do Supabase (SECRETA - só aqui no backend)
  BASE_URL               URL pública deste backend no Render (ex: https://gridpost-pagamentos.onrender.com)
  UPLOAD_URL             URL da página de upload (ex: https://gridpost.github.io/gridpost/upload.html)
  PRECO                  Preço em reais (default 14.99)
  ALLOWED_ORIGINS        Origens liberadas no CORS, separadas por vírgula (ex: https://gridpost.github.io)
"""

import os
import hmac
import hashlib
import uuid
import logging
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gridpost")

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")  # vazio = pula validação (use só pra testar)
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
UPLOAD_URL = os.environ["UPLOAD_URL"]
PRECO = float(os.environ.get("PRECO", "14.99"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
MAKE_PUBLISH_URL = os.environ.get("MAKE_PUBLISH_URL", "")
MAKE_PUBLISH_SECRET = os.environ.get("MAKE_PUBLISH_SECRET", "")

MP_API = "https://api.mercadopago.com"
SUPA_TABLE = f"{SUPABASE_URL}/rest/v1/gridpost_pagamentos"

app = FastAPI(title="GridPost Pagamentos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Healthcheck (UptimeRobot pinga aqui pra manter o Render acordado)
# ---------------------------------------------------------------------------
@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "service": "gridpost-pagamentos"}


# ---------------------------------------------------------------------------
# 1) Criar pagamento -> devolve o link do Checkout Pro
# ---------------------------------------------------------------------------
class CriarPagamentoIn(BaseModel):
    nome: str | None = None
    email: str | None = None


class PublicarIn(BaseModel):
    ref: str
    cliente_nome: str | None = None
    cliente_ig: str | None = None
    cliente_email: str | None = None
    total_partes: int | None = None
    posts: dict[str, str] = {}


@app.post("/criar-pagamento")
async def criar_pagamento(dados: CriarPagamentoIn):
    # nosso identificador único do pedido (rastreia o cliente do início ao fim)
    ref = uuid.uuid4().hex

    preference = {
        "items": [{
            "title": "GridPost - Mosaico Instagram",
            "quantity": 1,
            "unit_price": PRECO,
            "currency_id": "BRL",
        }],
        "external_reference": ref,
        "notification_url": f"{BASE_URL}/webhook",
        "back_urls": {
            "success": f"{UPLOAD_URL}?ref={ref}",
            "pending": f"{UPLOAD_URL}?ref={ref}",
            "failure": f"{UPLOAD_URL}?ref={ref}&erro=1",
        },
        "auto_return": "approved",
        "statement_descriptor": "GRIDPOST",
    }
    if dados.email:
        preference["payer"] = {"email": dados.email}
        if dados.nome:
            preference["payer"]["name"] = dados.nome

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{MP_API}/checkout/preferences",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            json=preference,
        )
    if r.status_code not in (200, 201):
        log.error("Erro ao criar preference: %s %s", r.status_code, r.text)
        raise HTTPException(502, "Falha ao criar pagamento no Mercado Pago")

    pref = r.json()
    # registra o pedido como 'pending' já na criação
    await _upsert_supabase({
        "external_reference": ref,
        "status": "pending",
        "valor": PRECO,
    })

    return {
        "ref": ref,
        "init_point": pref["init_point"],          # link de produção
        "preference_id": pref["id"],
    }


# ---------------------------------------------------------------------------
# 2) Webhook do Mercado Pago
# ---------------------------------------------------------------------------
def _assinatura_valida(request: Request, data_id: str) -> bool:
    """Valida o header x-signature conforme a especificação do Mercado Pago."""
    if not MP_WEBHOOK_SECRET:
        return True  # validação desligada (apenas para testes iniciais)

    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")
    ts, v1 = "", ""
    for parte in x_signature.split(","):
        if "=" in parte:
            k, _, val = parte.partition("=")
            k, val = k.strip(), val.strip()
            if k == "ts":
                ts = val
            elif k == "v1":
                v1 = val
    if not (ts and v1):
        return False

    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    esperado = hmac.new(
        MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(esperado, v1)


@app.post("/webhook")
async def webhook(request: Request):
    # data.id pode vir na query (?data.id=...) e/ou no corpo
    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    tipo = request.query_params.get("type") or request.query_params.get("topic")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not data_id:
        data_id = str((body.get("data") or {}).get("id") or "")
    if not tipo:
        tipo = body.get("type") or body.get("topic") or ""

    # só nos interessa pagamento; merchant_order e afins são ignorados (200 rápido)
    if tipo != "payment" or not data_id:
        return {"ignored": True}

    if not _assinatura_valida(request, data_id):
        log.warning("Assinatura invalida para data.id=%s", data_id)
        raise HTTPException(401, "assinatura invalida")

    # busca o pagamento real na API do MP
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{MP_API}/v1/payments/{data_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        )
    if r.status_code != 200:
        log.error("Erro ao buscar pagamento %s: %s", data_id, r.status_code)
        raise HTTPException(502, "falha ao consultar pagamento")  # MP vai reenviar

    p = r.json()
    registro = {
        "external_reference": p.get("external_reference"),
        "mp_payment_id": str(p.get("id")),
        "status": p.get("status"),
        "valor": p.get("transaction_amount"),
        "metodo": p.get("payment_method_id"),
        "payer_email": (p.get("payer") or {}).get("email"),
        "paid_at": p.get("date_approved"),
    }
    if not registro["external_reference"]:
        # pagamento sem nossa referência (ex: link antigo) - grava mesmo assim pelo mp_payment_id
        registro["external_reference"] = f"mp_{registro['mp_payment_id']}"

    await _upsert_supabase(registro)
    log.info("Pagamento %s gravado: status=%s", data_id, registro["status"])
    return {"ok": True, "status": registro["status"]}


# ---------------------------------------------------------------------------
# 3) Status do pedido (a página de upload consulta antes de liberar)
# ---------------------------------------------------------------------------
@app.get("/status/{ref}")
async def status(ref: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SUPA_TABLE}?external_reference=eq.{ref}&select=status,valor,paid_at",
            headers=_supa_headers(),
        )
    linhas = r.json() if r.status_code == 200 else []
    if not linhas:
        return {"ref": ref, "status": "not_found", "pago": False}
    reg = linhas[0]
    return {"ref": ref, "status": reg["status"], "pago": reg["status"] == "approved"}


# ---------------------------------------------------------------------------
# 4) Publicar mosaico (só libera se pagamento aprovado e não publicado ainda)
# ---------------------------------------------------------------------------
@app.post("/publicar")
async def publicar(dados: PublicarIn):
    # 1. Busca o pedido no Supabase
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SUPA_TABLE}?external_reference=eq.{dados.ref}&select=status,publicado",
            headers=_supa_headers(),
        )
    linhas = r.json() if r.status_code == 200 else []
    if not linhas:
        raise HTTPException(404, "pedido nao encontrado")
    reg = linhas[0]

    # 2. Só publica se estiver pago
    if reg.get("status") != "approved":
        raise HTTPException(403, "pagamento nao aprovado")

    # 3. Evita publicar duas vezes com o mesmo pagamento
    if reg.get("publicado"):
        raise HTTPException(409, "pedido ja publicado")

    # 4. Marca como publicado
    async with httpx.AsyncClient(timeout=10) as client:
        await client.patch(
            f"{SUPA_TABLE}?external_reference=eq.{dados.ref}",
            headers={**_supa_headers(), "Prefer": "return=minimal"},
            json={"publicado": True},
        )

    # 5. Monta o payload e manda pro Make (com a senha)
    payload = {
        "cliente_nome": dados.cliente_nome or "",
        "cliente_ig": dados.cliente_ig or "",
        "cliente_email": dados.cliente_email or "",
        "total_partes": dados.total_partes,
        "secret": MAKE_PUBLISH_SECRET,
        **dados.posts,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(MAKE_PUBLISH_URL, json=payload)
    if resp.status_code not in (200, 201, 204):
        raise HTTPException(502, "falha ao enviar para publicacao")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers Supabase
# ---------------------------------------------------------------------------
def _supa_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


async def _upsert_supabase(registro: dict):
    """Insere ou atualiza o pedido pela external_reference (upsert)."""
    registro = {k: v for k, v in registro.items() if v is not None}
    headers = _supa_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{SUPA_TABLE}?on_conflict=external_reference",
            headers=headers,
            json=registro,
        )
    if r.status_code not in (200, 201, 204):
        log.error("Erro Supabase upsert: %s %s", r.status_code, r.text)
        raise HTTPException(502, "falha ao gravar no banco")
