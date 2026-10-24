type Store = Pick<Storage, "getItem" | "setItem">;
export const PROVIDER_STORAGE_KEY = "paper2code.provider.v1";
export const TIMELINE_STORAGE_KEY = "paper2code.timelineOpen";

/** 仅首次迁移旧名称；没有明确 remember_key=true 的密钥绝不写入新存储。 */
export function migrateStorage(storage: Store) {
  if (storage.getItem(PROVIDER_STORAGE_KEY) === null) {
    const raw = storage.getItem("paperlens.provider.v1");
    if (raw) {
      try {
        const old = JSON.parse(raw);
        if (old && typeof old === "object") {
          const text = (value: unknown) => typeof value === "string" ? value : "";
          storage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify({
            protocol: old.protocol === "anthropic" ? "anthropic" : "openai-compatible",
            base_url: text(old.base_url), model: text(old.model),
            remember_key: old.remember_key === true,
            api_key: old.remember_key === true ? text(old.api_key) : "",
          }));
        }
      } catch { /* 旧数据损坏时保持空表单，不把损坏数据复制到新键。 */ }
    }
  }
  if (storage.getItem(TIMELINE_STORAGE_KEY) === null) {
    const old = storage.getItem("paperlens.timelineOpen");
    if (old !== null) storage.setItem(TIMELINE_STORAGE_KEY, old);
  }
}
