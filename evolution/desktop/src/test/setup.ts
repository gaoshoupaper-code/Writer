// vitest 全局 setup（RSK-003 桌面测试基建）。
// 注册 jest-dom 断言。假时钟在各测试内按需启用（避免初始 effect 异步落定被冻住）。
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
