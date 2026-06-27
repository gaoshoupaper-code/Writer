import type { Metadata } from "next";
import { TopNav } from "@/components/TopNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Writer 进化",
  description: "AEGIS 自驱进化循环 · 驾驶舱",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <TopNav />
        <main style={{ maxWidth: 1400, margin: "0 auto", padding: 24 }}>{children}</main>
      </body>
    </html>
  );
}
