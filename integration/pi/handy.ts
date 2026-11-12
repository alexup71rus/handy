import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { hyperlink, Text, truncateToWidth } from "@earendil-works/pi-tui";
import { basename } from "node:path";
import { boundToolResult } from "./context.ts";
import { registerModelPicker } from "./models.ts";
import { artifactLink } from "./artifact-links.ts";

export default function handy(pi: ExtensionAPI) {
  registerModelPicker(pi);
  const state = process.env.HANDY_STATE_DIR!;
  const python = process.env.HANDY_PYTHON || "python3";
  const args = process.argv.slice(2);
  const options = args.includes("--") ? args.slice(0, args.indexOf("--")) : args;
  const explicitChoice = options.some((arg, index) =>
    ["--continue", "-c", "--resume", "-r"].includes(arg) ||
    ["--provider", "--model", "--session"].includes(arg) && index + 1 < options.length);
  let requiresSelection = process.env.HANDY_REQUIRE_MODEL_SELECTION === "1" && !explicitChoice;
  const needsSelection = (ctx: { model?: { provider: string } }) => requiresSelection && !ctx.model?.provider.startsWith("handy-");
  pi.on("model_select", event => {
    if (event.source === "set" || event.source === "cycle") requiresSelection = false;
  });
  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode === "tui") process.stdout.write("\x1b[6 q");
    if (ctx.hasUI) ctx.ui.setTitle(`Handy · ${basename(ctx.cwd)}`);
    if (ctx.hasUI && (!ctx.model || needsSelection(ctx))) ctx.ui.notify("Выбери модель: /models. Для системных моделей Handy можно скачать веса.", "info");
    if (ctx.hasUI) ctx.ui.setHeader((_tui, theme) => ({
      render: (width: number) => [truncateToWidth(theme.fg("accent", "h›") + theme.bold(" Handy") + theme.fg("dim", "   /models · /handy-settings"), width)],
      invalidate() {},
    }));
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (ctx.mode === "tui") process.stdout.write("\x1b[0 q");
  });
  // Pi's model IDs stay distinct; the app-owned llama.cpp endpoint has one alias.
  pi.on("before_provider_request", (event, ctx) => {
    if (ctx.model?.provider === "handy-system" && event.payload && typeof event.payload === "object") {
      return { ...event.payload, model: "memory-engine" };
    }
  });
  pi.on("before_agent_start", async (event, ctx) => {
    if (needsSelection(ctx)) {
      ctx.abort();
      throw new Error("Модель Handy не выбрана. Выбери провайдера и модель через /models.");
    }
    if (pi.getActiveTools().includes("mcp__handy__artifact_create")) {
      const guidance = "Use Handy artifact_create for its supported widgets: clock shows live time in an IANA time_zone; timer counts down seconds. Return the saved artifact as a clickable file link only after the tool succeeds. Use custom HTML for widgets the artifact tool does not support.";
      if (!event.systemPromptOptions.promptGuidelines.includes(guidance)) event.systemPromptOptions.promptGuidelines.push(guidance);
    }
    if (ctx.model?.provider !== "handy-system") return;
    try {
      const result = await pi.exec(python, ["-m", "folder_memory.pi_control", "--state-dir", state, "select-model", "--profile", ctx.model.id],
        { signal: ctx.signal, timeout: 30000, maxBuffer: 8192 });
      if (result.code !== 0) throw new Error(result.stderr || "Не удалось запустить выбранную модель Handy");
    } catch (error) {
      ctx.abort();
      throw error;
    }
  });
  pi.on("tool_result", async (event) => boundToolResult(event, state));
  pi.registerToolRenderer((name, next) => {
    if (name !== "mcp__handy__artifact_create") return next();
    const original = next();
    return {
      ...original,
      renderResult(result, options, theme, context) {
        const link = artifactLink(result);
        if (!link) return original?.renderResult?.(result, options, theme, context) ?? new Text(
          result.content.filter(part => part.type === "text").map(part => part.text).join("\n"), 0, 0,
        );
        return new Text(theme.fg("success", "Артефакт создан") + "\n" + hyperlink(`Открыть «${link.title}»`, link.uri) + "\n" +
          theme.fg("dim", link.path.replace(/[\x00-\x1f\x7f]/g, "�")), 0, 0);
      },
    };
  });
  pi.registerCommand("handy-settings", {
    description: "Открыть настройки Handy",
    handler: async (_args, ctx) => {
      const result = await pi.exec(python, ["-m", "folder_memory.pi_control", "--state-dir", state, "open-settings"]);
      if (result.code !== 0 && ctx.hasUI) ctx.ui.notify(result.stderr || "Не удалось открыть настройки", "error");
    },
  });
}
