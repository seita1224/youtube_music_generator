import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone", // docker イメージを薄くする (ADR-0029)
  async rewrites() {
    // 管理 UI → backend (Basic 認証は lib/auth.ts でヘッダ注入, T065)
    const backend = process.env.BACKEND_BASE_URL ?? "http://127.0.0.1:8000";
    return [{ source: "/api/backend/:path*", destination: `${backend}/:path*` }];
  },
};

export default nextConfig;
