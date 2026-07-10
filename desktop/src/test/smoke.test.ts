/**
 * 冒烟测试：验证 Vitest + jsdom + jest-dom 基础设施可用。
 * 后续 P1 重构完成后可删除。
 */
import { describe, it, expect } from "vitest";

describe("test infrastructure smoke", () => {
  it("should run basic assertions", () => {
    expect(1 + 1).toBe(2);
  });

  it("should have jsdom DOM environment", () => {
    const div = document.createElement("div");
    div.textContent = "hello";
    document.body.appendChild(div);
    expect(div).toBeInTheDocument();
    expect(div).toHaveTextContent("hello");
  });
});
