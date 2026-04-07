"use client";

import type { CSSProperties, ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useState } from "react";

import {
  defaultThemeId,
  isDarkTheme,
  resolveThemeId,
  themeGroups,
  themeOptions,
  themeStorageKey,
  type ThemeId,
} from "../theme-config";

function applyTheme(themeId: ThemeId) {
  document.documentElement.dataset.theme = themeId;
  document.documentElement.style.colorScheme = isDarkTheme(themeId) ? "dark" : "light";
}

type ThemeContextValue = {
  activeTheme: (typeof themeOptions)[number];
  themeId: ThemeId;
  setThemeId: (themeId: ThemeId) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within ThemeController");
  }
  return context;
}

export function ThemeController({ children }: { children: ReactNode }) {
  const [themeId, setCurrentThemeId] = useState<ThemeId>(defaultThemeId);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const savedTheme = resolveThemeId(window.localStorage.getItem(themeStorageKey));
    setCurrentThemeId(savedTheme);
    applyTheme(savedTheme);
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    applyTheme(themeId);
    window.localStorage.setItem(themeStorageKey, themeId);
  }, [hydrated, themeId]);

  const activeTheme = themeOptions.find((item) => item.id === themeId) ?? themeOptions[0];
  const contextValue = useMemo(
    () => ({
      activeTheme,
      themeId,
      setThemeId: (nextThemeId: ThemeId) => setCurrentThemeId(resolveThemeId(nextThemeId)),
    }),
    [activeTheme, themeId]
  );

  return (
    <ThemeContext.Provider value={contextValue}>
      {children}
      <details className="themeDock" data-testid="theme-switcher">
        <summary className="themeDockSummary">
          <span className="themeDockSummaryLabel">Theme</span>
          <strong>{activeTheme.label}</strong>
        </summary>
        <div className="themeDockPanel">
          <div className="themeDockHeader">
            <div>
              <p className="eyebrow">UI Theme</p>
              <strong>背景与色板</strong>
            </div>
            <span className="pill">{activeTheme.family}</span>
          </div>
          <label className="themeSelectLabel">
            <span className="muted">整体风格</span>
            <select
              value={themeId}
              onChange={(event) => setCurrentThemeId(resolveThemeId(event.target.value))}
              data-testid="theme-switcher-select"
            >
              {themeGroups.map((group) => (
                <optgroup key={group.group} label={group.group}>
                  {group.options.map((theme) => (
                    <option key={theme.id} value={theme.id}>
                      {theme.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
          <div className="themeOptionGrid">
            {themeOptions.map((theme) => {
              const previewStyle: CSSProperties = { background: theme.preview };
              return (
                <button
                  key={theme.id}
                  type="button"
                  className="themeOption"
                  data-active={theme.id === themeId ? "true" : "false"}
                  onClick={() => setCurrentThemeId(theme.id)}
                >
                  <span className="themeOptionPreview" style={previewStyle} aria-hidden="true" />
                  <span className="themeOptionText">
                    <strong>{theme.label}</strong>
                    <small>{theme.description}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </details>
    </ThemeContext.Provider>
  );
}
