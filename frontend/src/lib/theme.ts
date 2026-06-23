// Gruvbox palette (https://github.com/morhetz/gruvbox) split into an
// independently-chosen background brightness and accent color, composed at
// runtime as CSS custom properties on <html>. See Base.astro's inline
// pre-paint script for the no-FOUC duplicate of this lookup.

export type BackgroundId =
  | "very-dark"
  | "dark-hard"
  | "dark"
  | "dark-soft"
  | "light-soft"
  | "light"
  | "light-hard"
  | "very-light";

export type AccentId = "red" | "orange" | "yellow" | "green" | "aqua" | "blue" | "purple";

export interface BackgroundInfo {
  id: BackgroundId;
  label: string;
  mode: "dark" | "light";
  bg: string;
  card: string;
  border: string;
  text: string;
  muted: string;
}

export interface AccentInfo {
  id: AccentId;
  label: string;
  dark: string; // bright variant, used on dark backgrounds
  light: string; // neutral variant, used on light backgrounds
}

export const BACKGROUNDS: BackgroundInfo[] = [
  { id: "very-dark", label: "Very Dark", mode: "dark", bg: "#141617", card: "#1d2021", border: "#282828", text: "#ebdbb2", muted: "#a89984" },
  { id: "dark-hard", label: "Dark Hard", mode: "dark", bg: "#1d2021", card: "#282828", border: "#3c3836", text: "#ebdbb2", muted: "#a89984" },
  { id: "dark", label: "Dark", mode: "dark", bg: "#282828", card: "#3c3836", border: "#504945", text: "#ebdbb2", muted: "#a89984" },
  { id: "dark-soft", label: "Dark Soft", mode: "dark", bg: "#32302f", card: "#45403d", border: "#5a524d", text: "#ebdbb2", muted: "#a89984" },
  { id: "light-soft", label: "Light Soft", mode: "light", bg: "#f2e5bc", card: "#e5d3a8", border: "#d5c4a1", text: "#3c3836", muted: "#7c6f64" },
  { id: "light", label: "Light", mode: "light", bg: "#fbf1c7", card: "#ebdbb2", border: "#d5c4a1", text: "#3c3836", muted: "#7c6f64" },
  { id: "light-hard", label: "Light Hard", mode: "light", bg: "#f9f5d7", card: "#ebdbb2", border: "#d5c4a1", text: "#3c3836", muted: "#7c6f64" },
  { id: "very-light", label: "Very Light", mode: "light", bg: "#fffdf5", card: "#fbf8ef", border: "#e8e3d3", text: "#3c3836", muted: "#7c6f64" },
];

export const ACCENTS: AccentInfo[] = [
  { id: "red", label: "Red", dark: "#fb4934", light: "#cc241d" },
  { id: "orange", label: "Orange", dark: "#fe8019", light: "#d65d0e" },
  { id: "yellow", label: "Yellow", dark: "#fabd2f", light: "#d79921" },
  { id: "green", label: "Green", dark: "#b8bb26", light: "#98971a" },
  { id: "aqua", label: "Aqua", dark: "#8ec07c", light: "#689d6a" },
  { id: "blue", label: "Blue", dark: "#83a598", light: "#458588" },
  { id: "purple", label: "Purple", dark: "#d3869b", light: "#b16286" },
];

const BG_KEY = "theme-bg";
const ACCENT_KEY = "theme-accent";
const DEFAULT_BG: BackgroundId = "light-soft";
const DEFAULT_ACCENT: AccentId = "blue";

function migrateOldTheme(): BackgroundId | null {
  const old = localStorage.getItem("theme");
  if (!old) return null;
  const map: Record<string, BackgroundId> = {
    "gruvbox-dark": "dark",
    "gruvbox-dark-soft": "dark-soft",
    "gruvbox-dark-hard": "dark-hard",
    "gruvbox-light": "light",
    "gruvbox-light-soft": "light-soft",
    "gruvbox-light-hard": "light-hard",
    light: "light",
    dark: "dark",
  };
  return map[old] ?? null;
}

export function getBackground(): BackgroundId {
  const stored = localStorage.getItem(BG_KEY);
  if (BACKGROUNDS.some((b) => b.id === stored)) return stored as BackgroundId;
  return migrateOldTheme() ?? DEFAULT_BG;
}

export function getAccent(): AccentId {
  const stored = localStorage.getItem(ACCENT_KEY);
  if (ACCENTS.some((a) => a.id === stored)) return stored as AccentId;
  return DEFAULT_ACCENT;
}

export function applyTheme(bgId: BackgroundId, accentId: AccentId) {
  const bg = BACKGROUNDS.find((b) => b.id === bgId) ?? BACKGROUNDS[2];
  const accent = ACCENTS.find((a) => a.id === accentId) ?? ACCENTS[1];
  const ok = bg.mode === "dark" ? "#b8bb26" : "#79740e";
  const bad = bg.mode === "dark" ? "#fb4934" : "#9d0006";
  const style = document.documentElement.style;
  style.setProperty("--color-bg", bg.bg);
  style.setProperty("--color-card", bg.card);
  style.setProperty("--color-border", bg.border);
  style.setProperty("--color-text", bg.text);
  style.setProperty("--color-muted", bg.muted);
  style.setProperty("--color-accent", bg.mode === "dark" ? accent.dark : accent.light);
  style.setProperty("--color-ok", ok);
  style.setProperty("--color-bad", bad);
}

export function setBackground(bgId: BackgroundId) {
  localStorage.setItem(BG_KEY, bgId);
  applyTheme(bgId, getAccent());
}

export function setAccent(accentId: AccentId) {
  localStorage.setItem(ACCENT_KEY, accentId);
  applyTheme(getBackground(), accentId);
}
