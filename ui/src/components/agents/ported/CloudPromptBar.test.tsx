// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { CloudPromptBar } from "./CloudPromptBar"
import { MAX_ATTACHMENT_ENCODED_BYTES } from "@/lib/agents/fileAttachments"

function textFile(name: string, content = "# Insights"): File {
  const bytes = new TextEncoder().encode(content)
  const file = new File([bytes], name)
  Object.defineProperty(file, "arrayBuffer", {
    value: () => Promise.resolve(bytes.buffer),
  })
  return file
}

function deferredFile(name: string, bytes: Uint8Array) {
  const buffer = new ArrayBuffer(bytes.byteLength)
  new Uint8Array(buffer).set(bytes)
  let resolveRead: ((value: ArrayBuffer) => void) | undefined
  let markReadStarted: (() => void) | undefined
  const readStarted = new Promise<void>((resolve) => {
    markReadStarted = resolve
  })
  const file = new File([buffer], name)
  Object.defineProperty(file, "arrayBuffer", {
    value: () => {
      markReadStarted?.()
      return new Promise<ArrayBuffer>((resolve) => {
        resolveRead = resolve
      })
    },
  })
  return {
    file,
    readStarted,
    resolveRead: () => resolveRead?.(buffer),
  }
}

function deferredTextFile(name: string, content = "# Insights") {
  return deferredFile(name, new TextEncoder().encode(content))
}

describe("CloudPromptBar text attachments", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("shows a removable file pill and submits the file separately from images", async () => {
    const onSubmit = vi.fn()
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const file = textFile("analysis.md")

    expect(input?.accept).toContain(".md")
    fireEvent.change(input!, {
      target: { files: [file] },
    })

    await screen.findByText("analysis.md")
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Build the report" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit).toHaveBeenCalledWith(
      "Build the report",
      [],
      [
        expect.objectContaining({
          kind: "file",
          fileName: "analysis.md",
          mimeType: "text/markdown",
        }),
      ]
    )
  })

  it("keeps the prompt and attachments when submission fails", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("network failed"))
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = container.querySelector<HTMLTextAreaElement>("textarea")
    if (!textarea) throw new Error("textarea not rendered")

    fireEvent.change(input!, {
      target: { files: [textFile("analysis.md")] },
    })
    await screen.findByText("analysis.md")
    fireEvent.change(textarea, {
      target: { value: "Build the report" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    await waitFor(() => expect(textarea.disabled).toBe(false))
    expect(textarea.value).toBe("Build the report")
    expect(screen.queryByText("analysis.md")).not.toBeNull()
  })

  it("waits for a selected attachment to finish reading before Enter submits", async () => {
    const onSubmit = vi.fn()
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")
    const deferred = deferredTextFile("analysis.md")

    fireEvent.change(textarea, { target: { value: "Build the report" } })
    fireEvent.change(input!, { target: { files: [deferred.file] } })
    fireEvent.keyDown(textarea, { key: "Enter" })

    expect(onSubmit).not.toHaveBeenCalled()
    expect(textarea.disabled).toBe(true)

    await deferred.readStarted
    act(() => deferred.resolveRead())

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit).toHaveBeenCalledWith(
      "Build the report",
      [],
      [expect.objectContaining({ fileName: "analysis.md" })]
    )
  })

  it("retains an attachment that finished reading before a failed send", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("network failed"))
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")
    const deferred = deferredTextFile("analysis.md")

    fireEvent.change(textarea, { target: { value: "Build the report" } })
    fireEvent.change(input!, { target: { files: [deferred.file] } })
    fireEvent.keyDown(textarea, { key: "Enter" })
    await deferred.readStarted
    act(() => deferred.resolveRead())

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    await waitFor(() => expect(textarea.disabled).toBe(false))
    expect(textarea.value).toBe("Build the report")
    expect(screen.queryByText("analysis.md")).not.toBeNull()
  })

  it("aborts a waiting submit when the selected attachment is rejected", async () => {
    const onSubmit = vi.fn()
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")
    const deferred = deferredFile("invalid.txt", new Uint8Array([0xff, 0xfe]))

    fireEvent.change(textarea, { target: { value: "Read this file" } })
    fireEvent.change(input!, { target: { files: [deferred.file] } })
    fireEvent.keyDown(textarea, { key: "Enter" })
    await deferred.readStarted
    act(() => deferred.resolveRead())

    await waitFor(() => expect(textarea.disabled).toBe(false))
    expect(onSubmit).not.toHaveBeenCalled()
    expect(textarea.value).toBe("Read this file")
    expect(screen.getByRole("status").textContent).toContain(
      "invalid.txt is not valid UTF-8 text."
    )

    fireEvent.keyDown(textarea, { key: "Enter" })
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit).toHaveBeenCalledWith("Read this file", [], [])
  })

  it("accepts text files from both drop and paste", async () => {
    render(<CloudPromptBar onSubmit={vi.fn()} />)
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")
    const promptSurface = textarea.parentElement
    if (!promptSurface) throw new Error("prompt surface not rendered")

    fireEvent.drop(promptSurface, {
      dataTransfer: {
        types: ["Files"],
        files: [textFile("dropped.md")],
      },
    })
    await screen.findByText("dropped.md")

    fireEvent.paste(textarea, {
      clipboardData: {
        items: [
          {
            kind: "file",
            getAsFile: () => textFile("pasted.txt", "notes"),
          },
        ],
      },
    })
    await screen.findByText("pasted.txt")
  })

  it("keeps the draft until submission is confirmed", async () => {
    let confirmSubmission: (() => void) | undefined
    const onSubmit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          confirmSubmission = resolve
        })
    )
    const { container } = render(<CloudPromptBar onSubmit={onSubmit} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")

    fireEvent.change(input!, {
      target: { files: [textFile("analysis.md")] },
    })
    await screen.findByText("analysis.md")
    fireEvent.change(textarea, {
      target: { value: "Build the report" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(textarea.value).toBe("Build the report")
    expect(screen.queryByText("analysis.md")).not.toBeNull()

    act(() => confirmSubmission?.())

    await waitFor(() => expect(textarea.value).toBe(""))
    expect(screen.queryByText("analysis.md")).toBeNull()
  })

  it("keeps attachment remove controls visible and touch-sized", async () => {
    const { container } = render(<CloudPromptBar onSubmit={vi.fn()} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')

    fireEvent.change(input!, {
      target: {
        files: [
          new File(["image"], "preview.png", { type: "image/png" }),
          textFile("analysis.md"),
        ],
      },
    })

    const removeImage = await screen.findByRole("button", {
      name: "Remove image",
    })
    const removeFile = await screen.findByRole("button", {
      name: "Remove analysis.md",
    })
    for (const button of [removeImage, removeFile]) {
      expect(button.className).toContain("size-7")
      expect(button.className).toContain("focus-visible:ring-2")
      expect(button.className).not.toContain("opacity-0")
    }

    fireEvent.click(removeImage)
    fireEvent.click(removeFile)
    expect(screen.queryByRole("button", { name: "Remove image" })).toBeNull()
    expect(
      screen.queryByRole("button", { name: "Remove analysis.md" })
    ).toBeNull()
  })

  it("applies the file count cap atomically across concurrent additions", async () => {
    const { container } = render(<CloudPromptBar onSubmit={vi.fn()} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const files = Array.from({ length: 6 }, (_, index) =>
      textFile(`file-${index}.txt`, `file ${index}`)
    )

    fireEvent.change(input!, { target: { files: files.slice(0, 3) } })
    fireEvent.change(input!, { target: { files: files.slice(3) } })

    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: /Remove file-\d\.txt/ })
      ).toHaveLength(5)
    )
    expect(screen.getByRole("status").textContent).toContain(
      "You can attach up to 5 text files."
    )
  })

  it("applies the encoded byte cap atomically across concurrent additions", async () => {
    const encodedImage = "a".repeat(MAX_ATTACHMENT_ENCODED_BYTES / 2 + 1)
    class MockFileReader {
      result: string | ArrayBuffer | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null

      readAsDataURL(file: File) {
        this.result = `data:${file.type};base64,${encodedImage}`
        queueMicrotask(() => this.onload?.())
      }
    }
    vi.stubGlobal("FileReader", MockFileReader)
    const { container } = render(<CloudPromptBar onSubmit={vi.fn()} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')

    fireEvent.change(input!, {
      target: { files: [new File(["a"], "first.png", { type: "image/png" })] },
    })
    fireEvent.change(input!, {
      target: { files: [new File(["b"], "second.png", { type: "image/png" })] },
    })

    await waitFor(() =>
      expect(container.querySelectorAll("img")).toHaveLength(1)
    )
    expect(screen.getByRole("status").textContent).toContain(
      "Attachments cannot exceed 20 MiB after encoding."
    )
  })

  it("does not read image files beyond the attachment count limit", async () => {
    let readCount = 0
    class CountingFileReader {
      result: string | ArrayBuffer | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null

      readAsDataURL(file: File) {
        readCount += 1
        this.result = `data:${file.type};base64,aW1hZ2U=`
        queueMicrotask(() => this.onload?.())
      }
    }
    vi.stubGlobal("FileReader", CountingFileReader)
    const { container } = render(<CloudPromptBar onSubmit={vi.fn()} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const files = Array.from(
      { length: 50 },
      (_, index) =>
        new File([`image-${index}`], `image-${index}.png`, {
          type: "image/png",
        })
    )

    fireEvent.change(input!, { target: { files } })

    await waitFor(() =>
      expect(container.querySelectorAll("img")).toHaveLength(5)
    )
    expect(readCount).toBe(5)
    expect(screen.getByRole("status").textContent).toContain(
      "You can attach up to 5 images."
    )
  })

  it("uses raw file size to enforce the encoded aggregate cap before reading", async () => {
    let readCount = 0
    class CountingFileReader {
      result: string | ArrayBuffer | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null

      readAsDataURL(file: File) {
        readCount += 1
        this.result = `data:${file.type};base64,aW1hZ2U=`
        queueMicrotask(() => this.onload?.())
      }
    }
    vi.stubGlobal("FileReader", CountingFileReader)
    const { container } = render(<CloudPromptBar onSubmit={vi.fn()} />)
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const files = ["first.png", "second.png"].map((name) => {
      const file = new File(["image"], name, { type: "image/png" })
      Object.defineProperty(file, "size", { value: 10 * 1024 * 1024 })
      return file
    })

    fireEvent.change(input!, { target: { files } })

    await waitFor(() =>
      expect(container.querySelectorAll("img")).toHaveLength(1)
    )
    expect(readCount).toBe(1)
    expect(screen.getByRole("status").textContent).toContain(
      "Attachments cannot exceed 20 MiB after encoding."
    )
  })
})
