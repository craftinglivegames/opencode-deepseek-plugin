import { Plugin } from "@opencode/plugin"
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const here = path.dirname(fileURLToPath(import.meta.url))
const PORT = 8777
const BASE = `http://127.0.0.1:${PORT}`

const BRIDGE_SCRIPT = path.join(here, "bridge", "deepseek_bridge.py")

const NEED_LOGIN =
  "ТРЕБУЕТСЯ ВХОД В DEEPSEEK.\n" +
  "Это не ошибка: просто аккаунт DeepSeek ещё не авторизован в браузере моста. " +
  "ПРАВИЛО ПОВЕДЕНИЯ: немедленно прекрати любую генерацию и коротко спроси пользователя: " +
  "«Нужен вход в DeepSeek — пришли, пожалуйста, логин (email) и пароль от аккаунта». " +
  "Дождись ответа пользователя и вызови инструмент deepseek_login с присланными email и password. " +
  "Ничего не придумывай и не генерируй «от себя», пока вход не выполнен."

const LOGIN_MANUAL =
  "Открыл видимое окно браузера со страницей входа DeepSeek.\n" +
  "Попроси пользователя войти в аккаунт вручную в этом окне, затем вызови deepseek_login с параметром confirm=true, " +
  "чтобы проверить, что вход выполнен."

interface JObj {
  [k: string]: unknown
}

async function jfetch(
  url: string,
  init?: RequestInit,
  timeoutMs = 20000,
  signal?: AbortSignal,
): Promise<JObj> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener("abort", onAbort, { once: true })
  }
  try {
    const res = await fetch(url, { ...init, signal: controller.signal })
    const text = await res.text()
    try {
      return JSON.parse(text) as JObj
    } catch {
      return { ok: false, error: `не-JSON ответ (${res.status}): ${text.slice(0, 300)}` }
    }
  } finally {
    clearTimeout(timer)
    if (signal) signal.removeEventListener("abort", onAbort)
  }
}

function bridgeAlive(timeoutMs = 3000): Promise<boolean> {
  return jfetch(`${BASE}/health`, undefined, timeoutMs)
    .then((r) => r.ok === true)
    .catch(() => false)
}

function pythonCandidates(): string[] {
  const out: string[] = []
  const own = path.join(here, "bridge", "venv", "Scripts", "python.exe")
  if (existsSync(own)) out.push(own)
  if (process.env.LOCALAPPDATA) {
    const wordVenv = path.join(
      process.env.LOCALAPPDATA,
      "WordAIAssistant",
      "bridge",
      "venv",
      "Scripts",
      "python.exe",
    )
    if (existsSync(wordVenv)) out.push(wordVenv)
  }
  out.push("python")
  return out
}

async function spawnBridge(): Promise<void> {
  const started = pythonCandidates().map((py) => {
    try {
      const child = spawn(py, [BRIDGE_SCRIPT], {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
      })
      child.unref()
      return true
    } catch {
      return false
    }
  })
  if (!started.some(Boolean)) throw new Error("не удалось запустить python для моста")
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 1000))
    if (await bridgeAlive()) return
  }
  throw new Error(
    "мост DeepSeek не поднялся за 40 секунд. Запусти bridge/install.ps1 из папки плагина " +
      "и посмотри bridge/bridge.log",
  )
}

async function ensureBridge(): Promise<void> {
  if (await bridgeAlive()) return
  await spawnBridge()
}

interface Status {
  running: boolean
  logged_in: boolean
  busy?: boolean
  page?: string
  error?: string
}

async function getStatus(): Promise<Status> {
  const s = await jfetch(`${BASE}/status`, undefined, 20000)
  return {
    running: s.running === true,
    logged_in: s.logged_in === true,
    busy: s.busy === true,
    page: typeof s.page === "string" ? s.page : undefined,
    error: typeof s.error === "string" ? s.error : undefined,
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

interface ChatParams {
  prompt: string
  thinking?: boolean
  search?: boolean
  new_chat?: boolean
  timeout?: number
  images?: string[]
}

/** Убирает «мусорные» вступления ответа («Конечно, вот ваш реферат…»). */
function cleanAnswer(text: string): string {
  const lines = text.split(/\r?\n/)
  if (lines.length > 0) {
    const first = lines[0].trim()
    if (
      first.length < 140 &&
      /^(конечно[!.,\s]|вот (ваш|твой|готовый|развернутый)\b|здравствуйте[!.,]?\s*вот\b)/i.test(first)
    ) {
      lines.shift()
      while (lines.length && !lines[0].trim()) lines.shift()
    }
  }
  return lines.join("\n").trim()
}

async function sendChat(params: ChatParams, signal?: AbortSignal): Promise<string> {
  let lastErr = "unknown"
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await sleep(12000)
    const r = await jfetch(
      `${BASE}/chat`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: params.prompt,
          images: params.images ?? [],
          thinking: params.thinking === true,
          search: params.search === true,
          new_chat: params.new_chat !== false,
          timeout: params.timeout ?? null,
        }),
      },
      30 * 60 * 1000,
      signal,
    )
    if (r.ok === true && typeof r.text === "string") {
      const out = cleanAnswer(r.text)
      if (out) return out
      lastErr = "empty_response"
      continue
    }
    if (r.error === "not_logged_in") return NEED_LOGIN
    lastErr = String(r.error ?? r.message ?? "unknown error")
    if (r.error !== "busy") break
  }
  throw new Error(`DeepSeek не ответил: ${lastErr}`)
}

type Schema = Record<string, unknown>

const schema = (properties: Schema, required: string[] = []): Schema => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
})

const str = (description: string): Schema => ({ type: "string", description })
const bool = (description: string): Schema => ({ type: "boolean", description })
const num = (description: string): Schema => ({ type: "number", description })

export default Plugin.define({
  id: "deepseek",
  async setup(ctx) {
    await ctx.session.hook("context", (event) => {
      event.system.push({
        type: "text",
        text:
          "DeepSeek-инструменты (deepseek_ask, deepseek_rewrite): промпт в chat.deepseek.com посылается как обычное " +
          "сообщение живого человека — никогда не пиши в нём, что ты ИИ, агент, ассистент или модель, и не упоминай " +
          "OpenCode/плагины. Если инструмент вернул «ТРЕБУЕТСЯ ВХОД В DEEPSEEK» — сразу остановись, кратко спроси " +
          "у пользователя логин и пароль от DeepSeek и вызови deepseek_login с ними; до входа ничего не генерируй.",
      })
    })

    await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "deepseek",
        description:
          "Генерация текстов через веб-чат DeepSeek (chat.deepseek.com): рефераты, курсовые, главы, планы, " +
          "переписывание и сокращение фрагментов. Работает как обычный пользовательский чат — без API-ключей.",
      })

      editor.add({
        name: "status",
        description:
          "Состояние моста DeepSeek: запущен ли он и выполнен ли вход в аккаунт. Проверяй перед первой генерацией " +
          "или если сомневаешься, нужен ли вход.",
        input: schema({}),
        options: { codemode: true },
        execute: async () => {
          await ensureBridge()
          const s = await getStatus()
          return {
            content: JSON.stringify(
              {
                bridge_running: s.running,
                logged_in: s.logged_in,
                busy: s.busy === true,
                page: s.page ?? null,
                error: s.error ?? null,
                need_login: !s.logged_in,
              },
              null,
              2,
            ),
          }
        },
      })

      editor.add({
        name: "login",
        description:
          "Вход в аккаунт DeepSeek. Правила: (1) пароль НЕ сохраняется — после первого входа сессия живёт в профиле " +
          "браузера; (2) если инструмент вернул «ТРЕБУЕТСЯ ВХОД В DEEPSEEK», сначала спроси у пользователя логин " +
          "(email) и пароль и только потом вызывай этот инструмент с email/password из его ответа; (3) вызов без " +
          "аргументов открывает видимое окно браузера для ручного входа пользователем — после чего вызови инструмент " +
          "снова с confirm=true; (4) confirm=true только проверяет, что вход уже выполнен.",
        input: schema({
          email: str("Email (логин) от аккаунта DeepSeek. Получи его у пользователя."),
          password: str("Пароль от аккаунта DeepSeek. Получи его у пользователя."),
          confirm: bool("true — только проверить, что вход уже выполнен, без ввода данных."),
          visible: bool("true — открыть видимое окно браузера вместо фонового (для ручного входа)."),
        }),
        options: { codemode: true },
        execute: async (input) => {
          const i = input as {
            email?: string
            password?: string
            confirm?: boolean
            visible?: boolean
          }
          await ensureBridge()
          if (i.confirm) {
            const r = await jfetch(
              `${BASE}/login`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirm: true }),
              },
              60000,
            )
            if (r.logged_in === true) return { content: "Вход в DeepSeek выполнен." }
            return { content: NEED_LOGIN }
          }
          if (!i.email || !i.password) {
            await jfetch(`${BASE}/open_login`, { method: "POST" }, 60000)
            return { content: LOGIN_MANUAL }
          }
          const r = await jfetch(
            `${BASE}/login`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ email: i.email, password: i.password, visible: i.visible === true }),
            },
            180000,
          )
          if (r.logged_in === true) return { content: "Вход в DeepSeek выполнен." }
          return {
            content:
              "Вход не выполнен: " +
              String(r.error ?? "неизвестная ошибка") +
              "\nПопроси пользователя проверить логин/пароль либо вызови deepseek_login без аргументов " +
              "для ручного входа в видимом окне браузера.",
          }
        },
      })

      editor.add({
        name: "ask",
        description:
          "Отправляет запрос в веб-чат DeepSeek и возвращает готовый текст: главы реферата, курсовой, планы, " +
          "списки литературы, варианты формулировок и т.п. Поле prompt — это то, что увидит человек в чате: напиши " +
          "полный самостоятельный запрос от первого лица (тема, объём, структура, требования к оформлению), как " +
          "пользователь пишет в чате. ЗАПРЕЩЕНО упоминать, что ты ИИ/агент/ассистент/модель, и называть OpenCode или " +
          "плагины. Проси чистый текст без вступлений вида «Конечно, вот…». Если вернулось «ТРЕБУЕТСЯ ВХОД В " +
          "DEEPSEEK» — остановись и спроси у пользователя логин/пароль (см. deepseek_login).",
        input: schema({
          prompt: str("Запрос в чат DeepSeek, написанный как обычное сообщение живого человека."),
          thinking: bool("Включить режим глубокого размышления (DeepThink). По умолчанию false."),
          search: bool("Включить поиск в интернете у DeepSeek. По умолчанию false."),
          new_chat: bool("Начать новый диалог DeepSeek (true, по умолчанию) или продолжить текущий (false)."),
          timeout: num("Тайм-аут ожидания ответа в секундах (по умолчанию 1500)."),
          images: {
            type: "array",
            items: { type: "string" },
            description: "Пути к картинкам (например, скрин задания), которые нужно приложить к запросу.",
          },
        }, ["prompt"]),
        options: { codemode: true },
        execute: async (input, context) => {
          const i = input as {
            prompt: string
            thinking?: boolean
            search?: boolean
            new_chat?: boolean
            timeout?: number
            images?: string[]
          }
          await ensureBridge()
          const s = await getStatus()
          if (!s.logged_in) return { content: NEED_LOGIN }
          const text = await sendChat(
            {
              prompt: i.prompt,
              thinking: i.thinking,
              search: i.search,
              new_chat: i.new_chat,
              timeout: i.timeout,
              images: i.images,
            },
            context.signal,
          )
          return { content: text }
        },
      })

      editor.add({
        name: "rewrite",
        description:
          "Перегенерировать готовый фрагмент текста по инструкции (переписать иначе, сократить, расширить, " +
          "сделать проще/научнее, изменить стиль). Отправляет фрагмент и требование в тот же диалог DeepSeek, " +
          "поэтому переписанное согласуется с предыдущей генерацией (new_chat=false по умолчанию). Возвращает " +
          "только новый текст фрагмента. При «ТРЕБУЕТСЯ ВХОД В DEEPSEEK» — остановись и спроси логин/пароль у " +
          "пользователя.",
        input: schema({
          text: str("Текст фрагмента, который нужно переписать."),
          instruction: str("Что сделать: например «перепиши другими словами без потери смысла», «сократи вдвое», «добавь примеры»."),
          thinking: bool("Включить режим глубокого размышления. По умолчанию true."),
          new_chat: bool("Начать новый диалог вместо продолжения текущего. По умолчанию false."),
          timeout: num("Тайм-аут ожидания ответа в секундах (по умолчанию 1500)."),
        }, ["text", "instruction"]),
        options: { codemode: true },
        execute: async (input, context) => {
          const i = input as {
            text: string
            instruction: string
            thinking?: boolean
            new_chat?: boolean
            timeout?: number
          }
          await ensureBridge()
          const s = await getStatus()
          if (!s.logged_in) return { content: NEED_LOGIN }
          const prompt =
            `${i.instruction.trim()}\n\n` +
            `Текст фрагмента:\n"""\n${i.text}\n"""\n\n` +
            "Верни только готовый результат: без предисловий, пояснений и фраз вроде «Конечно, вот…»."
          const text = await sendChat(
            {
              prompt,
              thinking: i.thinking !== false,
              new_chat: i.new_chat === true,
              timeout: i.timeout,
            },
            context.signal,
          )
          return { content: text }
        },
      })
    })

    return () => {
      /* registrations are disposed with the plugin */
    }
  },
})
