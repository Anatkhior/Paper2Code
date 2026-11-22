"use client";

import { useEffect, useState } from "react";
import Icon, { type IconName } from "@/components/Icon";
import { THEME_STORAGE_KEY } from "@/lib/providerStorage";

type ThemeMode = "system" | "light" | "dark";

const ORDER: ThemeMode[] = ["system", "light", "dark"];
const META: Record<ThemeMode, { label: string; icon: IconName }> = {
  system: { label: "外观：跟随系统", icon: "monitor" },
  light: { label: "外观：浅色", icon: "sun" },
  dark: { label: "外观：深色", icon: "moon" },
};

function applyTheme(mode: ThemeMode) {
  const root = document.documentElement;
  if (mode === "system") delete root.dataset.theme;
  else root.dataset.theme = mode;
}

export default function ThemeToggle() {
  const [mode, setMode] = useState<ThemeMode>("system");

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (saved === "light" || saved === "dark") {
        queueMicrotask(() => setMode(saved));
        applyTheme(saved);
      }
    } catch { /* ignore storage errors */ }
  }, []);

  const cycle = () => {
    const next = ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
    setMode(next);
    applyTheme(next);
    try {
      if (next === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
      else window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch { /* ignore storage errors */ }
  };

  const current = META[mode];
  return (
    <button
      type="button"
      onClick={cycle}
      aria-label={`${current.label}，点击切换外观`}
      title={`${current.label}（点击切换）`}
      className="quiet-button text-muted"
    >
      <Icon name={current.icon} size={14} />
      <span className="hidden sm:inline">{mode === "system" ? "系统" : mode === "light" ? "浅色" : "深色"}</span>
    </button>
  );
}
