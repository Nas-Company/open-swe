// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { useSubmitAgentMessage } from "./useSubmitAgentMessage"
import type { ReactNode } from "react"

const mocks = vi.hoisted(() => ({
  isLoading: true,
  queueMessage: vi.fn(),
  streamSubmit: vi.fn(),
  confirmRunStart: vi.fn(),
}))

vi.mock("@langchain/react", () => ({
  useStreamContext: () => ({
    isLoading: mocks.isLoading,
    submit: mocks.streamSubmit,
  }),
}))

vi.mock("@/lib/agents/api", () => {
  class AgentsApiError extends Error {
    constructor(
      public readonly status: number,
      message: string
    ) {
      super(message)
    }
  }
  return {
    AgentsApiError,
    agentsApi: { queueMessage: mocks.queueMessage },
  }
})

vi.mock("@/lib/agents/AgentThreadStreamProvider", () => ({
  useAgentRunStartConfirmation: () => mocks.confirmRunStart,
}))

function createWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

describe("useSubmitAgentMessage busy queue", () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.isLoading = true
  })

  it("queues file attachments into the active run without starting another run", async () => {
    mocks.queueMessage.mockResolvedValue({ id: "thread-1" })
    const { result } = renderHook(() => useSubmitAgentMessage("thread-1"), {
      wrapper: createWrapper(),
    })
    const file = {
      kind: "file" as const,
      base64: "IyBJbnNpZ2h0cw==",
      mimeType: "text/markdown",
      fileName: "analysis.md",
    }

    await act(async () => {
      await result.current.mutateAsync({
        content: "Read the analysis",
        files: [file],
        model_id: "openai:gpt-5",
        effort: "high",
        plan_mode: false,
      })
    })

    expect(mocks.queueMessage).toHaveBeenCalledWith("thread-1", {
      content: "Read the analysis",
      images: undefined,
      files: [file],
      model_id: "openai:gpt-5",
      effort: "high",
      plan_mode: false,
    })
    expect(mocks.streamSubmit).not.toHaveBeenCalled()
    expect(mocks.confirmRunStart).not.toHaveBeenCalled()
  })
})
