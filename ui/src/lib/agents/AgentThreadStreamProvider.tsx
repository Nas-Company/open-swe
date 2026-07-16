import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
} from "react"
import { StreamProvider } from "@langchain/react"
import { overrideFetchImplementation } from "@langchain/langgraph-sdk"
import { useQueryClient } from "@tanstack/react-query"

import { agentsApi } from "./api"
import { agentThreadKeys, invalidateAgentThreadLists } from "./queries"
import type { ReactNode } from "react"

const AGENT_ASSISTANT_ID = "agent"
const RUN_START_CONFIRMATION_TIMEOUT_MS = 35_000

type RunStartErrorHandler = (error: unknown) => void
type RunStarter = (onError: RunStartErrorHandler) => Promise<void>
type ConfirmRunStart = (start: RunStarter) => Promise<void>

interface RunStartWaiter {
  resolve: () => void
  reject: RunStartErrorHandler
}

const AgentRunStartConfirmationContext = createContext<ConfirmRunStart | null>(
  null
)

export function useAgentRunStartConfirmation(): ConfirmRunStart {
  const confirmRunStart = useContext(AgentRunStartConfirmationContext)
  if (!confirmRunStart) {
    throw new Error(
      "useAgentRunStartConfirmation must be used inside AgentThreadStreamProvider"
    )
  }
  return confirmRunStart
}

const dashboardFetch: typeof fetch = (input, init) =>
  fetch(input, { ...init, credentials: "include" })

/**
 * We use the SDK's built-in `sse` transport (via {@link StreamProvider}'s
 * `apiUrl` + `fetch`), so commands, the event stream, and `getState`
 * hydration all flow through {@link dashboardFetch}. But subagent/subgraph
 * discovery on hydrate (`POST /threads/:id/history`) and `getState` itself
 * are issued by the SDK's internal `Client` rather than the transport's
 * `fetch`. Without this, the `Client` falls back to a bare `fetch` that
 * omits the dashboard session cookie cross-origin, so the proxy rejects the
 * read with `401 "not authenticated"`. Override the SDK's global fetch so
 * every `Client` read carries the same credentials as the transport.
 */
overrideFetchImplementation(dashboardFetch)

/**
 * The SDK transport builds request URLs as `new URL(apiUrl + path)`, so
 * `apiUrl` must be absolute — a relative base (e.g. "/dashboard/api")
 * makes the SDK fall back to the LangGraph default host
 * (`http://localhost:8123`) and drop the proxy prefix. Promote a
 * same-origin base to an absolute URL using the current origin.
 */
function toAbsoluteApiUrl(url: string): string {
  if (/^https?:\/\//.test(url)) return url
  if (typeof window !== "undefined") {
    return `${window.location.origin}${url.startsWith("/") ? "" : "/"}${url}`
  }
  return url
}

const agentStreamApiUrl = toAbsoluteApiUrl(agentsApi.langGraphApiUrl)

/**
 * One persistent stream for the whole `/agents` subtree, mounted by the
 * layout so it survives the home → thread navigation. The built-in `sse`
 * transport (default `apiUrl` branch) is reused across thread switches —
 * changing `threadId` re-hydrates the same controller instead of tearing
 * down a per-thread transport — which is what lets a home-page
 * `stream.submit` keep streaming after we navigate to the minted thread.
 */
export function AgentThreadStreamProvider({
  threadId,
  children,
}: {
  /**
   * The active thread, or `null` on routes without one (the Agents home,
   * automations). A `null` id leaves the SDK in its lazy-create mode: the
   * first `stream.submit` mints the thread id, fires `onThreadId`, and skips
   * the `getState` hydrate — so a fresh thread needs no client-minted id and
   * no `getState` 404 round-trip.
   */
  threadId: string | null
  children: ReactNode
}) {
  const queryClient = useQueryClient()
  const runStartWaitersRef = useRef<Array<RunStartWaiter>>([])

  // The SDK captures the lifecycle callbacks once at controller creation, so
  // they must be stable. Read the live thread id from a ref instead of
  // closing over the (changing) prop.
  const threadIdRef = useRef<string | null>(threadId)
  threadIdRef.current = threadId

  const confirmRunStart = useCallback<ConfirmRunStart>((start) => {
    return new Promise<void>((resolve, reject) => {
      let settled = false
      const waiter: RunStartWaiter = {
        resolve: () => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          runStartWaitersRef.current = runStartWaitersRef.current.filter(
            (candidate) => candidate !== waiter
          )
          resolve()
        },
        reject: (error) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          runStartWaitersRef.current = runStartWaitersRef.current.filter(
            (candidate) => candidate !== waiter
          )
          reject(
            error instanceof Error
              ? error
              : new Error("The agent run could not be started")
          )
        },
      }

      runStartWaitersRef.current.push(waiter)
      const timer = setTimeout(() => {
        waiter.reject(new Error("Timed out waiting for the agent run to start"))
      }, RUN_START_CONFIRMATION_TIMEOUT_MS)

      try {
        void start(waiter.reject).catch(waiter.reject)
      } catch (error) {
        waiter.reject(error)
      }
    })
  }, [])

  useEffect(() => {
    return () => {
      const waiters = runStartWaitersRef.current
      runStartWaitersRef.current = []
      for (const waiter of waiters) {
        waiter.reject(new Error("Agent stream closed before the run started"))
      }
    }
  }, [])

  const onCreated = useCallback(() => {
    runStartWaitersRef.current[0]?.resolve()
    invalidateAgentThreadLists(queryClient)
  }, [queryClient])

  const onCompleted = useCallback(() => {
    const id = threadIdRef.current
    if (id) {
      void queryClient.invalidateQueries({
        queryKey: agentThreadKeys.detail(id),
      })
    }
    invalidateAgentThreadLists(queryClient)
  }, [queryClient])

  return (
    <AgentRunStartConfirmationContext.Provider value={confirmRunStart}>
      <StreamProvider
        apiUrl={agentStreamApiUrl}
        assistantId={AGENT_ASSISTANT_ID}
        fetch={dashboardFetch}
        threadId={threadId ?? undefined}
        onCreated={onCreated}
        onCompleted={onCompleted}
      >
        {children}
      </StreamProvider>
    </AgentRunStartConfirmationContext.Provider>
  )
}
