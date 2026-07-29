import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// vitest 配置（与 vite.config.ts 分离，避免影响 Tauri 构建）。
// RSK-003：建立可在 CI 执行的桌面 hook/页面状态测试，守住生命周期与陈旧契约。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // hook 计时/轮询测试需要假定时钟与可控的 setInterval/setTimeout。
    setupFiles: ["./src/test/setup.ts"],
  },
});
