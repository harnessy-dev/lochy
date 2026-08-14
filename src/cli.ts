#!/usr/bin/env node
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir, hostname, platform } from 'node:os'
import { dirname } from 'node:path'
import { parseArgs } from 'node:util'

import {
  AGENT,
  listSessions,
  resolveCwd,
  transcriptPathFor,
  type SessionMeta
} from './claude.js'
import { BUNDLE_VERSION, bundleRef, packBundle, unpackBundle, type Bundle } from './bundle.js'
import { residualOriginPaths, rewriteTranscript } from './rewrite.js'
import { createStore, resolveStoreUri } from './store.js'

const USAGE = `sessionport — save and restore agent coding sessions across machines

Usage:
  sessionport list    [--cwd <path>] [--branch <name>]
  sessionport save    [--cwd <path>] [--branch <name>] [--session <id>] [--store <uri>]
  sessionport restore <ref> [--into <path>] [--store <uri>] [--force] [--new-id]

Stores:
  <path>              local directory
  s3://bucket/prefix  any S3-compatible endpoint

Environment:
  SESSIONPORT_STORE        default store URI
  SESSIONPORT_S3_ENDPOINT  custom endpoint (R2, MinIO, ...)
  SESSIONPORT_S3_REGION    region override
`

function fail(message: string): never {
  process.stderr.write(`sessionport: ${message}\n`)
  process.exit(1)
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}K`
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`
}

function describe(meta: SessionMeta): string {
  const branch = meta.gitBranch ? `[${meta.gitBranch}]` : '[no branch]'
  const summary = meta.summary ? ` ${meta.summary.replace(/\s+/g, ' ')}` : ''
  return `${meta.sessionId}  ${meta.modifiedAt.slice(0, 16).replace('T', ' ')}  ${formatBytes(
    meta.bytes
  ).padStart(5)}  ${branch}${summary}`
}

function collect(options: {
  cwd?: string
  branch?: string
  session?: string
}): { cwd: string; sessions: SessionMeta[] } {
  const cwd = resolveCwd(options.cwd ?? process.cwd())
  const sessions = listSessions({ cwd, branch: options.branch, sessionId: options.session })
  return { cwd, sessions }
}

function commandList(argv: string[]): void {
  const { values } = parseArgs({
    args: argv,
    options: { cwd: { type: 'string' }, branch: { type: 'string' } },
    allowPositionals: false
  })

  const { cwd, sessions } = collect(values)
  if (sessions.length === 0) {
    process.stdout.write(`no ${AGENT} sessions found for ${cwd}\n`)
    return
  }

  process.stdout.write(`${sessions.length} session(s) for ${cwd}\n`)
  for (const meta of sessions) process.stdout.write(`  ${describe(meta)}\n`)
}

async function commandSave(argv: string[]): Promise<void> {
  const { values } = parseArgs({
    args: argv,
    options: {
      cwd: { type: 'string' },
      branch: { type: 'string' },
      session: { type: 'string' },
      store: { type: 'string' }
    },
    allowPositionals: false
  })

  const { cwd, sessions } = collect(values)
  if (sessions.length === 0) fail(`no ${AGENT} sessions found for ${cwd}`)

  const bundle: Bundle = {
    version: BUNDLE_VERSION,
    agent: AGENT,
    createdAt: new Date().toISOString(),
    origin: { home: homedir(), platform: platform(), hostname: hostname() },
    sessions: sessions.map((meta) => ({
      sessionId: meta.sessionId,
      cwd: meta.cwd,
      gitBranch: meta.gitBranch,
      claudeVersion: meta.claudeVersion,
      modifiedAt: meta.modifiedAt,
      transcript: readFileSync(meta.path, 'utf8')
    }))
  }

  const packed = packBundle(bundle)
  const ref = bundleRef(packed)
  const store = createStore(resolveStoreUri(values.store))
  await store.put(`${ref}.spb`, packed)

  for (const session of bundle.sessions) {
    process.stdout.write(`  packed ${session.sessionId} [${session.gitBranch ?? 'no branch'}]\n`)
  }
  process.stdout.write(`\nstored ${formatBytes(packed.length)} in ${store.describe()}\n`)
  process.stdout.write(`ref ${ref}\n`)
}

async function commandRestore(argv: string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      into: { type: 'string' },
      store: { type: 'string' },
      force: { type: 'boolean' },
      'new-id': { type: 'boolean' }
    },
    allowPositionals: true
  })

  const ref = positionals[0]
  if (!ref) fail('restore requires a bundle ref')

  const store = createStore(resolveStoreUri(values.store))
  let bundle: Bundle
  try {
    bundle = unpackBundle(await store.get(`${ref}.spb`))
  } catch (error) {
    fail(`could not read ${ref} from ${store.describe()}: ${(error as Error).message}`)
  }

  const targetCwd = resolveCwd(values.into ?? process.cwd())
  const targetHome = homedir()
  const resumeCommands: string[] = []

  for (const session of bundle.sessions) {
    const targetSessionId = values['new-id'] ? randomUUID() : session.sessionId
    const spec = {
      originCwd: session.cwd,
      originHome: bundle.origin.home,
      targetCwd,
      targetHome,
      originSessionId: session.sessionId,
      targetSessionId
    }

    const transcript = rewriteTranscript(session.transcript, spec)
    const destination = transcriptPathFor(targetCwd, targetSessionId)

    if (existsSync(destination) && !values.force) {
      process.stderr.write(`  skipped ${targetSessionId} (already exists; --force to overwrite)\n`)
      continue
    }

    mkdirSync(dirname(destination), { recursive: true })
    writeFileSync(destination, transcript)

    const residual = residualOriginPaths(transcript, spec)
    const note = residual.length > 0 ? `  (residual origin paths: ${residual.join(', ')})` : ''
    process.stdout.write(`  restored ${targetSessionId} [${session.gitBranch ?? 'no branch'}]${note}\n`)
    resumeCommands.push(`  cd ${targetCwd} && claude --resume ${targetSessionId}`)
  }

  if (resumeCommands.length === 0) fail('nothing restored')

  process.stdout.write(`\nfrom ${bundle.origin.hostname} (${bundle.origin.home})\nresume with:\n`)
  for (const command of resumeCommands) process.stdout.write(`${command}\n`)
}

async function main(): Promise<void> {
  const [command, ...rest] = process.argv.slice(2)

  switch (command) {
    case 'list':
      return commandList(rest)
    case 'save':
      return commandSave(rest)
    case 'restore':
      return commandRestore(rest)
    case 'help':
    case '--help':
    case '-h':
    case undefined:
      process.stdout.write(USAGE)
      return
    default:
      fail(`unknown command '${command}' (try: sessionport help)`)
  }
}

main().catch((error: unknown) => fail((error as Error).message))
