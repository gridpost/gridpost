-- ===========================================================================
-- GridPost - tabela de pagamentos
-- Rode isto no Supabase: SQL Editor > New query > Run
-- ===========================================================================

-- Se a tabela antiga de teste estiver vazia, pode recriar sem dó:
drop table if exists public.gridpost_pagamentos;

create table public.gridpost_pagamentos (
    id                  uuid primary key default gen_random_uuid(),
    external_reference  text unique not null,        -- nosso id do pedido (liga frontend -> pagamento -> upload)
    mp_payment_id       text,                         -- id do pagamento no Mercado Pago
    status              text not null default 'pending', -- pending | approved | rejected | cancelled | ...
    valor               numeric(10,2),
    metodo              text,                          -- pix | credit_card | bolbradesco | ...
    payer_email         text,
    paid_at             timestamptz,                   -- quando foi aprovado
    created_at          timestamptz not null default now()
);

create index on public.gridpost_pagamentos (status);
create index on public.gridpost_pagamentos (mp_payment_id);

-- ---------------------------------------------------------------------------
-- RLS: mantém a tabela TRANCADA.
-- O backend usa a service_role key, que IGNORA o RLS por padrão.
-- Assim ninguém com a anon key (exposta no frontend) consegue ler/gravar pagamentos.
-- ---------------------------------------------------------------------------
alter table public.gridpost_pagamentos enable row level security;
-- (nenhuma policy para anon = anon não acessa nada; só a service_role passa)
