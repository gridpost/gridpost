# GridPost — Backend de Pagamentos (deploy)

Backend que cuida **só do pagamento**. A publicação do mosaico continua no Make.

## O que cada arquivo é
- `main.py` — o backend (FastAPI)
- `requirements.txt` — dependências
- `schema.sql` — a tabela do Supabase, alinhada ao backend

---

## 1. Banco (Supabase)
1. Abra o Supabase → **SQL Editor** → New query.
2. Cole o conteúdo de `schema.sql` e clique em **Run**.
   - ⚠️ Ele faz `drop table` da `gridpost_pagamentos`. Como a sua é de teste/vazia, tudo bem. Se tiver dados que quer manter, apague a linha do `drop` antes.
3. Pegue a **service_role key**: Project Settings → API → Project API keys → `service_role` (a marcada como *secret*).

## 2. Subir no Render (free)
1. Suba esta pasta num repositório no GitHub (pode ser privado).
2. No Render → **New → Web Service** → conecte o repositório.
3. Configurações:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
4. Em **Environment**, adicione as variáveis:

| Variável | Valor |
|---|---|
| `MP_ACCESS_TOKEN` | Seu Access Token de produção (APP_USR-...) — **gere um novo**, o antigo vazou |
| `SUPABASE_URL` | `https://dmadzxqnotqcfkdxkpzp.supabase.co` |
| `SUPABASE_SERVICE_KEY` | a service_role key (secreta) |
| `BASE_URL` | a URL que o Render te der (ex: `https://gridpost-pagamentos.onrender.com`) |
| `UPLOAD_URL` | a URL da sua página de upload (ex: `https://gridpost.github.io/gridpost/upload.html`) |
| `ALLOWED_ORIGINS` | `https://gridpost.github.io` |
| `PRECO` | `14.99` |
| `MP_WEBHOOK_SECRET` | deixe vazio por enquanto (veja passo 4) |

5. Deploy. Quando subir, teste no navegador: `BASE_URL/ping` deve responder `{"status":"ok"}`.

## 3. Manter acordado (UptimeRobot)
- Crie um monitor HTTP apontando para `BASE_URL/ping`, intervalo de 5 min.
- (O Render free dorme após 15 min sem tráfego; o ping evita isso.)

## 4. Webhook seguro (faça depois que o básico funcionar)
1. No Mercado Pago → Suas integrações → seu app → **Webhooks → Configurar notificações**.
2. Modo **Produção**, URL: `BASE_URL/webhook`, evento **Pagamentos**. Salve.
3. Copie a **chave secreta** gerada e coloque em `MP_WEBHOOK_SECRET` no Render. Redeploy.
   - Enquanto estiver vazia, o backend aceita o webhook sem validar assinatura (ok só pra teste).
   - **Observação:** o `notification_url` que o backend já manda na criação do pagamento tem prioridade, então o webhook funciona mesmo antes deste passo. Este passo serve para a validação de origem (segurança).

## 5. Ligar no frontend
No botão "fazer pagamento" do site, em vez do link fixo, chame o backend:

```js
const r = await fetch("BASE_URL/criar-pagamento", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ nome: nomeDoCliente, email: emailDoCliente })
});
const { init_point, ref } = await r.json();
localStorage.setItem("gridpost_ref", ref);   // guarda pra checar depois
window.location.href = init_point;            // manda pro checkout
```

Na **página de upload**, antes de liberar o envio da imagem, confirme o pagamento:

```js
const ref = new URLSearchParams(location.search).get("ref");
const r = await fetch(`BASE_URL/status/${ref}`);
const { pago } = await r.json();
if (!pago) {
  // bloqueia o upload / mostra "pagamento não confirmado"
}
```

---

## Endpoints
| Método | Rota | Pra quê |
|---|---|---|
| GET | `/` ou `/ping` | healthcheck (UptimeRobot) |
| POST | `/criar-pagamento` | cria a cobrança, devolve `init_point` + `ref` |
| POST | `/webhook` | recebe a notificação do MP, grava pagamento aprovado |
| GET | `/status/{ref}` | a página de upload checa se pagou |

## Por que isso é melhor que o Make pro pagamento
- O Checkout Pro aceita **Pix, cartão, boleto** e funciona em **app ou navegador** — tudo num link só.
- O webhook é **validado por assinatura** (ninguém forja pagamento).
- A tabela fica **trancada** (service_role só no backend; anon não grava nada).
- Sem malabarismo de filtro: o backend já sabe separar pagamento de merchant_order.
