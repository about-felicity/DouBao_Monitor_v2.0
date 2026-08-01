import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "豆包 × 元宝双模型监控台",
  description: "豆包与腾讯元宝采集、按日分析、信源和品牌竞争统一监控面板",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
