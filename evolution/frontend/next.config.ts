import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // D1：静态导出模式，产物到 out/，由 evolution FastAPI StaticFiles 单端口托管。
  // 牺牲 SSR/image-optimization/middleware——监测端是纯客户端 SPA，不需要这些。
  output: "export",
  // SPA fallback：所有未匹配路由回落 index.html（App Router 路由靠客户端解析）。
  trailingSlash: true,
  // dev 直连 evolution（NEXT_PUBLIC_API_BASE_URL=http://localhost:7789）；
  // prod 同源（空字符串，StaticFiles 托管，无跨域）。
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
  },
};

export default nextConfig;
