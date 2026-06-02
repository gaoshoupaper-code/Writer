import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Writer Agent",
  description: "Minimal screenplay generation workspace",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

