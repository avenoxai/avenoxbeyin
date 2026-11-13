/**
 * beyin-bridge — DeepSeek Harness icin istege bagli kopru.
 *
 * 1) Oturum basinda `beyin.py recap --days N` calistirip son kayitlari baglama enjekte eder.
 * 2) Tur sonuc/karar ile bittiginde kullaniciya "Bunu beyne kaydedeyim mi?" diye sorar.
 *
 * DSH'de shell hook yoktur; plugin `session/event` akisini dinler ve tur sonunda
 * `agent.followup()` ile ajani uyandirir. Dosya okumaz/yazmaz; yalnizca kendi tanı
 * kaydini `~/.dsh/beyin-bridge.log` dosyasina yazar.
 *
 * Yapilandirma:
 *   BEYIN_VAULT=<vault yolu>            (ortam degiskeni)
 *   veya ~/.dsh/beyin-bridge.json       {"vault": "<vault yolu>"}
 *   BEYIN_PYTHON=<yorumlayici>          (varsayilan: python3; Windows'ta py -3)
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const name = 'beyin-bridge'
export const inject = ['agents']

const DSH_HOME = path.join(os.homedir(), '.dsh')
const LOG = path.join(DSH_HOME, 'beyin-bridge.log')
const CONFIG = path.join(DSH_HOME, 'beyin-bridge.json')

const ASK_COOLDOWN = 10 * 60 * 1000
const DECLINE_COOLDOWN = 30 * 60 * 1000
const MAX_ASKS = 4
const MIN_TOOL_CALLS = 1
const DIGEST_DAYS = 7
const DIGEST_ITEMS = 5

const CLOSING = /(^|[\s.!])(tamam|tamamdır|tamamdir|teşekkürler|tesekkurler|teşekkür|tesekkur|sağol|sagol|harika|süper|super|mükemmel|mukemmel|oldu|eyvallah|bitir|kapat|yeter)([\s.!]|$)/i
const DECISION = /(karar|sonuç|sonuc|özet|ozet|bitti|tamamland|hazır|hazir|uygulandı|uygulandi|doğrulandı|dogrulandi|test edildi|kaydedildi)/i
const DECLINE = /(hayır|hayir|kaydetme|gerek yok|sonra|şimdilik|simdilik)/i

const PROMPT = [
  '[beyin-bridge] Bu turda sonuç/karar içeren bir çalışma tamamlandı.',
  'Kullanıcıya YALNIZ tek satır sor: "Bunu beyne kaydedeyim mi?"',
  'Başka açıklama yazma, araç çağırma, iş yapma.',
].join(' ')

function log(line) {
  try {
    fs.appendFileSync(LOG, `${new Date().toISOString()} ${line}\n`)
  } catch {
    /* log yazılamazsa sessiz geç */
  }
}

function clip(text, limit) {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

function resolveVault() {
  const fromEnv = process.env.BEYIN_VAULT
  if (typeof fromEnv === 'string' && fromEnv.trim()) return fromEnv.trim()
  try {
    const config = JSON.parse(fs.readFileSync(CONFIG, 'utf8'))
    if (typeof config?.vault === 'string' && config.vault.trim()) return config.vault.trim()
  } catch {
    /* yapilandirma yoksa kurulum yok sayilir */
  }
  return null
}

function pythonCommand() {
  if (process.env.BEYIN_PYTHON) return [process.env.BEYIN_PYTHON, []]
  if (process.platform === 'win32') return ['py', ['-3']]
  return ['python3', []]
}

let createUserMessage = null
try {
  const llm = await import('@deepseek-ai/dsh-llm')
  createUserMessage = llm.createUserMessage ?? null
} catch {
  createUserMessage = null
}

function textOf(message) {
  const content = message?.content
  if (!Array.isArray(content)) return ''
  return content
    .filter((part) => part?.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text)
    .join('\n')
    .trim()
}

function makeMessage(text) {
  const payload = {
    content: [{ type: 'text', text }],
    source: { kind: 'plugin:beyin-bridge', form: 'instructions' },
  }
  if (createUserMessage) {
    try {
      return createUserMessage(payload)
    } catch {
      /* düz nesneye düş */
    }
  }
  return { role: 'user', ...payload }
}

export function apply(ctx) {
  if (globalThis.__beyinBridgeActive) {
    log('apply skipped (already active)')
    return
  }
  const vault = resolveVault()
  if (!vault) {
    log(`vault not configured (set BEYIN_VAULT or ${CONFIG})`)
    return
  }
  globalThis.__beyinBridgeActive = true
  try {
    install(ctx, vault)
  } catch (error) {
    globalThis.__beyinBridgeActive = false
    log(`apply failed: ${error?.message ?? error}`)
    console.error('[beyin-bridge] apply failed:', error)
  }
}

function install(ctx, vault) {
  const sessions = new Map()
  const timers = new Set()
  let disposed = false

  const stateOf = (agent) => {
    const id = agent?.session?.id
    if (id === undefined) return null
    let state = sessions.get(id)
    if (!state) {
      state = {
        agent,
        lastUser: '',
        lastAssistant: '',
        toolCalls: 0,
        asks: 0,
        cooldownUntil: 0,
        awaitingUser: false,
      }
      sessions.set(id, state)
    }
    state.agent = agent
    return state
  }

  const digest = (agent) => {
    const [command, prefix] = pythonCommand()
    execFile(
      command,
      [...prefix, path.join(vault, 'beyin.py'), '--vault', vault, 'recap', '--days', String(DIGEST_DAYS)],
      { timeout: 8000, maxBuffer: 1 << 20 },
      (error, stdout) => {
        if (error) {
          log(`digest failed: ${error.message}`)
          return
        }
        let data = null
        try {
          data = JSON.parse(stdout)
        } catch {
          log('digest parse failed')
          return
        }
        const items = Array.isArray(data?.items) ? data.items.slice(0, DIGEST_ITEMS) : []
        if (items.length === 0) {
          log('digest empty')
          return
        }
        const lines = items.map(
          (item) => `- ${String(item.created_at ?? '').slice(0, 10)} · ${clip(item.summary, 200)}`,
        )
        const text = [
          `[beyin] Son ${DIGEST_DAYS} günün kasa kayıtları (kaynak: receipts/):`,
          ...lines,
          'Bir işe başlarken ilgili kararı `context "<konu>" --project <proje>` ile doğrula; çelişki varsa kullanıcıya söyle.',
        ].join('\n')
        try {
          agent.inject(makeMessage(text))
          log(`digest injected items=${items.length} session=${agent?.session?.id?.slice(-8)}`)
        } catch (injectError) {
          log(`digest inject failed: ${injectError?.message ?? injectError}`)
        }
      },
    )
  }

  const subscribeAgent = (agent, { withDigest }) => {
    if (!agent || agent?.session?.header?.origin === 'subagent') return
    stateOf(agent)
    if (withDigest) digest(agent)
  }

  const disposeCreated = ctx.on('agent/created', ({ agent }) => subscribeAgent(agent, { withDigest: true }))
  const disposeLegacy = ctx.on('agent/session-start', ({ agent }) => subscribeAgent(agent, { withDigest: false }))

  const disposeDisposed = ctx.on('agent/disposed', ({ agent }) => {
    const id = agent?.session?.id
    if (id !== undefined) sessions.delete(id)
  })

  // Resume edilmis oturumlar plugin yuklenmeden once kurulmus olabilir.
  let seeded = 0
  try {
    const list = ctx.agents?.list?.() ?? []
    for (const entry of list) {
      const agent = entry?.agent ?? entry
      if (agent?.session?.id !== undefined && agent?.session?.header?.origin !== 'subagent') {
        stateOf(agent)
        seeded += 1
      }
    }
  } catch (error) {
    log(`seed failed: ${error?.message ?? error}`)
  }

  const ask = (state, reason) => {
    const now = Date.now()
    if (state.asks >= MAX_ASKS) {
      log(`ask skipped (max) session=${state.agent?.session?.id?.slice(-8)}`)
      return
    }
    if (now < state.cooldownUntil) {
      log(`ask skipped (cooldown) session=${state.agent?.session?.id?.slice(-8)}`)
      return
    }
    // turn/end islenirken oturum kaydi yaziliyor; followup ayni anda cagrilamaz.
    const timer = setTimeout(() => {
      timers.delete(timer)
      if (disposed) return
      try {
        state.agent.followup(makeMessage(PROMPT))
        state.asks += 1
        state.cooldownUntil = Date.now() + ASK_COOLDOWN
        state.awaitingUser = true
        log(`ask sent (${reason}) session=${state.agent?.session?.id?.slice(-8)}`)
      } catch (error) {
        log(`followup failed: ${error?.message ?? error}`)
        console.error('[beyin-bridge] followup failed:', error)
      }
    }, 120)
    timers.add(timer)
  }

  const disposeEvents = ctx.on('session/event', (session, event) => {
    const state = session?.id === undefined ? null : sessions.get(session.id)
    if (!state) return
    const type = event?.type

    if (type === 'user/message') {
      const text = textOf(event.data)
      if (!text) return
      state.lastUser = text
      state.toolCalls = 0
      if (state.awaitingUser) {
        state.awaitingUser = false
        if (DECLINE.test(text)) state.cooldownUntil = Date.now() + DECLINE_COOLDOWN
      }
      return
    }

    if (type === 'tool/call') {
      state.toolCalls += 1
      return
    }

    if (type === 'assistant/message') {
      const text = textOf(event.data?.message)
      if (text) state.lastAssistant = text
      return
    }

    if (type !== 'turn/end') return

    const closing = CLOSING.test(state.lastUser)
    const decisive = DECISION.test(state.lastAssistant) && state.toolCalls >= MIN_TOOL_CALLS
    log(`turn/end session=${session.id.slice(-8)} closing=${closing} decisive=${decisive} tools=${state.toolCalls}`)
    if (!closing && !decisive) return
    ask(state, closing ? 'closing' : 'decision')
  })

  ctx.effect?.(() => () => {
    disposed = true
    for (const timer of timers) clearTimeout(timer)
    timers.clear()
    disposeCreated?.()
    disposeLegacy?.()
    disposeDisposed?.()
    disposeEvents?.()
    sessions.clear()
    globalThis.__beyinBridgeActive = false
  })

  log(`active seeded=${seeded} vault=${vault}`)
  console.log(`[beyin-bridge] active (vault: ${vault})`)
}
