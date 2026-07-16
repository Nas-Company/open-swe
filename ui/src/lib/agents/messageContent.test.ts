import { describe, expect, it } from "vitest"

import { buildAgentMessageContent } from "./messageContent"

describe("buildAgentMessageContent", () => {
  it("serializes text files with the backend block shape", () => {
    expect(
      buildAgentMessageContent({
        content: " Build the report ",
        files: [
          {
            kind: "file",
            base64: "IyBJbnNpZ2h0cw==",
            mimeType: "text/markdown",
            fileName: "analysis.md",
          },
        ],
      })
    ).toEqual([
      {
        type: "file",
        base64: "IyBJbnNpZ2h0cw==",
        mime_type: "text/markdown",
        file_name: "analysis.md",
      },
      { type: "text", text: "Build the report" },
    ])
  })

  it("keeps the existing image block shape", () => {
    expect(
      buildAgentMessageContent({
        content: "",
        images: [
          {
            kind: "image",
            base64: "aW1hZ2U=",
            mimeType: "image/png",
            fileName: "preview.png",
          },
        ],
      })
    ).toEqual([
      {
        type: "image",
        base64: "aW1hZ2U=",
        mime_type: "image/png",
        file_name: "preview.png",
      },
    ])
  })
})
