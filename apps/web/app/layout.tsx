import "./globals.css";
import type { Metadata } from "next";

import { ThemeController } from "./components/theme-controller";
import { defaultThemeId, themeOptions, themeStorageKey } from "./theme-config";

export const metadata: Metadata = {
  title: "FilmIt Pipeline",
  description: "v1.0.0 workflow console",
};

const themeBootstrapScript = `
(() => {
  const storageKey = ${JSON.stringify(themeStorageKey)};
  const fallbackTheme = ${JSON.stringify(defaultThemeId)};
  const allowedThemes = ${JSON.stringify(themeOptions.map((theme) => theme.id))};

  try {
    const savedTheme = window.localStorage.getItem(storageKey);
    const themeId = savedTheme && allowedThemes.includes(savedTheme) ? savedTheme : fallbackTheme;
    document.documentElement.dataset.theme = themeId;
    document.documentElement.style.colorScheme = themeId.endsWith("dark") ? "dark" : "light";
  } catch {
    document.documentElement.dataset.theme = fallbackTheme;
    document.documentElement.style.colorScheme = fallbackTheme.endsWith("dark") ? "dark" : "light";
  }
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-theme={defaultThemeId} suppressHydrationWarning>
      <body>
        <script id="theme-bootstrap" dangerouslySetInnerHTML={{ __html: themeBootstrapScript }} />
        <ThemeController>{children}</ThemeController>
      </body>
    </html>
  );
}
