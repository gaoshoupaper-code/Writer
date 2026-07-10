/**
 * Vitest 全局 setup。
 *
 * 1. 注册 @testing-library/jest-dom matchers（toBeInTheDocument 等）
 * 2. 全局 mock @tauri-apps/api/core 的 invoke（非 Tauri 环境下会崩）
 * 3. 全局 mock @tauri-apps/api/event 的 listen（同上）
 *
 * 各测试文件可在 vi.mock("@/lib/api") / vi.mock("@/lib/stream") 里做更细粒度的
 * 业务级 mock，这里只兜底底层 Tauri 调用，防止模块加载阶段直接抛错。
 */
import "@testing-library/jest-dom";

// ── 全局 mock：@tauri-apps/api/core ──
const mockInvoke = vi.fn().mockResolvedValue({});
vi.mock("@tauri-apps/api/core", () => ({
  invoke: mockInvoke,
}));

// ── 全局 mock：@tauri-apps/api/event ──
// listen 返回一个 unlisten 函数（与真实签名一致）
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn().mockResolvedValue(() => {}),
}));

// ── jsdom 补丁：matchMedia（部分组件可能用到）──
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// 导出供测试文件按需引用（如需要断言 invoke 被调用）
export { mockInvoke };
