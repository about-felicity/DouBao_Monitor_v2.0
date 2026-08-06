"use client";

/**
 * 各模型官方 Logo — 来自 @lobehub/icons
 */

import { DeepSeek, Doubao, Wenxin, Yuanbao } from "@lobehub/icons";

type IconProps = {
  className?: string;
  size?: number;
};

export function DeepSeekIcon({ className, size = 24 }: IconProps) {
  return <DeepSeek.Color className={className} size={size} />;
}

export function DoubaoIcon({ className, size = 24 }: IconProps) {
  return <Doubao.Color className={className} size={size} />;
}

export function WenxinIcon({ className, size = 24 }: IconProps) {
  return <Wenxin.Color className={className} size={size} />;
}

export function YuanbaoIcon({ className, size = 24 }: IconProps) {
  return <Yuanbao.Color className={className} size={size} />;
}

/** 蚂蚁阿福 — 官方 favicon */
export function AfuIcon({ className, size = 24 }: IconProps) {
  return (
    <img
      className={className}
      src="/afu-icon.png"
      alt="蚂蚁阿福"
      width={size}
      height={size}
      style={{ borderRadius: 6 }}
    />
  );
}

/** 根据 tone 返回对应图标组件 */
export function getIcon(tone: string) {
  switch (tone) {
    case "deepseek":
      return DeepSeekIcon;
    case "doubao":
      return DoubaoIcon;
    case "wenxin":
      return WenxinIcon;
    case "yuanbao":
      return YuanbaoIcon;
    case "afu":
      return AfuIcon;
    default:
      return null;
  }
}
