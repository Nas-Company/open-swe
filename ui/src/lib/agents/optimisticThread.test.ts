import { describe, expect, it } from "vitest"

import { optimisticThread } from "./queries"

describe("optimisticThread", () => {
  it("shows file attachments before the first stream snapshot arrives", () => {
    const thread = optimisticThread("thread-1", {
      prompt: "Build the report",
      repo: "Nas-Company/nas-reporting",
      files: [
        {
          kind: "file",
          base64: "IyBJbnNpZ2h0cw==",
          mimeType: "text/markdown",
          fileName: "analysis.md",
        },
      ],
    })

    expect(thread.messages[0]?.chunks).toEqual([
      {
        kind: "file",
        base64: "IyBJbnNpZ2h0cw==",
        mimeType: "text/markdown",
        fileName: "analysis.md",
      },
      { kind: "text", text: "Build the report" },
    ])
  })
})
