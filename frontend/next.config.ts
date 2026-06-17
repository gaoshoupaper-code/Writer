import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 禁用响应压缩：dev 下 /api/* 经 rewrites 代理回后端，Next 的 gzip 会缓冲 SSE 心跳
  // 字节（浏览器带 Accept-Encoding: gzip 时尤其严重——70s 只漏出 1 个 ping），
  // 导致前端 45s 看门狗误判连接断开。本地 dev 无需压缩，关掉即可。
  compress: false,
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        // 阶段1：Next.js 服务端把 /api/* 转发给本机后端，让前端与 API 同源——
        // 手机外网访问时只需穿透前端一个端口，零跨域。阶段2上云时把 destination 改为云后端地址。
        destination: "http://127.0.0.1:7788/api/:path*",
      },
    ];
  },
};

export default nextConfig;
