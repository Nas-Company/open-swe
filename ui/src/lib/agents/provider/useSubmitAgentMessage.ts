import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useStreamContext as useAgentThreadStream } from "@langchain/react"

import type { SendAgentMessageVariables } from "@/lib/agents/queries"
import { AgentsApiError, agentsApi } from "@/lib/agents/api"
import {
  agentThreadKeys,
  invalidateAgentThreadLists,
} from "@/lib/agents/queries"
import { buildAgentMessageContent } from "@/lib/agents/messageContent"
import { useAgentRunStartConfirmation } from "@/lib/agents/AgentThreadStreamProvider"

/**
 * User-initiated sends from the prompt bar. Prefer this over calling `stream.submit`
 * directly so cache updates and the busy-thread queue path stay consistent.
 *
 * When the thread is idle, submits a new run via the stream commands endpoint.
 * When a run is already in flight (`stream.isLoading`), posts to the dashboard
 * `/messages` endpoint instead of using LangGraph `multitaskStrategy: "enqueue"`.
 * That endpoint writes to the thread store; `check_message_queue_before_model`
 * injects the message into the *current* run before the next model call — the
 * same mid-run follow-up path used by Slack, Linear, and GitHub webhooks.
 *
 * @param threadId - The ID of the thread to submit the message to.
 * @returns The mutation object.
 */
export function useSubmitAgentMessage(threadId: string) {
  const queryClient = useQueryClient()
  const stream = useAgentThreadStream()
  const confirmRunStart = useAgentRunStartConfirmation()

  return useMutation({
    mutationFn: async (vars: SendAgentMessageVariables) => {
      const queue = () =>
        agentsApi.queueMessage(threadId, {
          content: vars.content,
          images: vars.images,
          files: vars.files,
          model_id: vars.model_id,
          effort: vars.effort,
          plan_mode: vars.plan_mode,
        })

      if (stream.isLoading) {
        await queue()
        return
      }

      try {
        await queue()
        return
      } catch (error) {
        if (!(error instanceof AgentsApiError) || error.status !== 409) {
          throw error
        }
      }

      const configurable: Record<string, unknown> = {}
      if (vars.model_id && vars.effort) {
        configurable.agent_model_id = vars.model_id
        configurable.agent_effort = vars.effort
      }
      if (vars.plan_mode) {
        configurable.plan_mode = true
      }
      const config =
        Object.keys(configurable).length > 0 ? { configurable } : undefined

      const markRunError = () => {
        queryClient.setQueryData(agentThreadKeys.detail(threadId), (prev) =>
          prev ? { ...prev, status: "error" as const } : prev
        )
        invalidateAgentThreadLists(queryClient)
      }

      // `stream.submit` resolves only when the run finishes. Wait instead for
      // the provider's `onCreated` callback so the prompt is cleared as soon as
      // the server accepts the run, while preserving it on dispatch failure.
      await confirmRunStart((rejectStart) =>
        stream
          .submit(
            {
              messages: [
                { type: "human", content: buildAgentMessageContent(vars) },
              ],
            },
            {
              config,
              onError: (error) => {
                markRunError()
                rejectStart(error)
              },
            }
          )
          .catch((error) => {
            markRunError()
            throw error
          })
      )
    },
    onSuccess: () => {
      queryClient.setQueryData(agentThreadKeys.detail(threadId), (prev) =>
        prev ? { ...prev, status: "running" as const } : prev
      )
      invalidateAgentThreadLists(queryClient)
    },
  })
}
