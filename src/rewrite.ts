import { encodePath } from './claude.js'

export interface RewriteSpec {
  originCwd: string
  originHome: string
  targetCwd: string
  targetHome: string
  originSessionId?: string
  targetSessionId?: string
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Single left-to-right pass so a substitution's output can never be
 *  re-matched by a later pair. Longest patterns win, which keeps a cwd
 *  nested under a home directory from being half-rewritten by the home
 *  pair. */
function applyReplacements(text: string, pairs: Array<[string, string]>): string {
  const active = pairs.filter(([from, to]) => from.length > 0 && from !== to)
  if (active.length === 0) return text

  const sorted = [...active].sort((a, b) => b[0].length - a[0].length)
  const lookup = new Map(sorted)
  const pattern = new RegExp(sorted.map(([from]) => escapeRegExp(from)).join('|'), 'g')
  return text.replace(pattern, (match) => lookup.get(match) ?? match)
}

export function rewriteTranscript(raw: string, spec: RewriteSpec): string {
  const pairs: Array<[string, string]> = [
    [spec.originCwd, spec.targetCwd],
    [spec.originHome, spec.targetHome],
    [encodePath(spec.originCwd), encodePath(spec.targetCwd)]
  ]

  if (spec.originSessionId && spec.targetSessionId) {
    pairs.push([spec.originSessionId, spec.targetSessionId])
  }

  return applyReplacements(raw, pairs)
}

/** Occurrences of the origin's paths that survived a rewrite. A non-empty
 *  result means the restored session will reference files that don't exist
 *  on this machine. */
export function residualOriginPaths(text: string, spec: RewriteSpec): string[] {
  const found = new Set<string>()
  for (const needle of [spec.originCwd, spec.originHome]) {
    if (needle && needle !== spec.targetCwd && needle !== spec.targetHome && text.includes(needle)) {
      found.add(needle)
    }
  }
  return [...found]
}
