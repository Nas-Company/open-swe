import { useCallback, useMemo, useState } from "react"
import { Download, FileText, LoaderCircle, RotateCw } from "lucide-react"

import type { ThreadArtifact } from "@/lib/agents/types"
import { agentsApi } from "@/lib/agents/api"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

interface AgentArtifactsPanelProps {
  threadId: string
  artifacts: Array<ThreadArtifact>
  isLoading: boolean
  loadError?: string | null
  onRetryLoad: () => void
}

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "Unknown size"
  if (bytes < 1024) return `${bytes} B`
  const units = ["KB", "MB", "GB"]
  let value = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024
    unit = units[index]
  }
  const formatted = value >= 100 ? value.toFixed(0) : value.toFixed(1)
  return `${Number(formatted)} ${unit}`
}

function formatDate(value: string): string | null {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : DATE_FORMAT.format(date)
}

function fileTypeLabel(artifact: ThreadArtifact): string {
  const extension = artifact.fileName.split(".").pop()?.trim()
  if (extension && extension !== artifact.fileName)
    return extension.toUpperCase()
  return artifact.mimeType.split("/").pop()?.toUpperCase() || "FILE"
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

export function AgentArtifactsPanel({
  threadId,
  artifacts,
  isLoading,
  loadError,
  onRetryLoad,
}: AgentArtifactsPanelProps) {
  const [downloading, setDownloading] = useState<Record<string, boolean>>({})
  const [downloadErrors, setDownloadErrors] = useState<Record<string, string>>(
    {}
  )
  const sortedArtifacts = useMemo(
    () =>
      [...artifacts].sort(
        (left, right) =>
          Date.parse(right.createdAt) - Date.parse(left.createdAt)
      ),
    [artifacts]
  )

  const downloadArtifact = useCallback(
    async (artifact: ThreadArtifact) => {
      setDownloading((current) => ({ ...current, [artifact.id]: true }))
      setDownloadErrors((current) => {
        const next = { ...current }
        delete next[artifact.id]
        return next
      })
      try {
        const { blob, filename } = await agentsApi.downloadThreadArtifact(
          threadId,
          artifact.id,
          artifact.fileName
        )
        triggerDownload(blob, filename)
      } catch (error) {
        setDownloadErrors((current) => ({
          ...current,
          [artifact.id]:
            error instanceof Error ? error.message : "Download failed",
        }))
      } finally {
        setDownloading((current) => ({ ...current, [artifact.id]: false }))
      }
    },
    [threadId]
  )

  return (
    <section
      aria-labelledby="generated-files-heading"
      className="flex min-h-0 flex-1 flex-col"
    >
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-[var(--ui-border)] px-4">
        <h2
          id="generated-files-heading"
          className="text-xs font-medium text-[var(--ui-text)]"
        >
          Generated files
        </h2>
        {!isLoading && !loadError && (
          <span className="text-[11px] text-[var(--ui-text-dim)]">
            {sortedArtifacts.length}
          </span>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <div
            role="status"
            aria-label="Loading generated files"
            className="space-y-1 px-3 py-2"
          >
            {[0, 1, 2].map((item) => (
              <div key={item} className="flex items-center gap-3 px-1 py-2.5">
                <Skeleton className="size-8 shrink-0 rounded-md" />
                <div className="min-w-0 flex-1 space-y-2">
                  <Skeleton className="h-3 w-3/4" />
                  <Skeleton className="h-2.5 w-1/2" />
                </div>
                <Skeleton className="h-7 w-20 shrink-0" />
              </div>
            ))}
          </div>
        ) : loadError ? (
          <div className="flex min-h-40 flex-col items-center justify-center gap-3 px-6 text-center">
            <p role="alert" className="text-xs text-[var(--ui-danger)]">
              Couldn&apos;t load generated files.
            </p>
            <Button variant="outline" size="sm" onClick={onRetryLoad}>
              <RotateCw aria-hidden="true" />
              Retry
            </Button>
          </div>
        ) : sortedArtifacts.length === 0 ? (
          <div className="flex min-h-40 items-center justify-center px-6 text-center">
            <p className="max-w-56 text-xs text-[var(--ui-text-dim)]">
              Files generated by this agent will appear here.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-[var(--ui-border-subtle)]">
            {sortedArtifacts.map((artifact) => {
              const isDownloading = downloading[artifact.id] === true
              const downloadError = downloadErrors[artifact.id]
              const createdAt = formatDate(artifact.createdAt)
              const expiresAt = formatDate(artifact.expiresAt)
              const metadataId = `artifact-metadata-${artifact.id}`

              return (
                <li key={artifact.id} className="px-4 py-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--ui-panel-2)] text-[var(--ui-text-muted)]">
                      <FileText aria-hidden="true" className="size-4" />
                    </span>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <p
                        title={artifact.fileName}
                        className="truncate text-xs font-medium text-[var(--ui-text)]"
                      >
                        {artifact.fileName}
                      </p>
                      <p
                        id={metadataId}
                        className="mt-0.5 truncate text-[11px] text-[var(--ui-text-dim)]"
                      >
                        {fileTypeLabel(artifact)} ·{" "}
                        {formatBytes(artifact.sizeBytes)}
                        {createdAt ? ` · ${createdAt}` : ""}
                      </p>
                      {expiresAt && (
                        <p className="mt-0.5 text-[10px] text-[var(--ui-text-dim)]">
                          Available until {expiresAt}
                        </p>
                      )}
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 shrink-0 px-2.5"
                      disabled={isDownloading}
                      aria-describedby={metadataId}
                      aria-label={`${downloadError ? "Retry downloading" : "Download"} ${artifact.fileName}`}
                      aria-busy={isDownloading}
                      onClick={() => void downloadArtifact(artifact)}
                    >
                      {isDownloading ? (
                        <LoaderCircle
                          aria-hidden="true"
                          className="animate-spin"
                        />
                      ) : downloadError ? (
                        <RotateCw aria-hidden="true" />
                      ) : (
                        <Download aria-hidden="true" />
                      )}
                      {isDownloading
                        ? "Downloading…"
                        : downloadError
                          ? "Retry"
                          : "Download"}
                    </Button>
                  </div>
                  {downloadError && (
                    <p
                      role="alert"
                      className="mt-1.5 pl-11 text-[11px] text-[var(--ui-danger)]"
                    >
                      Download failed: {downloadError}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}
