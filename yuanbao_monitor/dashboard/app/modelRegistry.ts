export type ModelId = "doubao" | "yuanbao";

export type ModelDefinition = {
  id: ModelId;
  name: string;
  shortName: string;
  englishName: string;
  tone: string;
  statsEndpoint: string;
  supportsControl: boolean;
};

/**
 * 新模型统一从这里进入前端。数据适配器准备好后新增一项，模型切换、
 * 侧栏状态、对比卡片和采集控制卡片都会自动获得位置。
 */
export const MODEL_REGISTRY: readonly ModelDefinition[] = [
  {
    id: "doubao",
    name: "豆包",
    shortName: "豆",
    englishName: "DOUBAO",
    tone: "doubao",
    statsEndpoint: "/api/models/doubao/stats",
    supportsControl: true,
  },
  {
    id: "yuanbao",
    name: "元宝",
    shortName: "元",
    englishName: "YUANBAO",
    tone: "yuanbao",
    statsEndpoint: "/api/models/yuanbao/stats",
    supportsControl: true,
  },
] as const;

/** 默认在总览和控制页展示的未来接入位数量。 */
export const RESERVED_MODEL_SLOTS = 2;

export const modelById = (id: ModelId) => MODEL_REGISTRY.find((model) => model.id === id)!;
