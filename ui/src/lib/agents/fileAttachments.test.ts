import { describe, expect, it } from "vitest"

import {
  MAX_ATTACHMENT_ENCODED_BYTES,
  MAX_TEXT_FILE_BYTES,
  attachmentEncodedBytes,
  canReserveAttachmentEncodedBytes,
  fileChunkByteLength,
  fileToTextFileChunk,
  isSupportedTextFile,
} from "./fileAttachments"

describe("text file attachments", () => {
  it("recognizes the supported extensions case-insensitively", () => {
    expect(isSupportedTextFile({ name: "analysis.MD" })).toBe(true)
    expect(isSupportedTextFile({ name: "report.html" })).toBe(true)
    expect(isSupportedTextFile({ name: "archive.pdf" })).toBe(false)
  })

  it("encodes valid UTF-8 with the canonical MIME type", async () => {
    const result = await fileToTextFileChunk(
      new File(["Insight: 成长"], "analysis.md", {
        type: "application/octet-stream",
      })
    )

    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.file).toMatchObject({
      kind: "file",
      fileName: "analysis.md",
      mimeType: "text/markdown",
    })
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(result.file.base64), (character) =>
        character.charCodeAt(0)
      )
    )
    expect(decoded).toBe("Insight: 成长")
    expect(fileChunkByteLength(result.file)).toBe(
      new TextEncoder().encode("Insight: 成长").byteLength
    )
  })

  it("rejects invalid UTF-8 and oversized files", async () => {
    const invalid = await fileToTextFileChunk(
      new File([new Uint8Array([0xff, 0xfe])], "invalid.txt")
    )
    const oversized = await fileToTextFileChunk(
      new File([new Uint8Array(MAX_TEXT_FILE_BYTES + 1)], "large.csv")
    )

    expect(invalid).toEqual({
      ok: false,
      message: "invalid.txt is not valid UTF-8 text.",
    })
    expect(oversized).toEqual({
      ok: false,
      message: "large.csv exceeds the 2 MiB limit.",
    })
  })

  it("caps the combined encoded bytes across attachment types", () => {
    expect(
      attachmentEncodedBytes([{ base64: "1234" }, { base64: "123456" }])
    ).toBe(10)
    expect(
      canReserveAttachmentEncodedBytes(MAX_ATTACHMENT_ENCODED_BYTES - 4, 4)
    ).toBe(true)
    expect(
      canReserveAttachmentEncodedBytes(MAX_ATTACHMENT_ENCODED_BYTES - 4, 5)
    ).toBe(false)
  })
})
