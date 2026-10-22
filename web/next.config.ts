import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // Docker 镜像只带运行所需文件（不带整个 node_modules）
  devIndicators: false, // 开发模式角标会挡住侧栏底部，也会出现在截图里
  /* config options here */
};

export default nextConfig;
