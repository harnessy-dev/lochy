import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export interface Store {
  describe(): string
  put(key: string, data: Buffer): Promise<void>
  get(key: string): Promise<Buffer>
}

export const DEFAULT_STORE = join(homedir(), '.sessionport', 'store')

class FileStore implements Store {
  constructor(private readonly root: string) {}

  describe(): string {
    return this.root
  }

  async put(key: string, data: Buffer): Promise<void> {
    const path = join(this.root, key)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, data)
  }

  async get(key: string): Promise<Buffer> {
    return readFileSync(join(this.root, key))
  }
}

class S3Store implements Store {
  constructor(
    private readonly bucket: string,
    private readonly prefix: string
  ) {}

  describe(): string {
    return `s3://${this.bucket}/${this.prefix}`
  }

  private keyFor(key: string): string {
    return this.prefix ? `${this.prefix}/${key}` : key
  }

  // Imported lazily so the file backend never pays for loading the SDK.
  private async sdk() {
    const sdk = await import('@aws-sdk/client-s3')
    const endpoint = process.env['SESSIONPORT_S3_ENDPOINT']
    const region = process.env['SESSIONPORT_S3_REGION']
    const client = new sdk.S3Client({
      ...(endpoint ? { endpoint, forcePathStyle: true } : {}),
      ...(region ? { region } : {})
    })
    return { sdk, client }
  }

  async put(key: string, data: Buffer): Promise<void> {
    const { sdk, client } = await this.sdk()
    await client.send(
      new sdk.PutObjectCommand({
        Bucket: this.bucket,
        Key: this.keyFor(key),
        Body: data,
        ContentType: 'application/gzip'
      })
    )
  }

  async get(key: string): Promise<Buffer> {
    const { sdk, client } = await this.sdk()
    const response = await client.send(
      new sdk.GetObjectCommand({ Bucket: this.bucket, Key: this.keyFor(key) })
    )
    if (!response.Body) throw new Error(`empty object at ${this.keyFor(key)}`)
    return Buffer.from(await response.Body.transformToByteArray())
  }
}

export function createStore(uri: string): Store {
  if (uri.startsWith('s3://')) {
    const withoutScheme = uri.slice('s3://'.length)
    const slash = withoutScheme.indexOf('/')
    const bucket = slash === -1 ? withoutScheme : withoutScheme.slice(0, slash)
    const prefix = slash === -1 ? '' : withoutScheme.slice(slash + 1).replace(/\/$/, '')
    if (!bucket) throw new Error(`invalid S3 store URI: ${uri}`)
    return new S3Store(bucket, prefix)
  }

  if (uri.startsWith('file://')) return new FileStore(fileURLToPath(uri))
  return new FileStore(isAbsolute(uri) ? uri : resolve(uri))
}

export function resolveStoreUri(explicit?: string): string {
  return explicit ?? process.env['SESSIONPORT_STORE'] ?? DEFAULT_STORE
}
