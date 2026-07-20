// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { AgentGitPanel } from "./AgentGitPanel"
import type { AgentThread, ThreadArtifact } from "@/lib/agents/types"

const mocks = vi.hoisted(() => ({
  artifacts: [] as Array<ThreadArtifact>,
  refetchArtifacts: vi.fn(),
}))

vi.mock("@pierre/diffs/react", () => ({
  MultiFileDiff: () => null,
  Virtualizer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  WorkerPoolContextProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
}))

vi.mock("@pierre/trees/react", () => ({
  FileTree: () => null,
  useFileTree: () => ({ model: {} }),
  useFileTreeSelection: () => ({}),
}))

vi.mock("@/components/agents/utils/diffUtils", () => ({
  DIFF_VIRTUALIZER_CONFIG: {},
  DIFF_VIRTUAL_METRICS: {},
  DIFF_WORKER_HIGHLIGHTER_OPTIONS: {},
  DIFF_WORKER_POOL_OPTIONS: {},
  fileContentsCacheKey: () => "key",
  useDiffOptions: () => ({}),
}))

vi.mock("@/components/agents/ReviewTab", () => ({
  ReviewTab: () => null,
}))

vi.mock("@/lib/agents/queries", () => ({
  useAgentThreadPrDiff: () => ({
    data: undefined,
    isLoading: false,
  }),
  useAgentThreadArtifacts: () => ({
    data: { artifacts: mocks.artifacts },
    isLoading: false,
    isError: false,
    error: null,
    refetch: mocks.refetchArtifacts,
  }),
}))

vi.mock("@/lib/useIsMobile", () => ({
  useIsMobile: () => false,
}))

const thread: AgentThread = {
  id: "thread-1",
  title: "Build report",
  repo: "nas-reporting",
  repoFullName: "Nas-Company/nas-reporting",
  branch: "main",
  model: "openai:gpt-5.6-sol",
  source: "dashboard",
  status: "finished",
  viewed: true,
  isOwner: true,
  createdAt: Date.now(),
  updatedAt: Date.now(),
  messages: [],
}

function artifact(id: string, fileName: string): ThreadArtifact {
  return {
    id,
    fileName,
    mimeType: "text/html",
    sizeBytes: 1024,
    sha256: id.repeat(64).slice(0, 64),
    createdAt: "2026-07-17T08:00:00Z",
    expiresAt: "2026-08-17T08:00:00Z",
  }
}

describe("AgentGitPanel generated files", () => {
  beforeEach(() => {
    const store = new Map<string, string>()
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => store.set(key, value),
        removeItem: (key: string) => store.delete(key),
        clear: () => store.clear(),
      },
    })
  })

  afterEach(() => {
    cleanup()
    mocks.artifacts = []
    vi.clearAllMocks()
    window.localStorage.clear()
  })

  it("shows a counted Files tab and its persistent file list", () => {
    mocks.artifacts = [
      artifact("a", "report.html"),
      artifact("b", "diagnostics.json"),
    ]
    render(
      <AgentGitPanel
        thread={thread}
        messages={[]}
        isStreaming={false}
        collapsed={false}
        onCollapsedChange={vi.fn()}
      />
    )

    const filesTab = screen.getByRole("tab", { name: /Files/ })
    expect(filesTab.textContent).toContain("2")
    const gitTab = screen.getByRole("tab", { name: "Git" })
    gitTab.focus()
    fireEvent.keyDown(gitTab, { key: "ArrowRight" })

    expect(filesTab.getAttribute("aria-selected")).toBe("true")
    expect(
      screen.getByRole("heading", { name: "Generated files" })
    ).not.toBeNull()
    expect(screen.getByText("report.html")).not.toBeNull()
    expect(screen.getByText("diagnostics.json")).not.toBeNull()
  })

  it("announces generated files from the collapsed workspace button", () => {
    mocks.artifacts = [artifact("a", "report.html")]
    render(
      <AgentGitPanel
        thread={thread}
        messages={[]}
        isStreaming={false}
        collapsed
        onCollapsedChange={vi.fn()}
      />
    )

    expect(
      screen.getByRole("button", {
        name: "Expand workspace panel, 1 generated file",
      })
    ).not.toBeNull()
  })

  it("opens the Files view when a new artifact arrives during the task", async () => {
    const onCollapsedChange = vi.fn()
    const { rerender } = render(
      <AgentGitPanel
        thread={thread}
        messages={[]}
        isStreaming
        collapsed
        onCollapsedChange={onCollapsedChange}
      />
    )

    mocks.artifacts = [artifact("a", "report.html")]
    rerender(
      <AgentGitPanel
        thread={thread}
        messages={[]}
        isStreaming
        collapsed
        onCollapsedChange={onCollapsedChange}
      />
    )

    await waitFor(() => expect(onCollapsedChange).toHaveBeenCalledWith(false))
  })
})
