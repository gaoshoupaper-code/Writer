"use client";

import { Toaster } from "sonner";

/** 全局 Toast 容器（sonner）。
 *
 * 独立 client 组件，供 server component layout.tsx 挂载。
 */
export function AppToaster() {
  return <Toaster richColors position="top-right" />;
}
