import { describe, expect, it } from 'vitest'

import { residualOriginPaths, rewriteTranscript, type RewriteSpec } from './rewrite.js'

const crossMachine: RewriteSpec = {
  originCwd: '/Users/mike/proj',
  originHome: '/Users/mike',
  targetCwd: '/Users/alice/work/proj',
  targetHome: '/Users/alice'
}

describe('rewriteTranscript', () => {
  it('rewrites the cwd field and paths inside tool results', () => {
    const raw = [
      JSON.stringify({ type: 'user', cwd: '/Users/mike/proj', gitBranch: 'main' }),
      JSON.stringify({ type: 'assistant', toolUseResult: 'read /Users/mike/proj/src/a.ts' })
    ].join('\n')

    const out = rewriteTranscript(raw, crossMachine)

    expect(out).toContain('"cwd":"/Users/alice/work/proj"')
    expect(out).toContain('/Users/alice/work/proj/src/a.ts')
    expect(out).not.toContain('/Users/mike')
  })

  it('does not double-rewrite a cwd nested under the home directory', () => {
    const raw = JSON.stringify({
      cwd: '/Users/mike/proj',
      config: '/Users/mike/.claude/settings.json'
    })

    const out = rewriteTranscript(raw, crossMachine)

    expect(out).toContain('"cwd":"/Users/alice/work/proj"')
    expect(out).toContain('/Users/alice/.claude/settings.json')
    expect(out).not.toContain('/Users/alice/work/proj/.claude/settings.json')
  })

  it('rewrites the encoded project-directory form', () => {
    const raw = JSON.stringify({
      note: '~/.claude/projects/-Users-mike-proj/abc.jsonl'
    })

    const out = rewriteTranscript(raw, crossMachine)

    expect(out).toContain('-Users-alice-work-proj')
    expect(out).not.toContain('-Users-mike-proj')
  })

  it('rewrites the session id when a new one is minted', () => {
    const raw = JSON.stringify({ sessionId: 'old-id', uuid: 'x' })

    const out = rewriteTranscript(raw, {
      ...crossMachine,
      originSessionId: 'old-id',
      targetSessionId: 'new-id'
    })

    expect(out).toContain('"sessionId":"new-id"')
  })

  it('leaves the transcript untouched when origin and target match', () => {
    const sameMachine: RewriteSpec = {
      originCwd: '/Users/mike/proj',
      originHome: '/Users/mike',
      targetCwd: '/Users/mike/proj',
      targetHome: '/Users/mike'
    }
    const raw = JSON.stringify({ cwd: '/Users/mike/proj' })

    expect(rewriteTranscript(raw, sameMachine)).toBe(raw)
  })
})

describe('residualOriginPaths', () => {
  it('reports nothing when the rewrite was complete', () => {
    const out = rewriteTranscript(JSON.stringify({ cwd: '/Users/mike/proj' }), crossMachine)
    expect(residualOriginPaths(out, crossMachine)).toEqual([])
  })

  it('reports origin paths that survived', () => {
    expect(residualOriginPaths('stale /Users/mike/elsewhere/file.ts', crossMachine)).toEqual([
      '/Users/mike'
    ])
  })
})
