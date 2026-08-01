import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = new URL("../app/", import.meta.url);

test("统一 React 面板包含双模型、每日分析和采集控制", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /双模型监控台/);
  assert.match(source, /综合总览/);
  assert.match(source, /每日分析/);
  assert.match(source, /采集控制/);
  assert.match(source, /api\/yuanbao\/stats/);
  assert.match(source, /api\/control\/\$\{model\}/);
  assert.match(source, /北京时区 UTC\+8/);
});

test("页面元数据已经替换为正式产品信息", async () => {
  const layout = await readFile(new URL("layout.tsx", app), "utf8");
  assert.match(layout, /豆包 × 元宝双模型监控台/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview/);
});
