// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AgentsHome } from "./AgentsHome"

const mocks = vi.hoisted(() => ({
  createThreadRun: vi.fn(),
  navigate: vi.fn(),
}))

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => mocks.navigate,
}))

vi.mock("@/lib/agents/api", () => ({
  agentsApi: {
    createThreadRun: mocks.createThreadRun,
  },
}))

vi.mock("@/lib/agents/provider/useModelOptions", () => ({
  formatModelSelection: () => "Default",
  useModelOptions: () => ({
    models: [],
    defaultSelection: null,
    isLoading: false,
  }),
}))

vi.mock("@/lib/profile", () => ({
  useProfile: () => ({ data: { default_repo: null } }),
  useRepos: () => ({ data: { repositories: [] } }),
}))

vi.mock("@/lib/notifications", () => ({
  requestNotificationPermission: () => Promise.resolve("denied"),
  setNotificationsPref: vi.fn(),
}))

vi.mock("@/components/agents/OnboardingDialog", () => ({
  OnboardingDialog: () => null,
}))

vi.mock("@/components/agents/ported/Logo", () => ({
  Logo: () => null,
}))

function textFile(name: string, content = "# Insights"): File {
  const bytes = new TextEncoder().encode(content)
  const file = new File([bytes], name)
  Object.defineProperty(file, "arrayBuffer", {
    value: () => Promise.resolve(bytes.buffer),
  })
  return file
}

describe("AgentsHome submission failure", () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it("keeps the prompt and attachment when thread creation fails", async () => {
    mocks.createThreadRun.mockRejectedValueOnce(new Error("network failed"))
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <AgentsHome />
      </QueryClientProvider>
    )
    const fileInput =
      container.querySelector<HTMLInputElement>('input[type="file"]')
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox")

    fireEvent.change(fileInput!, {
      target: { files: [textFile("analysis.md")] },
    })
    await screen.findByText("analysis.md")
    fireEvent.change(textarea, { target: { value: "Build the report" } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))

    await waitFor(() => expect(mocks.createThreadRun).toHaveBeenCalledOnce())
    await waitFor(() => {
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Send message" })
          .disabled
      ).toBe(false)
    })
    expect(textarea.value).toBe("Build the report")
    expect(screen.queryByText("analysis.md")).not.toBeNull()
    expect(mocks.navigate).not.toHaveBeenCalled()
  })
})
