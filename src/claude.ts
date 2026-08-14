import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const AGENT = 'claude-code'

const METADATA_SCAN_LINES = 200

export interface SessionMeta {
  sessionId: string
  path: string
  cwd: string
  gitBranch?: string
  claudeVersion?: string
  bytes: number
  modifiedAt: string
  summary?: string
}

export function projectsDir(home: string = homedir()): string {
  return join(home, '.claude', 'projects')
}

/** Claude resolves symlinks before deriving the directory name, so /tmp
 *  becomes /private/tmp on macOS. Resolving here keeps lookups matching. */
export function resolveCwd(cwd: string): string {
  try {
    return realpathSync(cwd)
  } catch {
    return cwd
  }
}

/** Pure form of the encoding, for paths that don't exist locally (an
 *  origin machine's cwd) and so can't be passed through realpath. */
export function encodePath(path: string): string {
  return path.replace(/[^a-zA-Z0-9]/g, '-')
}

export function encodeCwd(cwd: string): string {
  return encodePath(resolveCwd(cwd))
}

export function sessionDirFor(cwd: string, home: string = homedir()): string {
  return join(projectsDir(home), encodeCwd(cwd))
}

export function transcriptPathFor(
  cwd: string,
  sessionId: string,
  home: string = homedir()
): string {
  return join(sessionDirFor(cwd, home), `${sessionId}.jsonl`)
}

function firstString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 && value !== '-' ? value : undefined
}

function extractSummary(record: Record<string, unknown>): string | undefined {
  if (record['type'] !== 'user') return undefined
  const message = record['message'] as { content?: unknown } | undefined
  const content = message?.content
  if (typeof content === 'string') return content.slice(0, 120)
  if (!Array.isArray(content)) return undefined
  for (const block of content) {
    const text = (block as { type?: unknown; text?: unknown } | null)?.text
    if (typeof text === 'string' && text.length > 0) return text.slice(0, 120)
  }
  return undefined
}

export function readSessionMeta(path: string): SessionMeta | null {
  let stat: ReturnType<typeof statSync>
  try {
    stat = statSync(path)
  } catch {
    return null
  }

  let raw: string
  try {
    raw = readFileSync(path, 'utf8')
  } catch {
    return null
  }

  const lines = raw.split('\n')
  const sessionId = path.split('/').pop()!.replace(/\.jsonl$/, '')
  let cwd: string | undefined
  let gitBranch: string | undefined
  let claudeVersion: string | undefined
  let summary: string | undefined

  for (let i = 0; i < Math.min(lines.length, METADATA_SCAN_LINES); i++) {
    const line = lines[i]?.trim()
    if (!line) continue
    let record: Record<string, unknown>
    try {
      record = JSON.parse(line) as Record<string, unknown>
    } catch {
      continue
    }
    cwd ??= firstString(record['cwd'])
    gitBranch ??= firstString(record['gitBranch'])
    claudeVersion ??= firstString(record['version'])
    summary ??= extractSummary(record)
    if (cwd && gitBranch && claudeVersion && summary) break
  }

  if (!cwd) return null

  return {
    sessionId,
    path,
    cwd,
    gitBranch,
    claudeVersion,
    bytes: stat.size,
    modifiedAt: stat.mtime.toISOString(),
    summary
  }
}

export interface ListOptions {
  cwd: string
  branch?: string
  sessionId?: string
  home?: string
}

export function listSessions(options: ListOptions): SessionMeta[] {
  const home = options.home ?? homedir()
  const dir = sessionDirFor(options.cwd, home)
  if (!existsSync(dir)) return []

  const metas: SessionMeta[] = []
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.jsonl')) continue
    if (options.sessionId && file !== `${options.sessionId}.jsonl`) continue
    const meta = readSessionMeta(join(dir, file))
    if (!meta) continue
    if (options.branch && meta.gitBranch !== options.branch) continue
    metas.push(meta)
  }

  return metas.sort((a, b) => b.modifiedAt.localeCompare(a.modifiedAt))
}
