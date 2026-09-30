# D.Lourenço Barbearia – site + ERP
    pip install -r requirements.txt
    cp .env.example .env   # preencha e exporte as variáveis (ex.: `set -a; . ./.env; set +a`)
    gunicorn -w 1 app:app  # 1 worker: o agendador de lembretes roda dentro do processo
Site: `/` · ERP: `/admin` (login básico ADMIN_USER/ADMIN_PASS; use HTTPS em produção).
Sem `WA_TOKEN`/`GOOGLE_SA_FILE` o sistema roda em modo simulado (WhatsApp vai para o log; sem evento no Google Agenda).
**Google Agenda:** crie uma conta de serviço, compartilhe a agenda de cada barbeiro com o e-mail dela e grave o ID da agenda em `barbers.calendar_id`.
**WhatsApp (custo zero):** sem `WA_TOKEN` os lembretes aparecem na aba *Lembretes* do `/admin` com link `wa.me` (um clique abre o WhatsApp Business com a mensagem pronta). Com `WA_TOKEN` o envio é automático. Detalhes da API: a API da Meta só permite texto livre dentro de 24h da última mensagem do cliente; para lembretes fora da janela cadastre *templates* aprovados e troque o payload em `send_whatsapp`.

## WhatsApp não oficial (Baileys) – custo zero, com risco
    cd wa-gateway && npm install && WA_GATEWAY_SECRET=... node index.js   # escaneie o QR com um chip/número dedicado
Depois defina `WA_GATEWAY_URL` e `WA_GATEWAY_SECRET` no Flask. Se o gateway cair, os lembretes permanecem na aba *Lembretes* do `/admin`.
Riscos: viola os termos do WhatsApp (o número pode ser banido). Use um número dedicado, não o principal da barbearia; envie só a clientes que agendaram; mantenha baixo volume.
Hospedagem: o gateway precisa ficar ligado 24h com disco persistente (`./auth`). Planos gratuitos que "dormem" não servem; use um PC/Raspberry na barbearia ou uma VPS gratuita (ex.: Oracle Always Free).

## Modo demonstração
`WA_DEMO=1` → aba "WhatsApp (demo)" no `/admin`: botão 1 cria dados de exemplo, botão 2 dispara os lembretes e as mensagens aparecem em um chat simulado.
