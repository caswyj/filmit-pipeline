import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Novel-to-Video Pipeline",
  description: "v1.0.0 workflow console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
