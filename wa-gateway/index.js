// Gateway local (NÃO OFICIAL) – Baileys. Escuta só em 127.0.0.1; o Flask chama POST /send.
import http from 'node:http'
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
import qrcode from 'qrcode-terminal'
import pino from 'pino'

const PORT = +process.env.WA_GATEWAY_PORT || 3001
const SECRET = process.env.WA_GATEWAY_SECRET
if (!SECRET) { console.error('Defina WA_GATEWAY_SECRET'); process.exit(1) }

let sock, ready = false
const queue = []; let draining = false
const sleep = ms => new Promise(r => setTimeout(r, ms))

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState('./auth')   // sessão salva em ./auth (não versionar!)
  const { version } = await fetchLatestBaileysVersion()
  sock = makeWASocket({ version, auth: state, logger: pino({ level: 'silent' }), browser: ['Barbearia', 'Chrome', '1.0'] })
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) { console.log('Escaneie no WhatsApp > Aparelhos conectados:'); qrcode.generate(qr, { small: true }) }
    if (connection === 'open') { ready = true; console.log('WhatsApp conectado') }
    if (connection === 'close') {
      ready = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) console.error('Sessão encerrada. Apague ./auth e reinicie para novo QR.')
      else setTimeout(connect, 5000)
    }
  })
}

// Fila com intervalo aleatório (3–8 s) entre mensagens: reduz risco de bloqueio por comportamento de robô
async function drain() {
  if (draining) return; draining = true
  while (queue.length) {
    const { to, text, resolve } = queue.shift()
    try {
      const [res] = await sock.onWhatsApp(to)            // resolve o JID correto (inclui o 9º dígito BR)
      if (!res?.exists) { resolve({ ok: false, error: 'número sem WhatsApp' }) }
      else { await sock.sendMessage(res.jid, { text }); resolve({ ok: true }) }
    } catch (e) { resolve({ ok: false, error: String(e.message || e) }) }
    await sleep(3000 + Math.random() * 5000)
  }
  draining = false
}

http.createServer((req, res) => {
  const send = (code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)) }
  if (req.headers['x-secret'] !== SECRET) return send(401, { error: 'unauthorized' })
  if (req.method === 'GET' && req.url === '/status') return send(200, { ready })
  if (req.method === 'POST' && req.url === '/send') {
    let raw = ''; req.on('data', c => { raw += c; if (raw.length > 10000) req.destroy() })
    req.on('end', async () => {
      let j; try { j = JSON.parse(raw) } catch { return send(400, { error: 'json inválido' }) }
      if (!ready) return send(503, { error: 'WhatsApp desconectado' })
      if (!/^\d{12,13}$/.test(j.to || '') || !j.text) return send(400, { error: 'dados inválidos' })
      const r = await new Promise(resolve => { queue.push({ to: j.to, text: String(j.text).slice(0, 1000), resolve }); drain() })
      send(r.ok ? 200 : 502, r)
    })
    return
  }
  send(404, { error: 'not found' })
}).listen(PORT, '127.0.0.1', () => console.log('Gateway em 127.0.0.1:' + PORT))

connect()
