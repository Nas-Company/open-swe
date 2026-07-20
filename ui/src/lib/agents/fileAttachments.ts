import type { FileChunk } from "./types"

export const MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
export const MAX_TEXT_FILE_COUNT = 5
export const MAX_TEXT_FILE_TOTAL_BYTES = 10 * 1024 * 1024
export const MAX_ATTACHMENT_ENCODED_BYTES = 20 * 1024 * 1024

export const TEXT_FILE_ACCEPT = ".md,.html,.json,.csv,.txt"

const MIME_TYPE_BY_EXTENSION = new Map([
  [".md", "text/markdown"],
  [".html", "text/html"],
  [".json", "application/json"],
  [".csv", "text/csv"],
  [".txt", "text/plain"],
])

export type TextFileAttachmentResult =
  | { ok: true; file: FileChunk }
  | { ok: false; message: string }

function extensionFor(fileName: string): string {
  const dotIndex = fileName.lastIndexOf(".")
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : ""
}

export function isSupportedTextFile(file: Pick<File, "name">): boolean {
  return MIME_TYPE_BY_EXTENSION.has(extensionFor(file.name))
}

export function fileChunkByteLength(file: FileChunk): number {
  const padding = file.base64.endsWith("==")
    ? 2
    : file.base64.endsWith("=")
      ? 1
      : 0
  return Math.max(0, Math.floor((file.base64.length * 3) / 4) - padding)
}

export function attachmentEncodedBytes(
  attachments: ReadonlyArray<{ base64: string }>
): number {
  return attachments.reduce(
    (total, attachment) => total + attachment.base64.length,
    0
  )
}

export function canReserveAttachmentEncodedBytes(
  currentBytes: number,
  candidateBytes: number
): boolean {
  return currentBytes + candidateBytes <= MAX_ATTACHMENT_ENCODED_BYTES
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = ""
  const chunkSize = 32 * 1024
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}

export async function fileToTextFileChunk(
  file: File
): Promise<TextFileAttachmentResult> {
  const extension = extensionFor(file.name)
  const mimeType = MIME_TYPE_BY_EXTENSION.get(extension)
  if (!mimeType) {
    return {
      ok: false,
      message: `${file.name} is not a supported text file. Use .md, .html, .json, .csv, or .txt.`,
    }
  }
  if (file.size === 0) {
    return { ok: false, message: `${file.name} is empty.` }
  }
  if (file.size > MAX_TEXT_FILE_BYTES) {
    return { ok: false, message: `${file.name} exceeds the 2 MiB limit.` }
  }

  try {
    const bytes = new Uint8Array(await file.arrayBuffer())
    new TextDecoder("utf-8", { fatal: true }).decode(bytes)
    return {
      ok: true,
      file: {
        kind: "file",
        base64: bytesToBase64(bytes),
        mimeType,
        fileName: file.name,
      },
    }
  } catch {
    return {
      ok: false,
      message: `${file.name} is not valid UTF-8 text.`,
    }
  }
}
