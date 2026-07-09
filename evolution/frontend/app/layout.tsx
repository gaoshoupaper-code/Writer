import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { TopNav } from "@/components/TopNav";
import { AppToaster } from "@/components/AppToaster";
import "./globals.css";

export const metadata: Metadata = {
  title: "Writer 进化",
  description: "AEGIS 自驱进化循环 · 驾驶舱",
};

// 真正加载 Inter / JetBrains Mono。
// globals.css 里 var(--font-sans)/var(--font-mono) 此前是死引用（无 @font-face、无 <link>），
// 全站一直回退到系统字体；这里补上，精密排版才落地。
// 用独立变量名 --font-inter / --font-jetbrains 避免与 @theme 里组合用的 --font-sans 自引用冲突。
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
});

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className={`${inter.variable} ${mono.variable}`}>
      <body>
        <TopNav />
        <main style={{ maxWidth: 1440, margin: "0 auto", padding: "32px 28px" }}>{children}</main>
        <AppToaster />
      </body>
    </html>
  );
}
