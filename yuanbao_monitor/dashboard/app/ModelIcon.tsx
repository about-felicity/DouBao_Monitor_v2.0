"use client";

type IconProps = {
  className?: string;
  size?: number;
};

const iconStyles = {
  deepseek: { label: "DS", background: "linear-gradient(145deg, #5b8cff, #3156ca)" },
  doubao: { label: "豆", background: "linear-gradient(145deg, #29bf91, #087d60)" },
  wenxin: { label: "文", background: "linear-gradient(145deg, #3888ef, #1856bb)" },
  yuanbao: { label: "元", background: "linear-gradient(145deg, #20c984, #07945d)" },
  afu: { label: "福", background: "linear-gradient(145deg, #ff9a63, #df5a20)" },
} as const;

function ModelBadge({ tone, className, size = 24 }: IconProps & { tone: keyof typeof iconStyles }) {
  const style = iconStyles[tone];
  return (
    <span
      aria-label={tone}
      className={className}
      style={{
        width: size,
        height: size,
        display: "inline-grid",
        placeItems: "center",
        flex: "0 0 auto",
        borderRadius: Math.max(7, Math.round(size * 0.28)),
        background: style.background,
        color: "#fff",
        fontFamily: "Arial, Microsoft YaHei UI, sans-serif",
        fontSize: Math.max(9, Math.round(size * 0.34)),
        fontWeight: 900,
        letterSpacing: tone === "deepseek" ? "-0.5px" : 0,
        boxShadow: "inset 0 0 0 1px #ffffff38",
      }}
    >
      {style.label}
    </span>
  );
}

export function DeepSeekIcon(props: IconProps) {
  return <ModelBadge {...props} tone="deepseek" />;
}

export function DoubaoIcon(props: IconProps) {
  return <ModelBadge {...props} tone="doubao" />;
}

export function WenxinIcon(props: IconProps) {
  return <ModelBadge {...props} tone="wenxin" />;
}

export function YuanbaoIcon(props: IconProps) {
  return <ModelBadge {...props} tone="yuanbao" />;
}

export function AfuIcon(props: IconProps) {
  return <ModelBadge {...props} tone="afu" />;
}

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
