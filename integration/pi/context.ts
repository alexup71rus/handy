import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

export const TOOL_TEXT_BYTES = 8192;

/** Bound model-visible output while retaining the original for deliberate reading. */
export async function boundToolResult(event: any, state: string) {
  const blocks = event.content ?? [];
  const full = blocks.filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n");
  const structured = event.structuredContent === undefined ? "" : JSON.stringify(event.structuredContent);
  if (Buffer.byteLength(full) <= TOOL_TEXT_BYTES && Buffer.byteLength(structured) <= TOOL_TEXT_BYTES) return;
  const directory = join(state, "pi", "tool-output");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const path = join(directory, randomUUID() + ".txt");
  await writeFile(path, full || structured, { mode: 0o600 });
  const notice = `\n[Вывод сокращён. Полный результат: ${path}. Читай нужный фрагмент через read с offset/limit.]`;
  const budget = TOOL_TEXT_BYTES - Buffer.byteLength(notice);
  const preview = Buffer.from(full || structured).subarray(0, Math.max(0, budget)).toString("utf8").replace(/\uFFFD$/, "");
  return { content: [...blocks.filter((part: any) => part.type !== "text"), { type: "text", text: preview + notice }] };
}
