import { HumanMessage } from "@langchain/core/messages"
import { describe, expect, it } from "vitest"

import { streamMessagesToUi } from "./streamMessagesToUi"

describe("streamMessagesToUi file attachments", () => {
  it("preserves file blocks as user-facing chunks", () => {
    const messages = streamMessagesToUi([
      new HumanMessage({
        content: [
          {
            type: "file",
            base64: "IyBJbnNpZ2h0cw==",
            mime_type: "text/markdown",
            file_name: "analysis.md",
          },
          { type: "text", text: "Build the report" },
        ],
      }),
    ])

    expect(messages).toHaveLength(1)
    expect(messages[0]?.chunks).toEqual([
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
