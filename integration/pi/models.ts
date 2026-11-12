import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type ModelChoice = {
  id: string;
  name: string;
  ready: boolean;
  downloadable: boolean;
  download_bytes: number;
};
type ModelGroup = { id: string; name: string; system: boolean; models: ModelChoice[] };
type Inventory = { groups: ModelGroup[]; selected: string | null };
type Selection = { id: string; name: string; pi_provider: string; pi_model: string };

function uniqueLabels(labels: string[], identifiers: string[]): string[] {
  return labels.map((label, index) => labels.indexOf(label) !== labels.lastIndexOf(label) ? `${label} · ${identifiers[index]}` : label);
}

function sizeLabel(bytes: number): string {
  if (!bytes) return "";
  const gb = bytes / 1_000_000_000;
  return gb >= 1 ? `${gb.toFixed(1)} ГБ` : `${Math.ceil(bytes / 1_000_000)} МБ`;
}

export function registerModelPicker(pi: ExtensionAPI) {
  async function backend<T>(operation: string, selection?: { id: string; download: boolean }): Promise<T> {
    const args = ["-m", "folder_memory.pi_control", "--state-dir", process.env.HANDY_STATE_DIR!, operation];
    if (selection) {
      args.push("--profile", selection.id);
      if (selection.download) args.push("--download");
    }
    const result = await pi.exec(process.env.HANDY_PYTHON || "python3", args, {
      timeout: selection?.download ? 6 * 60 * 60 * 1000 : 30_000,
      maxBuffer: 512 * 1024,
    });
    if (result.code !== 0) throw new Error(result.stderr.trim() || "Не удалось обновить модели Handy.");
    if (result.truncated) throw new Error("Список моделей слишком большой.");
    return JSON.parse(result.stdout) as T;
  }

  pi.registerCommand("models", {
    description: "Выбрать провайдера и модель Handy",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) throw new Error("Выбор моделей доступен в интерактивном Handy.");
      try {
        const inventory = await backend<Inventory>("model-inventory");
        if (!inventory.groups.length) {
          ctx.ui.notify("Добавь или покажи модели в настройках Handy: /handy-settings", "info");
          return;
        }
        const names = uniqueLabels(inventory.groups.map(group => `${group.name} · ${group.models.length}`), inventory.groups.map(group => group.id));
        const provider = await ctx.ui.select("Провайдер", names);
        if (provider === undefined) return;
        const group = inventory.groups[names.indexOf(provider)];
        const choices = uniqueLabels(group.models.map(model => {
          const status = model.ready ? "" : model.downloadable ? ` — скачать ${sizeLabel(model.download_bytes)}` : " — не настроена";
          return `${model.id === inventory.selected ? "● " : ""}${model.name}${status}`;
        }), group.models.map(model => model.id));
        const choice = await ctx.ui.select(group.name, choices);
        if (choice === undefined) return;
        const selected = group.models[choices.indexOf(choice)];
        if (!selected.ready && !selected.downloadable) {
          ctx.ui.notify("Проверь файлы модели или ключ провайдера в настройках Handy.", "warning");
          return;
        }
        const download = !selected.ready;
        if (download && !await ctx.ui.confirm("Скачать модель?", `${selected.name}${selected.download_bytes ? ` · ${sizeLabel(selected.download_bytes)}` : ""}. Файлы останутся на компьютере.`)) return;
        ctx.ui.setWorkingMessage(download ? "Скачиваю и проверяю модель…" : "Выбираю модель…");
        const result = await backend<Selection>("select-model", { id: selected.id, download });
        await ctx.modelRegistry.refresh();
        const model = ctx.modelRegistry.find(result.pi_provider, result.pi_model);
        if (!model || !await pi.setModel(model)) throw new Error("Модель сохранена, но pi не смог её подключить. Проверь настройки Handy.");
        ctx.ui.notify(`Выбрана ${result.name}`, "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      } finally {
        ctx.ui.setWorkingMessage();
      }
    },
  });
}
