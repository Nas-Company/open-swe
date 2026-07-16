import { FileText, X } from "lucide-react"

import { cn } from "@/lib/utils"

interface FileAttachmentPillProps {
  fileName: string
  mimeType: string
  onRemove?: () => void
  className?: string
}

function fileTypeLabel(fileName: string, mimeType: string): string {
  const extension = fileName.split(".").pop()?.trim()
  if (extension && extension !== fileName) return extension.toUpperCase()
  return mimeType.split("/").pop()?.toUpperCase() || "FILE"
}

export function FileAttachmentPill({
  fileName,
  mimeType,
  onRemove,
  className,
}: FileAttachmentPillProps) {
  return (
    <div
      title={fileName}
      className={cn(
        "inline-flex max-w-full min-w-0 items-center gap-2 rounded-lg border border-[var(--ui-border)] bg-[var(--ui-panel-2)] px-2.5 py-1.5 text-xs text-[color:var(--ui-text)]",
        className
      )}
    >
      <FileText
        aria-hidden="true"
        className="size-3.5 shrink-0 text-[color:var(--ui-text-muted)]"
      />
      <span className="max-w-52 truncate font-medium">{fileName}</span>
      <span className="shrink-0 text-[10px] tracking-wide text-[color:var(--ui-text-dim)]">
        {fileTypeLabel(fileName, mimeType)}
      </span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${fileName}`}
          className="-my-1 -mr-1.5 flex size-7 shrink-0 items-center justify-center rounded-full text-[color:var(--ui-text-dim)] transition-colors hover:bg-[var(--ui-surface)] hover:text-[color:var(--ui-text)] focus-visible:ring-2 focus-visible:ring-[var(--ui-accent)] focus-visible:outline-none"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  )
}
