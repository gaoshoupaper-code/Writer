import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vitest 配置：复用 vite 的 @ 别名 + React 插件。
// 独立于 vite.config.ts（后者是 async 函数形式 + Tauri 端口锁定，不适合测试环境）。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    testTimeout: 20000,
    // 默认包含 **/*.{test,spec}.{js,ts,jsx,tsx}，且自动忽略 node_modules
    include: ["src/**/*.{test,spec}.{js,ts,jsx,tsx}"],
  },
});
