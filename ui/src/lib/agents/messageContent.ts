import type { SendAgentMessageVariables } from "./queries"

export function buildAgentMessageContent(vars: SendAgentMessageVariables) {
  const text = vars.content.trim()
  const imageBlocks =
    vars.images?.map((image) => ({
      type: "image",
      base64: image.base64,
      mime_type: image.mimeType,
      ...(image.fileName ? { file_name: image.fileName } : {}),
    })) ?? []
  const fileBlocks =
    vars.files?.map((file) => ({
      type: "file",
      base64: file.base64,
      mime_type: file.mimeType,
      file_name: file.fileName,
    })) ?? []
  return [
    ...imageBlocks,
    ...fileBlocks,
    ...(text ? [{ type: "text", text }] : []),
  ]
}
