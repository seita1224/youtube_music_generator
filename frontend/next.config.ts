import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone", // docker イメージを薄くする (ADR-0029)
  // backend プロキシは app/api/backend/[...path] BFF が担当(ADR-0013)。
  // rewrites だとクライアント資格情報やセッション検証を挟めない。
};

export default nextConfig;
