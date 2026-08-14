import { createHash } from 'node:crypto'
import { gunzipSync, gzipSync } from 'node:zlib'

export const BUNDLE_VERSION = 1

export interface BundleSession {
  sessionId: string
  cwd: string
  gitBranch?: string
  claudeVersion?: string
  modifiedAt: string
  transcript: string
}

export interface Bundle {
  version: number
  agent: string
  createdAt: string
  origin: {
    home: string
    platform: string
    hostname: string
  }
  sessions: BundleSession[]
}

export function packBundle(bundle: Bundle): Buffer {
  return gzipSync(Buffer.from(JSON.stringify(bundle), 'utf8'), { level: 9 })
}

export function unpackBundle(data: Buffer): Bundle {
  const bundle = JSON.parse(gunzipSync(data).toString('utf8')) as Bundle
  if (bundle.version !== BUNDLE_VERSION) {
    throw new Error(
      `unsupported bundle version ${bundle.version} (this build reads ${BUNDLE_VERSION})`
    )
  }
  return bundle
}

export function bundleRef(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex')
}
