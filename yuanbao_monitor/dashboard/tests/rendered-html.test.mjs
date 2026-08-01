import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = new URL("../app/", import.meta.url);

test("统一 React 面板包含双模型、每日分析和采集控制", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /多模型监控台/);
  assert.match(source, /综合总览/);
  assert.match(source, /每日分析/);
  assert.match(source, /采集控制/);
  assert.match(source, /modelById\("yuanbao"\)\.statsEndpoint/);
  assert.match(source, /api\/control\/\$\{model\}/);
  assert.match(source, /北京时区 UTC\+8/);
  assert.match(source, /MODEL_REGISTRY\.map/);
  assert.match(source, /RESERVED_MODEL_SLOTS/);
});

test("模型注册表保留统一数据和控制入口", async () => {
  const registry = await readFile(new URL("modelRegistry.ts", app), "utf8");
  assert.match(registry, /statsEndpoint:\s*"\/api\/models\/doubao\/stats"/);
  assert.match(registry, /statsEndpoint:\s*"\/api\/models\/yuanbao\/stats"/);
  assert.match(registry, /RESERVED_MODEL_SLOTS\s*=\s*2/);
});

test("页面元数据已经替换为正式产品信息", async () => {
  const layout = await readFile(new URL("layout.tsx", app), "utf8");
  assert.match(layout, /多模型监控台/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview/);
});
