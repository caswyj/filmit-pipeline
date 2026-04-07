export const themeStorageKey = "filmit-ui-theme";
export const defaultThemeId = "default-light";

export type ThemeOption = {
  id: string;
  label: string;
  family: string;
  group: string;
  description: string;
  preview: string;
  inspiration: string;
};

export const themeOptions = [
  {
    id: "default-light",
    label: "Default Light",
    family: "FilmIt",
    group: "FilmIt Core",
    description: "暖色纸感背景，适合日常审片与编辑。",
    preview: "linear-gradient(135deg, #fff6e8 0%, #f5efe3 52%, #e7dfd2 100%)",
    inspiration: "FilmIt 原生暖色工作台",
  },
  {
    id: "default-dark",
    label: "Default Dark",
    family: "FilmIt",
    group: "FilmIt Core",
    description: "克制深蓝暗面，保留工作台的专业感。",
    preview: "linear-gradient(135deg, #09111f 0%, #111c2e 52%, #18263d 100%)",
    inspiration: "FilmIt 原生暗色工作台",
  },
  {
    id: "solarized-light",
    label: "Solarized Light",
    family: "Solarized",
    group: "FilmIt Core",
    description: "低眩光米色底，信息密度高时更耐看。",
    preview: "linear-gradient(135deg, #fdf6e3 0%, #eee8d5 55%, #e4dcc7 100%)",
    inspiration: "Solarized 家族的护眼浅色取向",
  },
  {
    id: "solarized-dark",
    label: "Solarized Dark",
    family: "Solarized",
    group: "FilmIt Core",
    description: "经典蓝绿深底，长时间查看更柔和。",
    preview: "linear-gradient(135deg, #002b36 0%, #073642 58%, #0a4552 100%)",
    inspiration: "Solarized Dark 的低眩光深色取向",
  },
  {
    id: "cyberpunk-dark",
    label: "Cyberpunk Dark",
    family: "Cyberpunk",
    group: "FilmIt Core",
    description: "霓虹夜景氛围，适合分镜与视频生成阶段。",
    preview: "linear-gradient(135deg, #090312 0%, #17081f 38%, #04293d 100%)",
    inspiration: "电影感霓虹夜景与赛博视觉",
  },
  {
    id: "darcula-dark",
    label: "Darcula Dark",
    family: "JetBrains",
    group: "IDE-Inspired",
    description: "中性石墨暗面，像经典 JetBrains IDE 一样克制稳重。",
    preview: "linear-gradient(135deg, #1f2329 0%, #2b2b2b 52%, #35393e 100%)",
    inspiration: "JetBrains Darcula",
  },
  {
    id: "monokai-dark",
    label: "Monokai Dark",
    family: "VS Code",
    group: "IDE-Inspired",
    description: "高饱和代码主题气质，适合需要明显强调色的界面。",
    preview: "linear-gradient(135deg, #1d1f1a 0%, #272822 52%, #3a3d35 100%)",
    inspiration: "VS Code / Monokai",
  },
  {
    id: "nord-dark",
    label: "Nord Dark",
    family: "VS Code",
    group: "IDE-Inspired",
    description: "冷静的北欧蓝灰，适合长时间阅读结构化信息。",
    preview: "linear-gradient(135deg, #2a303b 0%, #2e3440 48%, #3b4252 100%)",
    inspiration: "VS Code 团队偏爱的 Nord",
  },
  {
    id: "premiere-dark",
    label: "Premiere Dark",
    family: "Adobe",
    group: "Studio Video",
    description: "接近剪辑软件的深炭灰界面，降低媒体内容外的干扰。",
    preview: "linear-gradient(135deg, #121417 0%, #1a1c1f 52%, #252931 100%)",
    inspiration: "Adobe Premiere 深色界面",
  },
  {
    id: "resolve-gray",
    label: "Resolve Gray",
    family: "Blackmagic",
    group: "Studio Video",
    description: "蓝灰到中性灰的调色台风格，更像调色和审片环境。",
    preview: "linear-gradient(135deg, #22272d 0%, #2c3137 52%, #44484d 100%)",
    inspiration: "DaVinci Resolve blue-gray / gray background",
  },
] as const satisfies readonly ThemeOption[];

export type ThemeId = (typeof themeOptions)[number]["id"];

export const themeGroups = Array.from(
  themeOptions.reduce((groups, option) => {
    const current = groups.get(option.group) ?? [];
    current.push(option);
    groups.set(option.group, current);
    return groups;
  }, new Map<string, Array<(typeof themeOptions)[number]>>())
).map(([group, options]) => ({ group, options }));

const themeIdSet = new Set<string>(themeOptions.map((item) => item.id));

export function resolveThemeId(value: string | null | undefined): ThemeId {
  return value && themeIdSet.has(value) ? (value as ThemeId) : defaultThemeId;
}

export function isDarkTheme(themeId: ThemeId): boolean {
  return themeId.endsWith("dark");
}
