// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { useState } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  AgentThreadStreamProvider,
  useAgentRunStartConfirmation,
} from "./AgentThreadStreamProvider"
import type { ReactNode } from "react"

interface CapturedStreamProviderProps {
  children: ReactNode
  onCreated?: (info: { runId: string }) => void
}

const captured = vi.hoisted(() => ({
  streamProviderProps: null as CapturedStreamProviderProps | null,
}))

vi.mock("@langchain/react", () => ({
  StreamProvider: (props: CapturedStreamProviderProps) => {
    captured.streamProviderProps = props
    return props.children
  },
}))

function ConfirmationHarness({
  start,
}: {
  start: (onError: (error: unknown) => void) => Promise<void>
}) {
  const confirmRunStart = useAgentRunStartConfirmation()
  const [status, setStatus] = useState("idle")

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setStatus("pending")
          void confirmRunStart(start).then(
            () => setStatus("confirmed"),
            () => setStatus("failed")
          )
        }}
      >
        Start
      </button>
      <span>{status}</span>
    </>
  )
}

function renderHarness(
  start: (onError: (error: unknown) => void) => Promise<void>
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AgentThreadStreamProvider threadId="thread-1">
        <ConfirmationHarness start={start} />
      </AgentThreadStreamProvider>
    </QueryClientProvider>
  )
}

describe("AgentThreadStreamProvider run-start confirmation", () => {
  afterEach(() => {
    cleanup()
    captured.streamProviderProps = null
  })

  it("resolves only after the stream reports that the run was created", async () => {
    renderHarness(() => new Promise<void>(() => undefined))

    fireEvent.click(screen.getByRole("button", { name: "Start" }))
    expect(screen.getByText("pending")).not.toBeNull()

    act(() => {
      captured.streamProviderProps?.onCreated?.({ runId: "run-1" })
    })

    await screen.findByText("confirmed")
  })

  it("rejects when the stream reports a dispatch error", async () => {
    let reportError: ((error: unknown) => void) | undefined
    renderHarness((onError) => {
      reportError = onError
      return new Promise<void>(() => undefined)
    })

    fireEvent.click(screen.getByRole("button", { name: "Start" }))
    await waitFor(() => expect(reportError).toBeTypeOf("function"))
    act(() => reportError?.(new Error("dispatch failed")))

    await screen.findByText("failed")
  })
})
