import {
  ArrowUp,
  ChevronDown,
  LoaderCircle,
  Map as MapIcon,
  Paperclip,
  X,
} from "lucide-react"
import { StopIcon } from "@phosphor-icons/react"
import { useQueryClient } from "@tanstack/react-query"
import { useStreamContext as useAgentThreadStream } from "@langchain/react"
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"

import type { ModelOption } from "@/lib/api"
import type { FileChunk, ImageChunk } from "@/lib/agents/types"
import type { ModelSelection } from "@/lib/agents/provider/useModelOptions"
import { FileAttachmentPill } from "@/components/agents/FileAttachmentPill"
import { RepoSelector } from "@/components/agents/RepoSelector"
import { useIsInAgentThreadStream } from "@/lib/agents/provider/useIsInAgentThreadStream"
import {
  agentThreadKeys,
  invalidateAgentThreadLists,
} from "@/lib/agents/queries"
import {
  MAX_ATTACHMENT_ENCODED_BYTES,
  MAX_TEXT_FILE_BYTES,
  MAX_TEXT_FILE_COUNT,
  MAX_TEXT_FILE_TOTAL_BYTES,
  TEXT_FILE_ACCEPT,
  attachmentEncodedBytes,
  canReserveAttachmentEncodedBytes,
  fileChunkByteLength,
  fileToTextFileChunk,
  isSupportedTextFile,
} from "@/lib/agents/fileAttachments"
import { formatModelSelection } from "@/lib/agents/provider/useModelOptions"
import { IconButton } from "@/components/ui/button"
import { cn } from "@/lib/utils"

const PROMPT_TEXTAREA_MAX_HEIGHT = 200

interface SubmitButtonProps {
  canSubmit: boolean
  submitting: boolean
  onSubmit: () => void
}

function PlainSubmitButton({
  canSubmit,
  submitting,
  onSubmit,
}: SubmitButtonProps) {
  return (
    <IconButton
      type="button"
      onClick={onSubmit}
      disabled={!canSubmit}
      aria-label="Send message"
      className="shrink-0 rounded-full bg-[var(--ui-accent)] text-white hover:bg-[var(--ui-accent)] hover:opacity-90 disabled:cursor-default disabled:opacity-40"
    >
      {submitting ? (
        <LoaderCircle className="size-3.5 animate-spin" />
      ) : (
        <ArrowUp className="size-3.5" strokeWidth={2.5} />
      )}
    </IconButton>
  )
}

function SubmitButton(props: SubmitButtonProps) {
  const inAgentThreadStream = useIsInAgentThreadStream()

  if (inAgentThreadStream) return <StreamSubmitButton {...props} />

  return <PlainSubmitButton {...props} />
}

function StreamSubmitButton(props: SubmitButtonProps) {
  const stream = useAgentThreadStream()
  const queryClient = useQueryClient()
  const [stopping, setStopping] = useState(false)

  const handleStop = async () => {
    if (stopping) return
    setStopping(true)
    try {
      await stream.stop()
      const threadId = stream.threadId
      if (threadId) {
        queryClient.setQueryData(agentThreadKeys.detail(threadId), (prev) =>
          prev ? { ...prev, status: "interrupted" as const } : prev
        )
        invalidateAgentThreadLists(queryClient)
      }
    } finally {
      setStopping(false)
    }
  }

  if (!stream.isLoading) return <PlainSubmitButton {...props} />

  return (
    <IconButton
      type="button"
      onClick={() => void handleStop()}
      disabled={stopping}
      aria-label="Stop run"
      title="Stop run"
      className="shrink-0 rounded-full bg-[var(--ui-accent)] text-white hover:bg-[var(--ui-accent)] hover:opacity-90 disabled:cursor-default disabled:opacity-40"
    >
      {stopping ? (
        <LoaderCircle className="size-3.5 animate-spin" />
      ) : (
        <StopIcon className="size-3.5" weight="fill" />
      )}
    </IconButton>
  )
}
const MAX_IMAGE_COUNT = 5
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const SUPPORTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
])
const ATTACHMENT_ACCEPT = [...SUPPORTED_IMAGE_TYPES, TEXT_FILE_ACCEPT].join(",")

export interface CloudPromptBarProps {
  placeholder?: string
  compact?: boolean
  disabled?: boolean
  busy?: boolean
  onSubmit?: (
    value: string,
    images: Array<ImageChunk>,
    files: Array<FileChunk>
  ) => void | Promise<void>
  models?: Array<ModelOption>
  selection?: ModelSelection | null
  onSelectionChange?: (next: ModelSelection) => void
  /** Repos the user can target. When provided with onRepoChange, a repo picker is shown. */
  repos?: Array<{ full_name: string }>
  selectedRepo?: string | null
  onRepoChange?: (repo: string | null) => void
  /** When provided, a Plan mode toggle is shown. Plan mode researches read-only and proposes a plan before editing. */
  planMode?: boolean
  onPlanModeChange?: (next: boolean) => void
}

function fileToImageChunk(file: File): Promise<ImageChunk | null> {
  if (!SUPPORTED_IMAGE_TYPES.has(file.type) || file.size > MAX_IMAGE_BYTES) {
    return Promise.resolve(null)
  }

  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = typeof reader.result === "string" ? reader.result : ""
      const base64 = dataUrl.split(",")[1]
      resolve(
        base64
          ? {
              kind: "image",
              base64,
              mimeType: file.type,
              fileName: file.name,
            }
          : null
      )
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

/** Web-adapted PromptBar from open-swe-app — local state, no Electron/Zustand deps. */
export const CloudPromptBar = memo(function CloudPromptBarComponent({
  placeholder = "Ask Open SWE to build, fix bugs, explore",
  compact = false,
  disabled = false,
  busy = false,
  onSubmit,
  models = [],
  selection = null,
  onSelectionChange,
  repos,
  selectedRepo = null,
  onRepoChange,
  planMode = false,
  onPlanModeChange,
}: CloudPromptBarProps) {
  const [value, setValue] = useState("")
  const [pendingImages, setPendingImages] = useState<Array<ImageChunk>>([])
  const [pendingFiles, setPendingFiles] = useState<Array<FileChunk>>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [isDragOver, setIsDragOver] = useState(false)
  const [modelDropdownOpen, setModelDropdownOpen] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const pendingImagesRef = useRef<Array<ImageChunk>>([])
  const pendingFilesRef = useRef<Array<FileChunk>>([])
  const attachmentQueueRef = useRef<Promise<void>>(Promise.resolve())
  const attachmentFailureVersionRef = useRef(0)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const dragDepthRef = useRef(0)
  const modelDropdownRef = useRef<HTMLDivElement>(null)
  // Synchronous double-submit guard: blocks a same-tick second send (Enter +
  // click, or two rapid Enters) before React re-renders. Scoped to the send
  // request only — never the run lifecycle.
  const submittingRef = useRef(false)

  const updatePendingImages = useCallback(
    (update: (current: Array<ImageChunk>) => Array<ImageChunk>) => {
      const next = update(pendingImagesRef.current)
      pendingImagesRef.current = next
      setPendingImages(next)
    },
    []
  )

  const updatePendingFiles = useCallback(
    (update: (current: Array<FileChunk>) => Array<FileChunk>) => {
      const next = update(pendingFilesRef.current)
      pendingFilesRef.current = next
      setPendingFiles(next)
    },
    []
  )

  const combos = useMemo<Array<ModelSelection>>(() => {
    const list: Array<ModelSelection> = []
    for (const model of models) {
      for (const effort of model.efforts) {
        list.push({ modelId: model.id, effort })
      }
    }
    return list
  }, [models])

  const selectionLabel = formatModelSelection(models, selection)

  const selectedModelSupportsImages = useMemo(() => {
    if (!selection || pendingImages.length === 0) return true
    return models.some((m) => m.id === selection.modelId && m.supports_images)
  }, [selection, pendingImages.length, models])

  const canSubmit =
    !disabled &&
    !isSubmitting &&
    selectedModelSupportsImages &&
    (value.trim().length > 0 ||
      pendingImages.length > 0 ||
      pendingFiles.length > 0)

  const handleSubmit = useCallback(async () => {
    if (submittingRef.current || disabled) return
    const trimmed = value.trim()
    if (
      trimmed.length === 0 &&
      pendingImages.length === 0 &&
      pendingFiles.length === 0
    ) {
      return
    }

    submittingRef.current = true
    setIsSubmitting(true)
    try {
      const attachmentFailureVersion = attachmentFailureVersionRef.current
      await attachmentQueueRef.current
      if (attachmentFailureVersionRef.current !== attachmentFailureVersion) {
        return
      }
      const images = pendingImagesRef.current
      const files = pendingFilesRef.current
      if (trimmed.length === 0 && images.length === 0 && files.length === 0) {
        return
      }
      if (
        selection &&
        images.length > 0 &&
        !models.some(
          (model) => model.id === selection.modelId && model.supports_images
        )
      ) {
        return
      }
      await onSubmit?.(trimmed, images, files)
      setValue("")
      updatePendingImages(() => [])
      updatePendingFiles(() => [])
      setAttachmentError(null)
    } catch {
      // Caller surfaces send errors (e.g. via react-query mutation state).
    } finally {
      submittingRef.current = false
      setIsSubmitting(false)
    }
  }, [
    disabled,
    models,
    onSubmit,
    pendingFiles.length,
    pendingImages.length,
    selection,
    updatePendingFiles,
    updatePendingImages,
    value,
  ])

  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return

    el.style.height = "auto"
    const clampedHeight = Math.min(el.scrollHeight, PROMPT_TEXTAREA_MAX_HEIGHT)
    el.style.height = `${clampedHeight}px`
    el.style.overflowY =
      el.scrollHeight > PROMPT_TEXTAREA_MAX_HEIGHT ? "auto" : "hidden"
  }, [value])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node
      if (
        modelDropdownRef.current &&
        !modelDropdownRef.current.contains(target)
      ) {
        setModelDropdownOpen(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const addFiles = useCallback(
    (files: FileList | Array<File>) => {
      if (disabled || submittingRef.current) return Promise.resolve()
      setAttachmentError(null)
      const incoming = Array.from(files)
      const processBatch = async () => {
        const currentImages = pendingImagesRef.current
        const currentFiles = pendingFilesRef.current
        const acceptedImages: Array<ImageChunk> = []
        const acceptedFiles: Array<FileChunk> = []
        const errors: Array<string> = []
        const addError = (message: string) => {
          if (!errors.includes(message)) errors.push(message)
        }
        let imageCount = currentImages.length
        let textFileCount = currentFiles.length
        let encodedBytes = attachmentEncodedBytes([
          ...currentImages,
          ...currentFiles,
        ])
        let totalTextBytes = currentFiles.reduce(
          (total, file) => total + fileChunkByteLength(file),
          0
        )

        for (const file of incoming) {
          if (SUPPORTED_IMAGE_TYPES.has(file.type)) {
            if (imageCount >= MAX_IMAGE_COUNT) {
              addError("You can attach up to 5 images.")
              continue
            }
            if (file.size > MAX_IMAGE_BYTES) {
              addError(
                "Images must be PNG, JPEG, GIF, or WebP and no larger than 10 MiB."
              )
              continue
            }
            const estimatedEncodedBytes = Math.ceil(file.size / 3) * 4
            if (
              !canReserveAttachmentEncodedBytes(
                encodedBytes,
                estimatedEncodedBytes
              )
            ) {
              addError(
                `Attachments cannot exceed ${MAX_ATTACHMENT_ENCODED_BYTES / (1024 * 1024)} MiB after encoding.`
              )
              continue
            }
            const image = await fileToImageChunk(file)
            if (!image) {
              addError(
                "Images must be PNG, JPEG, GIF, or WebP and no larger than 10 MiB."
              )
              continue
            }
            if (
              !canReserveAttachmentEncodedBytes(
                encodedBytes,
                image.base64.length
              )
            ) {
              addError(
                `Attachments cannot exceed ${MAX_ATTACHMENT_ENCODED_BYTES / (1024 * 1024)} MiB after encoding.`
              )
              continue
            }
            acceptedImages.push(image)
            imageCount += 1
            encodedBytes += Math.max(image.base64.length, estimatedEncodedBytes)
            continue
          }

          if (!isSupportedTextFile(file)) {
            addError(
              "Attach images or UTF-8 .md, .html, .json, .csv, and .txt files."
            )
            continue
          }
          if (textFileCount >= MAX_TEXT_FILE_COUNT) {
            addError("You can attach up to 5 text files.")
            continue
          }
          if (file.size === 0) {
            addError(`${file.name} is empty.`)
            continue
          }
          if (file.size > MAX_TEXT_FILE_BYTES) {
            addError(`${file.name} exceeds the 2 MiB limit.`)
            continue
          }
          if (totalTextBytes + file.size > MAX_TEXT_FILE_TOTAL_BYTES) {
            addError("Text attachments cannot exceed 10 MiB in total.")
            continue
          }
          const estimatedEncodedBytes = Math.ceil(file.size / 3) * 4
          if (
            !canReserveAttachmentEncodedBytes(
              encodedBytes,
              estimatedEncodedBytes
            )
          ) {
            addError(
              `Attachments cannot exceed ${MAX_ATTACHMENT_ENCODED_BYTES / (1024 * 1024)} MiB after encoding.`
            )
            continue
          }
          const result = await fileToTextFileChunk(file)
          if (!result.ok) {
            addError(result.message)
            continue
          }
          if (
            !canReserveAttachmentEncodedBytes(
              encodedBytes,
              result.file.base64.length
            )
          ) {
            addError(
              `Attachments cannot exceed ${MAX_ATTACHMENT_ENCODED_BYTES / (1024 * 1024)} MiB after encoding.`
            )
            continue
          }
          acceptedFiles.push(result.file)
          textFileCount += 1
          totalTextBytes += fileChunkByteLength(result.file)
          encodedBytes += Math.max(
            result.file.base64.length,
            estimatedEncodedBytes
          )
        }

        if (acceptedImages.length > 0) {
          updatePendingImages((prev) => [...prev, ...acceptedImages])
        }
        if (acceptedFiles.length > 0) {
          updatePendingFiles((prev) => [...prev, ...acceptedFiles])
        }
        if (errors.length > 0) {
          attachmentFailureVersionRef.current += 1
          setAttachmentError(errors[0] ?? null)
        }
      }

      const processing = attachmentQueueRef.current.then(processBatch)
      const settled = processing.catch(() => {
        attachmentFailureVersionRef.current += 1
        setAttachmentError("The selected attachments could not be read.")
      })
      attachmentQueueRef.current = settled
      return settled
    },
    [disabled, updatePendingFiles, updatePendingImages]
  )

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files
      if (files) void addFiles(files)
      e.target.value = ""
    },
    [addFiles]
  )

  const handleDragEnter = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    dragDepthRef.current += 1
    setIsDragOver(true)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    setIsDragOver(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
    if (dragDepthRef.current === 0) setIsDragOver(false)
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!e.dataTransfer.types.includes("Files")) return
      e.preventDefault()
      dragDepthRef.current = 0
      setIsDragOver(false)
      void addFiles(e.dataTransfer.files)
    },
    [addFiles]
  )

  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = e.clipboardData.items
      const files: Array<File> = []
      for (const item of Array.from(items)) {
        if (item.kind === "file") {
          const file = item.getAsFile()
          if (
            file &&
            (SUPPORTED_IMAGE_TYPES.has(file.type) || isSupportedTextFile(file))
          ) {
            files.push(file)
          }
        }
      }
      if (files.length === 0) return
      e.preventDefault()
      void addFiles(files)
    },
    [addFiles]
  )

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && canSubmit) {
      e.preventDefault()
      void handleSubmit()
    }
    if (e.key === "Tab" && e.shiftKey && onPlanModeChange) {
      e.preventDefault()
      onPlanModeChange(!planMode)
    }
  }

  const pickerDisabled = combos.length === 0 || !onSelectionChange

  return (
    <div
      className={cn(
        "relative w-full font-sans text-[13px]",
        compact ? "max-w-none" : "max-w-2xl"
      )}
    >
      {onRepoChange && (
        <div className="mb-2 flex items-center gap-2 px-1 text-xs">
          <RepoSelector
            repos={repos}
            selectedRepo={selectedRepo}
            onRepoChange={onRepoChange}
          />
        </div>
      )}
      <div
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={cn(
          "relative flex min-h-[106px] flex-col rounded-2xl border border-[var(--ui-border)] bg-[var(--ui-surface)] px-3 py-2.5 shadow-sm",
          compact && "min-h-[88px]",
          isDragOver && "border-[var(--ui-accent)]"
        )}
      >
        {isDragOver && (
          <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-2xl bg-[var(--ui-surface)]/80 backdrop-blur-sm">
            <span className="rounded-md bg-[var(--ui-panel-2)] px-3 py-1.5 text-xs font-medium text-[color:var(--ui-accent)]">
              Drop images or text files here
            </span>
          </div>
        )}

        <input
          ref={fileInputRef}
          type="file"
          accept={ATTACHMENT_ACCEPT}
          multiple
          disabled={disabled || isSubmitting}
          className="hidden"
          onChange={handleFileChange}
        />

        {pendingImages.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {pendingImages.map((image, index) => (
              <div
                key={`${image.fileName ?? "image"}-${index}`}
                className="group relative"
              >
                <img
                  src={`data:${image.mimeType};base64,${image.base64}`}
                  alt={image.fileName || "pending image"}
                  className="size-16 rounded-lg border border-[var(--ui-border)] object-cover"
                />
                <button
                  type="button"
                  aria-label="Remove image"
                  onClick={() =>
                    updatePendingImages((prev) =>
                      prev.filter((_, i) => i !== index)
                    )
                  }
                  className="absolute -top-2 -right-2 flex size-7 items-center justify-center rounded-full border border-[var(--ui-border)] bg-[var(--ui-panel-2)] text-[color:var(--ui-text-muted)] shadow-sm transition-colors hover:text-[color:var(--ui-text)] focus-visible:ring-2 focus-visible:ring-[var(--ui-accent)] focus-visible:outline-none"
                >
                  <X className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        {pendingFiles.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {pendingFiles.map((file, index) => (
              <FileAttachmentPill
                key={`${file.fileName}-${index}`}
                fileName={file.fileName}
                mimeType={file.mimeType}
                onRemove={() =>
                  updatePendingFiles((prev) =>
                    prev.filter((_, fileIndex) => fileIndex !== index)
                  )
                }
              />
            ))}
          </div>
        )}

        {attachmentError && (
          <p
            role="status"
            className="mb-2 text-xs text-[color:var(--ui-text-muted)]"
          >
            {attachmentError}
          </p>
        )}

        {!selectedModelSupportsImages && (
          <div className="mb-2 rounded-md border border-[var(--ui-border)] bg-[var(--ui-panel-2)] px-3 py-1.5 text-xs text-[color:var(--ui-text-muted)]">
            The selected model does not support image input. Remove the image
            {pendingImages.length > 1 ? "s" : ""} or switch to a vision-enabled
            model to send.
          </div>
        )}

        <textarea
          ref={inputRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={busy ? "Send a message to queue next..." : placeholder}
          disabled={disabled || isSubmitting}
          className={cn(
            "w-full min-w-0 resize-none overflow-hidden bg-transparent text-[13px] leading-[1.45] text-[color:var(--ui-text)] outline-none placeholder:text-[color:var(--ui-text-dim)]",
            compact ? "min-h-[36px]" : "min-h-[52px]"
          )}
          style={{ maxHeight: PROMPT_TEXTAREA_MAX_HEIGHT }}
        />

        <div className="mt-auto flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 pt-2 text-xs text-[color:var(--ui-text-dim)]">
          <div ref={modelDropdownRef} className="relative min-w-0 shrink">
            <button
              type="button"
              disabled={pickerDisabled}
              onClick={() => setModelDropdownOpen((open) => !open)}
              className="flex max-w-[220px] cursor-pointer items-center gap-0.5 text-[13px] text-[color:var(--ui-text-muted)] transition-opacity hover:opacity-80 disabled:cursor-default disabled:opacity-60"
            >
              <span className="truncate">{selectionLabel}</span>
              {!pickerDisabled && (
                <ChevronDown className="size-3.5 shrink-0 opacity-60" />
              )}
            </button>
            {modelDropdownOpen && combos.length > 0 && (
              <div className="absolute bottom-full left-0 z-50 mb-1 max-h-72 overflow-hidden overflow-y-auto rounded border border-[var(--ui-border)] bg-[var(--ui-surface)] shadow-lg">
                {combos.map((combo) => {
                  const selected =
                    !!selection &&
                    selection.modelId === combo.modelId &&
                    selection.effort === combo.effort
                  return (
                    <button
                      key={`${combo.modelId}::${combo.effort}`}
                      type="button"
                      onClick={() => {
                        onSelectionChange?.(combo)
                        setModelDropdownOpen(false)
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 px-3 py-1.5 text-left whitespace-nowrap transition-colors hover:bg-[var(--ui-panel-2)]",
                        selected
                          ? "text-[color:var(--ui-text)]"
                          : "text-[color:var(--ui-text-muted)]"
                      )}
                    >
                      {formatModelSelection(models, combo)}
                      {selected && (
                        <span className="ml-auto pl-3 text-[color:var(--ui-text-dim)]">
                          ✓
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          {onPlanModeChange && (
            <button
              type="button"
              onClick={() => onPlanModeChange(!planMode)}
              aria-pressed={planMode}
              title="Plan mode: research read-only and propose a plan before editing (Shift+Tab)"
              className={cn(
                "flex shrink-0 items-center gap-1 rounded-full border px-2 py-1 text-[12px] transition-colors",
                planMode
                  ? "border-[var(--ui-accent)] bg-[var(--ui-accent)]/10 text-[color:var(--ui-accent)]"
                  : "border-[var(--ui-border)] text-[color:var(--ui-text-muted)] hover:bg-[var(--ui-panel-2)] hover:text-[color:var(--ui-text)]"
              )}
            >
              <MapIcon className="size-3.5" />
              <span>Plan</span>
            </button>
          )}

          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={
              disabled ||
              isSubmitting ||
              (pendingImages.length >= MAX_IMAGE_COUNT &&
                pendingFiles.length >= MAX_TEXT_FILE_COUNT)
            }
            aria-label="Attach images or text files"
            title="Attach images or text files"
            className="ml-auto flex size-7 shrink-0 items-center justify-center rounded-full text-[color:var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-panel-2)] hover:text-[color:var(--ui-text)] disabled:cursor-default disabled:opacity-40"
          >
            <Paperclip className="size-4" />
          </button>

          <SubmitButton
            canSubmit={canSubmit}
            submitting={isSubmitting}
            onSubmit={() => void handleSubmit()}
          />
        </div>
      </div>
    </div>
  )
})
