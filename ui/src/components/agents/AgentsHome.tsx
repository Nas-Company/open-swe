import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import type { FileChunk, ImageChunk } from "@/lib/agents/types"
import type { CreateAgentThreadVariables } from "@/lib/agents/queries"
import type { ModelSelection } from "@/lib/agents/provider/useModelOptions"
import { AgentPromptBar } from "@/components/agents/AgentPromptBar"
import { OnboardingDialog } from "@/components/agents/OnboardingDialog"
import { Logo } from "@/components/agents/ported/Logo"
import {
  agentThreadKeys,
  invalidateAgentThreadLists,
  optimisticThread,
  seedAgentThreadLists,
} from "@/lib/agents/queries"
import { agentsApi } from "@/lib/agents/api"
import { useModelOptions } from "@/lib/agents/provider/useModelOptions"
import { useProfile, useRepos } from "@/lib/profile"
import {
  requestNotificationPermission,
  setNotificationsPref,
} from "@/lib/notifications"

export function AgentsHome() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { models, defaultSelection } = useModelOptions()
  const [selection, setSelection] = useState<ModelSelection | null>(null)
  const activeSelection = selection ?? defaultSelection
  const [planMode, setPlanMode] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const reposQuery = useRepos()
  const profileQuery = useProfile()
  // undefined = untouched (fall back to the profile default); null = explicitly "no repo".
  const [repoOverride, setRepoOverride] = useState<string | null | undefined>(
    undefined
  )
  const repo =
    repoOverride === undefined
      ? (profileQuery.data?.default_repo ?? null)
      : repoOverride

  const handleSubmit = async (
    prompt: string,
    images: Array<ImageChunk>,
    files: Array<FileChunk>
  ) => {
    void requestNotificationPermission().then((perm) => {
      if (perm === "granted") setNotificationsPref(true)
    })
    setSubmitting(true)
    const draft: CreateAgentThreadVariables = {
      prompt,
      images,
      files,
      repo,
      repo_explicitly_none: repoOverride === null,
      model_id: activeSelection?.modelId ?? null,
      effort: activeSelection?.effort ?? null,
      plan_mode: planMode,
    }
    try {
      const thread = await agentsApi.createThreadRun({
        content: draft.prompt,
        images: draft.images,
        files: draft.files,
        repo: draft.repo,
        repo_explicitly_none: draft.repo_explicitly_none,
        model_id: draft.model_id,
        effort: draft.effort,
        plan_mode: draft.plan_mode,
      })
      const visibleThread = {
        ...thread,
        messages: optimisticThread(thread.id, draft).messages,
      }
      queryClient.setQueryData(agentThreadKeys.detail(thread.id), visibleThread)
      seedAgentThreadLists(queryClient, visibleThread)
      invalidateAgentThreadLists(queryClient)
      void navigate({
        to: "/agents/$threadId",
        params: { threadId: thread.id },
      })
    } catch (error) {
      setSubmitting(false)
      throw error
    }
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-y-auto px-6 py-8">
      <OnboardingDialog />
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center">
        <div className="flex w-full flex-col items-center gap-6">
          <Logo />
          <AgentPromptBar
            onSubmit={handleSubmit}
            disabled={submitting}
            models={models}
            selection={activeSelection}
            onSelectionChange={setSelection}
            repos={reposQuery.data?.repositories}
            selectedRepo={repo}
            onRepoChange={setRepoOverride}
            planMode={planMode}
            onPlanModeChange={setPlanMode}
          />
        </div>
      </div>
    </div>
  )
}
