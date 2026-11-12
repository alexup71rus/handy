import { isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";

/** Read only a successful MCP receipt; never infer creation from assistant prose. */
export function artifactLink(result: any) {
  let receipt = result.structuredContent?.structuredContent ?? result.structuredContent;
  if (receipt?.status !== "ok") {
    try {
      receipt = JSON.parse((result.content ?? []).filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n"));
    } catch { return; }
  }
  const artifact = receipt?.data?.artifact;
  if (receipt?.status !== "ok" || typeof artifact?.path !== "string" || !isAbsolute(artifact.path)) return;
  return { title: artifact.title || "Артефакт", path: artifact.path, uri: pathToFileURL(artifact.path).href };
}
