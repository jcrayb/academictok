export type Theme = "light" | "dark";

const KEY = "theme";

export function getTheme(): Theme {
  return localStorage.getItem(KEY) === "light" ? "light" : "dark";
}

export function setTheme(theme: Theme) {
  localStorage.setItem(KEY, theme);
  document.documentElement.setAttribute("data-theme", theme);
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === "light" ? "dark" : "light";
  setTheme(next);
  return next;
}
