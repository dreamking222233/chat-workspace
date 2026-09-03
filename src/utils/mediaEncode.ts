export const VISION_MAX_ENCODED_CHARS = 1_572_864
export const VISION_MAX_TOTAL_ENCODED_CHARS = 3_145_728
export const VISION_MAX_INPUT_BYTES = 20 * 1024 * 1024

export interface EncodedVisionImage {
  dataUrl: string
  mimeType: 'image/jpeg' | 'image/png'
  width: number
  height: number
  decodedSize: number
  encodedLength: number
}

const IMAGE_SIZES = [1600, 1280, 1024, 800]
const JPEG_QUALITIES = [0.86, 0.76, 0.66]

export type VisionMimeType = EncodedVisionImage['mimeType']

const DEFAULT_OUTPUT_MIME_TYPES: VisionMimeType[] = ['image/jpeg', 'image/png']

function canonicalImageMime(value: string): VisionMimeType | null {
  const mime = value.trim().toLowerCase().split(';', 1)[0]
  if (mime === 'image/jpg' || mime === 'image/jpeg') return 'image/jpeg'
  if (mime === 'image/png') return 'image/png'
  return null
}

export function decodedDataUrlSize(dataUrl: string): number {
  const encoded = dataUrl.split(',', 2)[1] ?? ''
  if (!encoded) return 0
  const padding = encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0
  return Math.max(0, Math.floor((encoded.length * 3) / 4) - padding)
}

function readAsDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    if (typeof FileReader === 'undefined') {
      reject(new Error('当前浏览器不支持读取图片'))
      return
    }
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('image read failed'))
    reader.readAsDataURL(blob)
  })
}

function loadImage(blob: Blob): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const source = URL.createObjectURL(blob)
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(source)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(source)
      reject(new Error('image decode failed'))
    }
    image.src = source
  })
}

function canvasAvailable(): boolean {
  return typeof document !== 'undefined' && typeof document.createElement === 'function'
}

/** Encode browser-local pixels for text-model vision input. */
export async function encodeVisionImageBlob(
  blob: Blob,
  maxEncodedChars = VISION_MAX_ENCODED_CHARS,
  preferredMimeTypes?: VisionMimeType[],
): Promise<EncodedVisionImage> {
  const declaredMime = String(blob.type || '')
  if (!declaredMime.toLowerCase().startsWith('image/')) throw new Error('仅支持图片文件')
  if (blob.size > VISION_MAX_INPUT_BYTES) throw new Error('图片超过 20 MiB 限制')
  const requestedMimeTypes = (preferredMimeTypes ?? DEFAULT_OUTPUT_MIME_TYPES).filter((item): item is VisionMimeType => item === 'image/jpeg' || item === 'image/png')
  const outputMimeTypes = [...new Set(requestedMimeTypes.length ? requestedMimeTypes : DEFAULT_OUTPUT_MIME_TYPES)]
  const outputMime = outputMimeTypes[0] ?? 'image/jpeg'

  const fallback = async (): Promise<EncodedVisionImage> => {
    const dataUrl = await readAsDataUrl(blob)
    const header = dataUrl.slice(0, dataUrl.indexOf(','))
    const sourceMime = canonicalImageMime(header.replace(/^data:/i, '').replace(/;base64$/i, ''))
    const sourceType = sourceMime ?? canonicalImageMime(declaredMime)
    const mimeType = sourceType && outputMimeTypes.includes(sourceType) ? sourceType : null
    if (!mimeType) throw new Error('当前浏览器无法处理该图片格式，请转换为 JPEG 或 PNG 后重试')
    // FileReader uses the Blob's declared type in the prefix. Normalize the
    // legacy `image/jpg` spelling so the API contract and bytes agree.
    const encodedPart = dataUrl.split(',', 2)[1] ?? ''
    if (!dataUrl.startsWith('data:') || !encodedPart) throw new Error('图片读取失败，请重试')
    const normalizedDataUrl = `data:${mimeType};base64,${encodedPart}`
    const encodedLength = encodedPart.length
    if (encodedLength > maxEncodedChars) throw new Error('图片压缩后仍超过视觉输入限制')
    return {
      dataUrl: normalizedDataUrl,
      mimeType,
      width: 1,
      height: 1,
      decodedSize: decodedDataUrlSize(normalizedDataUrl),
      encodedLength,
    }
  }

  // The fallback keeps the helper usable in non-visual test environments.
  // Normal browsers use the canvas branch so large uploads are compressed.
  if (!canvasAvailable()) return fallback()

  let image: HTMLImageElement
  try {
    image = await loadImage(blob)
  } catch {
    return fallback()
  }
  const sourceWidth = Math.max(1, image.naturalWidth || image.width)
  const sourceHeight = Math.max(1, image.naturalHeight || image.height)
  const sourceLongest = Math.max(sourceWidth, sourceHeight)
  let lastCandidate: EncodedVisionImage | null = null

  for (const limit of IMAGE_SIZES) {
    const scale = Math.min(1, limit / sourceLongest)
    const width = Math.max(1, Math.round(sourceWidth * scale))
    const height = Math.max(1, Math.round(sourceHeight * scale))
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    let context: CanvasRenderingContext2D | null
    try {
      context = canvas.getContext('2d')
    } catch {
      return fallback()
    }
    if (!context) return fallback()
    // JPEG has no alpha channel. A white background preserves the visible
    // appearance of transparent PNG uploads when sent to a text model. Keep
    // the alpha channel when a provider explicitly accepts PNG only.
    if (outputMime === 'image/jpeg') {
      context.fillStyle = '#ffffff'
      context.fillRect(0, 0, width, height)
    }
    context.drawImage(image, 0, 0, width, height)

    const qualities: Array<number | undefined> = outputMime === 'image/jpeg' ? JPEG_QUALITIES : [undefined]
    for (const quality of qualities) {
      let dataUrl: string
      try {
        dataUrl = quality === undefined ? canvas.toDataURL(outputMime) : canvas.toDataURL(outputMime, quality)
      } catch {
        return fallback()
      }
      const encodedLength = dataUrl.split(',', 2)[1]?.length ?? 0
      if (!dataUrl.startsWith(`data:${outputMime};base64,`) || !encodedLength) continue
      const candidate: EncodedVisionImage = {
        dataUrl,
        mimeType: outputMime,
        width,
        height,
        decodedSize: decodedDataUrlSize(dataUrl),
        encodedLength,
      }
      lastCandidate = candidate
      if (encodedLength <= maxEncodedChars) return candidate
    }
  }

  if (lastCandidate && lastCandidate.encodedLength <= maxEncodedChars) return lastCandidate
  throw new Error('图片压缩后仍超过视觉输入限制')
}
