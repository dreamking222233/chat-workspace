// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import { encodeVisionImageBlob, decodedDataUrlSize, VISION_MAX_INPUT_BYTES } from './mediaEncode'

describe('mediaEncode', () => {
  it('calculates decoded bytes from a base64 data URL', () => {
    expect(decodedDataUrlSize('data:image/jpeg;base64,SGVsbG8=')).toBe(5)
    expect(decodedDataUrlSize('data:image/png;base64,SGVsbG8=')).toBe(5)
    expect(decodedDataUrlSize('data:image/png;base64,')).toBe(0)
  })

  it('normalizes legacy JPEG MIME prefixes in the non-canvas fallback', async () => {
    const encoded = await encodeVisionImageBlob(new File(['fixture'], 'image.jpg', { type: 'image/jpg' }))
    expect(encoded.mimeType).toBe('image/jpeg')
    expect(encoded.dataUrl).toMatch(/^data:image\/jpeg;base64,Zm/)
  })

  it('honors a provider PNG-only output policy when canvas conversion is unavailable', async () => {
    const encoded = await encodeVisionImageBlob(new File(['fixture'], 'image.png', { type: 'image/png' }), undefined, ['image/png'])
    expect(encoded.mimeType).toBe('image/png')
    expect(encoded.dataUrl).toMatch(/^data:image\/png;base64,Zm/)
  })

  it('rejects unsupported formats when a browser cannot decode them', async () => {
    await expect(encodeVisionImageBlob(new File(['fixture'], 'image.gif', { type: 'image/gif' }))).rejects.toThrow('JPEG 或 PNG')
  })

  it('enforces the original upload and encoded payload limits', async () => {
    await expect(encodeVisionImageBlob(new Blob([new Uint8Array(VISION_MAX_INPUT_BYTES + 1)], { type: 'image/png' }))).rejects.toThrow('20 MiB')
    await expect(encodeVisionImageBlob(new File(['fixture'], 'image.png', { type: 'image/png' }), 2)).rejects.toThrow('视觉输入限制')
  })
})
