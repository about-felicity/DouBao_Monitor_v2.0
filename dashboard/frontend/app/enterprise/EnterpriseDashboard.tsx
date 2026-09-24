"use client";

import { useCallback, useEffect, useState } from "react";

type ModelRow = { id:string; name:string; received:number; valid_answers:number; failed:number; owned_mentions:number; mention_rate:number };
type DailyRow = { date:string; valid_answers:number; owned_mentions:number; model_count:number; mention_rate:number };
type Source = { title:string; url:string };
type Run = { model_id:string; model_name:string; run_id:string; date:string; finished_at:string; status:string; mentioned:boolean; answer:string; sources:Source[] };
type Data = { ok:boolean; generated_at:string; question:string; owned_company:string; dates:string[]; models:ModelRow[]; daily:DailyRow[]; recent_runs:Run[]; totals:{received:number;valid_answers:number;failed:number;owned_mentions:number;mention_rate:number;model_count:number} };

function apiBase() {
  if (typeof window === "undefined") return "";
  const configured = (process.env.NEXT_PUBLIC_API_BASE || "").trim().replace(/\/$/, "");
  if (configured) return configured;
  return window.location.port === "3000" ? `${window.location.protocol}//${window.location.hostname}:8765` : "";
}

export function EnterpriseDashboard() {
  const [data,setData] = useState<Data|null>(null);
  const [model,setModel] = useState("");
  const [date,setDate] = useState("");
  const [error,setError] = useState("");
  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams();
      if (model) query.set("model",model);
      if (date) query.set("date",date);
      const response = await fetch(`${apiBase()}/api/enterprise-analytics?${query}`, {cache:"no-store"});
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "企业数据加载失败");
      setData(payload); setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "企业数据加载失败"); }
  },[model,date]);
  useEffect(() => { load(); const timer=setInterval(load,15000); return ()=>clearInterval(timer); },[load]);

  return <main className="enterprise-shell">
    <aside className="enterprise-sidebar">
      <a className="enterprise-brand" href="/"><span>MI</span><b>企业概率监控</b></a>
      <p>与主产品面板完全独立，只统计指定的企业推荐问题。</p>
      <a className="back-dashboard" href="/">← 返回主产品面板</a>
    </aside>
    <section className="enterprise-content">
      <header className="enterprise-header">
        <div><small>ENTERPRISE GEO MONITOR</small><h1>企业推荐概率</h1><p>{data?.question || "成都做GEO比较好的公司推荐一下"}</p></div>
        <div className="enterprise-filters">
          <label>模型<select value={model} onChange={e=>setModel(e.target.value)}><option value="">全部模型</option>{data?.models.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <label>日期<select value={date} onChange={e=>setDate(e.target.value)}><option value="">全部日期</option>{data?.dates.map(item=><option key={item}>{item}</option>)}</select></label>
        </div>
      </header>
      {error && <div className="enterprise-error">{error}</div>}
      <div className="company-focus"><span>自有企业</span><b>{data?.owned_company || "成都巨量求索科技有限责任公司"}</b><small>概率 = 回答正文提及自有企业的有效回答 ÷ 有效回答；无回答和抓取失败不计入分母。</small></div>
      <div className="enterprise-kpis">
        <article><span>企业推荐概率</span><strong>{data?.totals.mention_rate.toFixed(1) || "0.0"}%</strong></article>
        <article><span>自有企业提及</span><strong>{data?.totals.owned_mentions || 0}</strong></article>
        <article><span>有效回答</span><strong>{data?.totals.valid_answers || 0}</strong><small>共接收 {data?.totals.received || 0} 条</small></article>
        <article><span>有效模型</span><strong>{data?.totals.model_count || 0}</strong><small>失败 {data?.totals.failed || 0} 条</small></article>
      </div>
      <section className="enterprise-panel"><h2>各模型企业推荐概率</h2><div className="enterprise-models">{data?.models.map(item=><article key={item.id}><h3>{item.name}</h3><strong>{item.mention_rate.toFixed(1)}%</strong><p>{item.owned_mentions} 次提及 / {item.valid_answers} 条有效回答</p><small>接收 {item.received} · 失败 {item.failed}</small></article>)}</div></section>
      <section className="enterprise-panel"><h2>每日趋势</h2><div className="enterprise-table"><div className="thead"><span>日期</span><span>企业概率</span><span>提及 / 有效回答</span><span>有效模型</span></div>{data?.daily.map(item=><div key={item.date}><b>{item.date}</b><strong>{item.mention_rate.toFixed(1)}%</strong><span>{item.owned_mentions} / {item.valid_answers}</span><span>{item.model_count}</span></div>)}</div></section>
      <section className="enterprise-panel"><h2>回答原文与信源审计</h2><div className="enterprise-runs">{data?.recent_runs.map(item=><details key={`${item.model_id}-${item.run_id}`}><summary><b>{item.model_name}</b><span>{item.date || item.finished_at}</span><em className={item.mentioned?"hit":"miss"}>{item.mentioned?"已提及":"未提及"}</em></summary><pre>{item.answer || "（无有效回答）"}</pre>{item.sources.length>0&&<div className="enterprise-sources">{item.sources.map((source,index)=><a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer">{index+1}. {source.title}</a>)}</div>}</details>)}</div></section>
    </section>
  </main>;
}
