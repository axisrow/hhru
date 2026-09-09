// Локальный плагин opencode для проекта hhru (коммитится в репозиторий;
// автозагрузка из .opencode/plugins/ при старте opencode из корня проекта).
//
// Назначение: человек общается с агентом по-русски, агент управляет CLI
// hh.ru-бота через единственный санкционированный инструмент `hhru`.
// Правила безопасности повторяют культуру проекта (CLAUDE.md,
// commands/hhru.md, skills/hhru/SKILL.md):
//   - READ-команды свободны;
//   - WRITE-команды к hh.ru — сначала --dry-run (план показывается человеку),
//     боевой запуск только после явного «да» в чате (confirmed=true);
//   - незнакомая команда — отказ (fail-closed), никаких выдуманных флагов;
//   - WRITE-hh.ru через голый bash в обход инструмента блокируется хуком.
import { type Plugin, tool } from "@opencode-ai/plugin"
import { existsSync } from "node:fs"
import { join } from "node:path"

// ---------------------------------------------------------------------------
// Классификация команд (по `hhru --help`, commands/hhru.md и skills/hhru/)
// ---------------------------------------------------------------------------

/** Ничего не меняют ни на hh.ru, ни локально (или читают свои же данные). */
const READ_COMMANDS = new Set([
  "adaptive-report", "call-api", "census", "competitors", "diagnostics",
  "export-resume", "funnel", "learn", "list-resumes", "log", "market",
  "probe", "professional-roles", "query", "responses", "resume-views",
  "review", "robot-queue", "schedule", "search", "skipped", "stats",
  "uncertain", "whoami",
])

/** Меняют только локальные данные (конфиг, история, сессия, установка). */
const LOCAL_WRITE_COMMANDS = new Set([
  "account", "backup", "blacklist", "clear-skipped", "config",
  "import-cookies", "mark", "profile", "questionnaire", "refresh-token",
  "reject", "restore", "robot-mark", "settings", "update",
])

/** Меняют состояние аккаунта на hh.ru через браузер (видны работодателям
 *  или необратимы). Требуют confirmed=true; подмножество ниже — ещё и
 *  состоявшегося dry-run до боевого запуска. */
const HH_WRITE_COMMANDS = new Set([
  "about", "adaptive-resume", "apply", "bump", "calendar", "clear-negotiations",
  "common", "copy-resume", "create-resume", "delete-education-entry",
  "delete-photo", "delete-resume", "edit-education", "edit-experience",
  "edit-languages", "edit-skills", "fill-form", "import-resume",
  "publish-resume", "rename-resume", "reply-employers", "report-vacancy",
  "resume-pool", "resume-position", "resume-sections", "resume-visibility",
  "robot-reply", "run", "select-photo", "upload-photo", "wizard-next",
])

/** Команды из правил проекта «сначала --dry-run»: боевой вызов через
 *  инструмент отклоняется, пока для той же команды+резюме не выполнялся
 *  успешный dry-run (страж в памяти плагина, fail-closed). */
const DRY_RUN_FIRST_COMMANDS = new Set([
  "about", "apply", "bump", "clear-negotiations", "copy-resume",
  "edit-education", "edit-experience", "edit-skills", "import-resume",
  "publish-resume", "reply-employers", "resume-position", "resume-sections",
  "robot-reply", "run",
])

/** Требуют headed-браузер (человек видит окно и участвует). */
const HEADED_COMMANDS = new Set(["login", "login-code", "calendar"])

/** Интерактивный вход: без confirmed, но headed и длинный таймаут. */
const AUTH_COMMANDS = new Set(["login", "login-code"])

const GLOBAL_FLAGS_WITH_VALUE = new Set(["--account", "--config", "--history"])
const GLOBAL_BOOL_FLAGS = new Set(["--headless", "--verbose", "--quiet"])

type Kind = "read" | "local_write" | "hh_write" | "headed_auth" | "unknown"

function classify(command: string): Kind {
  if (AUTH_COMMANDS.has(command)) return "headed_auth"
  if (READ_COMMANDS.has(command)) return "read"
  if (LOCAL_WRITE_COMMANDS.has(command)) return "local_write"
  if (HH_WRITE_COMMANDS.has(command) || HEADED_COMMANDS.has(command)) return "hh_write"
  return "unknown"
}

// ---------------------------------------------------------------------------
// Разбор строки флагов (кавычки уважаются; shell-метасимволы не исполняются —
// аргументы передаются массивом, Bun shell их экранирует сам)
// ---------------------------------------------------------------------------

// Приватные (не-export) хелперы: opencode вызывает КАЖДЫЙ named export модуля
// как plugin-factory с контекстом — лишние экспорты ломают загрузку плагина.
function tokenize(input: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(input)) !== null) {
    out.push(m[1] ?? m[2] ?? m[3])
  }
  return out
}

/** Вынимает глобальные флаги hhru (до субкоманды) из произвольной позиции. */
function extractGlobals(tokens: string[]): { globals: string[]; rest: string[] } {
  const globals: string[] = []
  const rest: string[] = []
  for (let i = 0; i < tokens.length; i += 1) {
    const t = tokens[i]
    if (GLOBAL_FLAGS_WITH_VALUE.has(t) && i + 1 < tokens.length) {
      globals.push(t, tokens[i + 1])
      i += 1
    } else if (GLOBAL_BOOL_FLAGS.has(t)) {
      globals.push(t)
    } else {
      rest.push(t)
    }
  }
  return { globals, rest }
}

/** Ключ «команда + резюме» для стража dry-run-прежде-боя. */
function guardKey(account: string, command: string, rest: string[]): string {
  const idx = rest.indexOf("--resume")
  const resume = idx >= 0 && idx + 1 < rest.length ? rest[idx + 1] : "all"
  return `${account}:${command}:${resume}`
}

function findBinary(root: string): string {
  const venv = join(root, ".venv", "bin", "hhru")
  return existsSync(venv) ? venv : "hhru"
}

/** Первый не-флаговый токен после бинарника (субкоманда) — для bash-хука. */
function firstSubcommand(tokens: string[]): string | null {
  const { rest } = extractGlobals(tokens)
  for (const t of rest) {
    if (!t.startsWith("-")) return t
  }
  return null
}

/** `timeout` (coreutils) или `gtimeout` (Homebrew) по PATH; на стоковом
 *  macOS его нет — тогда команды запускаются без OS-лимита времени
 *  (graceful-деградация: защитные лимиты остаются на стороне самого CLI).
 *  Результат кэшируется на процесс. */
let timeoutBin: string | null | undefined

function findTimeoutBin(): string | null {
  if (timeoutBin === undefined) {
    timeoutBin = null
    // PATH сплитим и по ":", и по ";": на Windows разделитель ";", лишние
    // пустые сегменты от смешанного содержимого отсеиваются проверкой выше.
    for (const dir of (process.env.PATH ?? "").split(/[:;]/)) {
      if (!dir) continue
      for (const name of ["timeout", "gtimeout"]) {
        const candidate = join(dir, name)
        if (existsSync(candidate)) timeoutBin = candidate
      }
    }
  }
  return timeoutBin
}

function timeoutFor(kind: Kind, command: string, dryRun: boolean): number {
  if (kind === "headed_auth") return 400_000 // login: до 300 с + запас
  if (command === "update") return 900_000
  if (kind === "read") return 300_000
  if (dryRun) return 600_000
  if (command === "apply" || command === "run") return 2_400_000 // лимит 40 откликов, паузы 8-25 с
  return 900_000
}

const MAX_OUTPUT = 60_000

// ---------------------------------------------------------------------------
// Плагин
// ---------------------------------------------------------------------------

export const HhruPlugin: Plugin = async ({ directory, worktree, $ }) => {
  const root = worktree || directory
  // Состояние стража «dry-run прежде боя» — в памяти процесса opencode.
  // Перезапуск opencode очищает его: это безопасно (fail-closed — придётся
  // показать план заново), а не наоборот.
  const dryRunDone = new Set<string>()

  return {
    // Defence-in-depth: WRITE-hh.ru через голый bash без --dry-run запрещён.
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return
      const command = String((output.args as Record<string, unknown>).command ?? "")
      // Проверяем ВСЕ вхождения hhru/run.sh в строке (команда может быть
      // цепочкой: `hhru whoami; hhru bump ...`), а наличие `--dry-run`
      // определяем токенизацией сегмента — подстрока по строке целиком
      // отключала бы страж значением аргумента или эхом в echo.
      const re = /(?:\.venv\/bin\/hhru|scripts\/run\.sh|(?<![\w/.-])hhru)\s+([^\n]*)/g
      let m: RegExpExecArray | null
      while ((m = re.exec(command)) !== null) {
        const tokens = tokenize(m[1])
        const sub = firstSubcommand(tokens)
        if (!sub) continue
        const kind = classify(sub)
        const dangerous =
          (kind === "hh_write" || kind === "local_write") && !tokens.includes("--dry-run")
        if (dangerous) {
          throw new Error(
            `hhru: команда «${sub}» меняет данные и запрещена через голый bash без --dry-run. ` +
            "Используй инструмент hhru: сначала dry_run=true (покажи план человеку), " +
            "боевой запуск — только после явного согласия (confirmed=true).",
          )
        }
      }
    },

    tool: {
      hhru: tool({
        description:
          "Запуск CLI hh.ru-бота (проект hhru). Классы команд: READ (search, probe, stats, " +
          "query, responses, funnel, whoami, list-resumes, market, log, census, diagnostics, " +
          "skipped, uncertain, review, robot-queue, resume-views, schedule, learn, " +
          "professional-roles, competitors, adaptive-report, export-resume, call-api) — свободны. " +
          "WRITE-hh.ru (apply, bump, run, publish-resume, reply-employers, clear-negotiations, " +
          "copy-resume, delete-resume, create-resume, import-resume, edit-*, about, " +
          "resume-position, robot-reply, " +
          "resume-sections, wizard-next, фото, rename-resume, resume-pool, resume-visibility, " +
          "fill-form, report-vacancy, common) меняют аккаунт на hh.ru: сначала dry_run=true и " +
          "покажи план человеку; боевой запуск ТОЛЬКО после его явного «да» в чате — тогда " +
          "confirmed=true. Для apply/bump/run/publish-resume/reply-employers/clear-negotiations/" +
          "copy-resume/edit-*/about/resume-position/resume-sections боевой вызов отклонится, если " +
          "dry-run для той же команды+резюме не выполнялся. WRITE-local (mark, clear-skipped, " +
          "questionnaire, config, settings, profile, account, backup, restore, blacklist, reject, " +
          "robot-mark, " +
          "refresh-token, import-cookies, update) — тоже confirmed=true. login/login-code — " +
          "открывают окно входа на экране человека (headed), подтверждение не нужно. " +
          "Вывод — текст/ASCII-таблицы; не добавляй эмодзи в пересказ. " +
          "Глобальные флаги (--headless, --account) подставляются автоматически.",
        args: {
          command: tool.schema
            .string()
            .describe("Субкоманда hhru без флагов, например: search, apply, bump, list-resumes"),
          flags: tool.schema
            .string()
            .optional()
            .describe(
              "Флаги и аргументы после субкоманды одной строкой, например: " +
                "'--resume <id> --limit 5'. Значения с пробелами — в двойных кавычках.",
            ),
          account: tool.schema
            .string()
            .optional()
            .describe("Имя аккаунта (data/accounts/<name>/), по умолчанию 'default'"),
          dry_run: tool.schema
            .boolean()
            .optional()
            .describe("true = добавить --dry-run (план без изменений на hh.ru)"),
          confirmed: tool.schema
            .boolean()
            .optional()
            .describe(
              "true = человек ЯВНО подтвердил боевой/локальный WRITE в чате после показа плана. " +
                "Не ставь true без явного согласия человека.",
            ),
        },
        async execute(args) {
          const account = (args.account ?? "default").trim() || "default"
          const command = args.command.trim()
          const kind = classify(command)

          if (kind === "unknown") {
            throw new Error(
              `Неизвестная субкоманда «${command}». Список: ${[...READ_COMMANDS, ...LOCAL_WRITE_COMMANDS, ...HH_WRITE_COMMANDS, ...AUTH_COMMANDS].sort().join(", ")}. Не выдумывай команды и флаги.`,
            )
          }
          const dryRun = args.dry_run === true
          const confirmed = args.confirmed === true

          const rawTokens = tokenize(args.flags ?? "")
          const { globals, rest } = extractGlobals(rawTokens)

          if (command === "log" && (rest.includes("-f") || rest.includes("--follow"))) {
            throw new Error("log -f (follow) блокирует инструмент навсегда — запусти без -f.")
          }
          if ((kind === "hh_write" || kind === "local_write") && !confirmed) {
            throw new Error(
              `Команда «${command}» меняет данные. Порядок: 1) dry_run=true — покажи план человеку; ` +
                '2) получи явное «да» в чате; 3) повтори вызов с confirmed=true' +
                (dryRun ? "" : " и без dry_run") + ".",
            )
          }
          const key = guardKey(account, command, rest)
          if (kind === "hh_write" && DRY_RUN_FIRST_COMMANDS.has(command) && !dryRun) {
            if (!dryRunDone.has(key)) {
              throw new Error(
                `«${command}» требует dry-run до боевого запуска: сначала dry_run=true ` +
                  `(план показывается человеку), затем confirmed=true. Для связки ${key} ` +
                  "успешный dry-run ещё не выполнялся.",
              )
            }
          }

          // --dry-run добавляем только командам, которые его поддерживают
          // (список DRY_RUN_FIRST + остальные HH_WRITE по правилам проекта).
          // login сюда не попадает: classify() отдаёт для него headed_auth.
          const supportsDryRun = kind === "hh_write"
          const flagTokens = dryRun && supportsDryRun && !rest.includes("--dry-run")
            ? ["--dry-run", ...rest]
            : rest

          const headed = HEADED_COMMANDS.has(command)
          const argv = [
            "--account", account,
            ...(headed ? [] : ["--headless"]),
            ...globals.filter((g) => g !== "--headless"),
            command,
            ...flagTokens,
          ]

          const binary = findBinary(root)
          // У Bun shell нет цепочечного .timeout() (и .kill()) — проверено на
          // Bun 1.4.0: ShellExpression предоставляет только cwd/nothrow/quiet/
          // env/text/json/lines/run/then/throws. Поэтому ограничение времени —
          // OS-уровня через coreutils `timeout`/`gtimeout`: TERM после limitSec,
          // KILL ещё через 10 с (exit 124/137). На стоковом macOS `timeout` нет —
          // запускаем без него (лимиты времени остаются на стороне самого CLI).
          // Аргументы передаются массивом — Bun shell экранирует их сам.
          const limitSec = Math.ceil(timeoutFor(kind, command, dryRun) / 1000)
          const osTimeout = findTimeoutBin()
          const argvFull = osTimeout
            ? [osTimeout, "-k", "10", String(limitSec), binary, ...argv]
            : [binary, ...argv]
          const proc = await $`${argvFull}`
            .cwd(root)
            .nothrow()
            .quiet()

          const stdout = proc.stdout.toString()
          const stderr = proc.stderr.toString()
          let out = [stdout, stderr].filter((s) => s.trim().length > 0).join("\n--- stderr ---\n")
          if (out.length > MAX_OUTPUT) {
            out = out.slice(0, MAX_OUTPUT) + `\n... [вывод обрезан, полный — в ${join(root, "data/logs/hhru_bot.log")}]`
          }
          const status = proc.exitCode === 0 ? "[exit 0]" : `[exit ${proc.exitCode}]`
          if (proc.exitCode === 0 && dryRun && DRY_RUN_FIRST_COMMANDS.has(command)) {
            dryRunDone.add(key)
          }
          const hint =
            dryRun && proc.exitCode === 0 && kind === "hh_write"
              ? "\n(план dry-run получен; покажи его человеку и дождись явного «да» перед боевым запуском)"
              : ""
          // 124 = TERM от `timeout`, 137 = KILL после -k: лимит исчерпан.
          const timedOut = proc.exitCode === 124 || proc.exitCode === 137
          const timeoutNote = timedOut
            ? `\n[TIMEOUT] команда «${command}» прервана по лимиту ${limitSec} с (exit ${proc.exitCode}). ` +
              `Полный лог: ${join(root, "data/logs/hhru_bot.log")}`
            : ""
          return `${status}\n${out.trim()}${timeoutNote}${hint}`
        },
      }),
    },
  }
}

export default HhruPlugin
