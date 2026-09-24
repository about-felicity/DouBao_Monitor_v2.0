"use strict";

const DEFAULT_QUESTIONS = [
  "推荐一款护发精油", "推荐一款护发素", "推荐一款控油蓬松洗发水", "推荐一款二硫化硒洗发水",
  "推荐一款沐浴精油", "推荐一款眉毛增长液", "推荐一款祛痘精华液", "推荐一款美白面霜",
  "推荐一款造型喷雾", "推荐一款染发剂", "推荐一款睫毛增长液", "推荐一款防脱洗发水",
  "推荐一款防脱精华液", "推荐一款面膜", "推荐一款眼霜", "推荐一款卸妆油",
  "推荐一款爽肤水", "推荐一款洗面奶", "推荐一款身体乳", "推荐一款护手霜", "推荐一款防晒霜"
];
const SETTINGS_SCHEMA_VERSION = 17;
const RECENT_RESULT_LIMIT = 200;
const RECENT_EVENT_LIMIT = 300;
const RESULT_ID_LIMIT = 2000;
const PENDING_SYNC_LIMIT = 5000;
const DEFAULT_SETTINGS = {
  questions: DEFAULT_QUESTIONS,
  rounds: 2,
  dailyEnabled: true,
  questionMode: "interleaved",
  intervalMinSeconds: 360,
  intervalMaxSeconds: 360,
  timeoutSeconds: 240,
  stableSeconds: 10,
  minAnswerLength: 60,
  maxRetries: 3,
  retryDelaySeconds: 30,
  receiverUrl: "http://127.0.0.1:8766",
  targetUrl: "https://chat.deepseek.com/"
};

const SUPPORTED = ["https://chat.deepseek.com/*"];

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function storageSet(value) {
  return new Promise((resolve) => chrome.storage.local.set(value, resolve));
}

function storageRemove(keys) {
  return new Promise((resolve) => chrome.storage.local.remove(keys, resolve));
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 3000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function tabsQuery(query) {
  return new Promise((resolve) => chrome.tabs.query(query, resolve));
}

function sendToTab(tabId, payload) {
  return new Promise((resolve) => chrome.tabs.sendMessage(tabId, payload, (response) => {
    if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
    else resolve(response || { ok: true });
  }));
}

function matchesTarget(url) {
  try {
    const parsed = new URL(url || "");
    const host = parsed.hostname.toLowerCase();
    return host === "chat.deepseek.com";
  } catch (_) {
    return false;
  }
}

async function settings() {
  const data = await storageGet(["settings"]);
  return { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
}

function safeSettings(value) {
  const next = { ...DEFAULT_SETTINGS, ...(value || {}) };
  next.intervalMinSeconds = 360;
  next.intervalMaxSeconds = 360;
  next.retryDelaySeconds = Math.max(20, Number(next.retryDelaySeconds) || DEFAULT_SETTINGS.retryDelaySeconds);
  return next;
}

async function claimSendSlot() {
  const currentSettings = safeSettings(await settings());
  const data = await storageGet(["nextAllowedSendAt"]);
  const now = Date.now();
  const nextAllowed = Math.max(0, Number(data.nextAllowedSendAt) || 0);
  if (nextAllowed > now) return { ok: false, waitMs: nextAllowed - now, nextAllowedSendAt: nextAllowed };
  const span = currentSettings.intervalMaxSeconds - currentSettings.intervalMinSeconds;
  const delaySeconds = currentSettings.intervalMinSeconds + Math.random() * span;
  const reservedUntil = now + Math.round(delaySeconds * 1000);
  await storageSet({ nextAllowedSendAt: reservedUntil });
  return { ok: true, waitMs: 0, nextAllowedSendAt: reservedUntil };
}

function nextDailyTime(anchorValue, now = Date.now()) {
  let next = Date.parse(anchorValue || "");
  if (!Number.isFinite(next)) next = now;
  do { next += 24 * 60 * 60 * 1000; } while (next <= now + 60_000);
  return next;
}

async function scheduleNextDailyRun(job) {
  const currentSettings = safeSettings(await settings());
  if (!currentSettings.dailyEnabled) {
    await chrome.alarms.clear("deepseek-daily-run");
    await storageSet({ nextDailyRunAt: 0 });
    return 0;
  }
  const when = nextDailyTime(job?.dailyScheduledFor || job?.startedAt);
  chrome.alarms.create("deepseek-daily-run", { when });
  await storageSet({ nextDailyRunAt: when, lastDailyError: "" });
  return when;
}

async function restoreDailySchedule(job, storedWhen = 0) {
  const currentSettings = safeSettings(await settings());
  if (!currentSettings.dailyEnabled || job?.state !== "completed") return 0;
  const now = Date.now();
  let when = Number(storedWhen) || 0;
  if (when <= now) {
    const anchor = Date.parse(job?.dailyScheduledFor || job?.startedAt || job?.finishedAt || "");
    const firstDue = Number.isFinite(anchor) ? anchor + 24 * 60 * 60 * 1000 : now + 1000;
    when = firstDue > now ? firstDue : now + 1000;
    await storageSet({ nextDailyRunAt: when, lastDailyError: "" });
  }
  chrome.alarms.create("deepseek-daily-run", { when });
  return when;
}

async function completeJob() {
  const job = await mutateJob((current) => current ? ({ ...current, state: "completed", finishedAt: new Date().toISOString() }) : current);
  if (job) await scheduleNextDailyRun(job);
  return { ok: true, job };
}

async function deferJob(request) {
  const seconds = Math.max(10 * 60, Math.min(30 * 60, Number(request.cooldownSeconds) || 30 * 60));
  const autoResumeAt = Date.now() + seconds * 1000;
  const reason = String(request.error || "DeepSeek静默限流，正在自动冷却");
  const job = await mutateJob((current) => current ? ({
    ...current, state: "paused", pauseReason: "silent_submission",
    autoResumeAt, lastError: reason
  }) : current);
  chrome.alarms.create("deepseek-cooldown-resume", { when: autoResumeAt });
  await updateBadge(job);
  return { ok: true, job, autoResumeAt };
}

async function resumeDeferredJob() {
  const job = await mutateJob((current) => {
    if (!current || current.state !== "paused" || current.pauseReason !== "silent_submission") return current;
    if (Number(current.autoResumeAt) > Date.now() + 1000) return current;
    return { ...current, state: "running", pauseReason: "", autoResumeAt: 0, lastError: "" };
  });
  if (job?.state === "running" && job.ownerTabId) await sendToTab(job.ownerTabId, { type: "DM_RUN" });
  await updateBadge(job);
  return job;
}

async function runScheduledDailyJob() {
  const data = await storageGet(["settings", "job"]);
  const currentSettings = safeSettings(data.settings);
  if (!currentSettings.dailyEnabled) return;
  if (["running", "paused", "error"].includes(data.job?.state)) {
    if (data.job?.state === "running") chrome.alarms.create("deepseek-daily-run", { when: Date.now() + 60 * 60 * 1000 });
    return;
  }
  try {
    await startJob({ settings: currentSettings, autoDaily: true, scheduledFor: new Date().toISOString() });
    await storageSet({ nextDailyRunAt: 0, lastDailyError: "" });
  } catch (error) {
    const message = String(error && error.message || error);
    await storageSet({ lastDailyError: "每日任务暂未启动：" + message });
    chrome.alarms.create("deepseek-daily-run", { when: Date.now() + 15 * 60 * 1000 });
  }
}

async function findTargetTab() {
  const active = await tabsQuery({ active: true, currentWindow: true });
  if (active[0] && matchesTarget(active[0].url)) return active[0];
  const tabs = await tabsQuery({ url: SUPPORTED });
  return tabs.find((tab) => matchesTarget(tab.url)) || null;
}

async function wakeRunner(reason = "watchdog") {
  const data = await storageGet(["job"]);
  const job = data.job;
  if (!job || job.state !== "running") return { ok: true, skipped: true };
  const tabs = await tabsQuery({ url: SUPPORTED });
  const target = tabs.find((tab) => tab.id === job.ownerTabId)
    || tabs.find((tab) => matchesTarget(tab.url));
  if (!target?.id) return { ok: false, error: "DeepSeek页面未打开" };
  await chrome.tabs.update(target.id, { autoDiscardable: false });
  if (job.ownerTabId !== target.id) {
    await mutateJob((current) => current?.state === "running"
      ? ({ ...current, ownerTabId: target.id }) : current);
  }
  let response = await sendToTab(target.id, { type: "DM_RUN", reason });
  if (!response?.ok) {
    const injected = await injectIntoTab(target.id);
    if (!injected.ok) return injected;
    response = await sendToTab(target.id, { type: "DM_RUN", reason });
  }
  return response || { ok: true };
}

async function scheduleRunnerWake(whenValue) {
  const when = Math.max(Date.now() + 1000, Number(whenValue) || Date.now() + 1000);
  chrome.alarms.create("deepseek-runner-wake", { when });
  await storageSet({ nextRunnerWakeAt: when });
  return { ok: true, when };
}

async function injectIntoTab(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"], world: "MAIN", injectImmediately: true });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["core.js", "content.js"], world: "ISOLATED", injectImmediately: true });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function updateBadge(job) {
  let text = "";
  let color = "#475569";
  if (job?.state === "running") { text = String(Math.min(999, (job.cursor || 0) + 1)); color = "#16a34a"; }
  if (job?.state === "paused") { text = "停"; color = "#d97706"; }
  if (job?.state === "error") { text = "!"; color = "#dc2626"; }
  if (job?.state === "completed") { text = "✓"; color = "#2563eb"; }
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
}

async function startJob(request) {
  const nextSettings = safeSettings(request.settings);
  nextSettings.questions = Array.from(new Set((nextSettings.questions || []).map((value) => String(value || "").trim()).filter(Boolean)));
  if (!nextSettings.questions.length) throw new Error("请至少填写一个问题");
  const existing = (await storageGet(["job"])).job;
  if (["running", "paused"].includes(existing?.state)) throw new Error("已有任务正在运行或暂停，请先停止后再开始新任务");
  const tab = await findTargetTab();
  if (!tab?.id) throw new Error("请先在 Chrome 中打开并登录 chat.deepseek.com，再点击开始");
  let probe = await sendToTab(tab.id, { type: "DM_PROBE" });
  if (!probe.ok) {
    await injectIntoTab(tab.id);
    probe = await sendToTab(tab.id, { type: "DM_PROBE" });
  }
  if (!probe.ok) throw new Error("当前页面尚未加载监控插件，请刷新 DeepSeek 页面后重试");
  if (!probe.hasComposer || !probe.hasNewChat) throw new Error("当前不是可提问的对话页面，未同时找到输入框和“新对话”按钮");
  const schedule = buildSchedule(nextSettings.questions, nextSettings.rounds, nextSettings.questionMode);
  const job = {
    id: crypto.randomUUID(),
    state: "running",
    cursor: 0,
    schedule,
    inFlight: null,
    ownerTabId: tab.id,
    autoDaily: Boolean(request.autoDaily),
    dailyScheduledFor: request.scheduledFor || "",
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    lastError: ""
  };
  await storageSet({ settings: nextSettings, job, lastDailyError: "", nextRunnerWakeAt: 0 });
  await chrome.tabs.update(tab.id, { autoDiscardable: false });
  await updateBadge(job);
  await sendToTab(tab.id, { type: "DM_RUN" });
  return { ok: true, job, tab: { id: tab.id, url: tab.url, title: tab.title } };
}

function buildSchedule(questions, rounds, mode) {
  const count = Math.max(1, Math.min(Number(rounds) || 1, 10000));
  const output = [];
  if (mode === "sequential") {
    questions.forEach((prompt, questionIndex) => {
      for (let round = 1; round <= count; round += 1) output.push({ prompt, questionIndex, questionRound: round });
    });
  } else {
    for (let round = 1; round <= count; round += 1) {
      questions.forEach((prompt, questionIndex) => output.push({ prompt, questionIndex, questionRound: round }));
    }
  }
  return output.map((item, globalIndex) => ({ ...item, globalIndex }));
}

async function mutateJob(mutator) {
  const data = await storageGet(["job"]);
  const job = data.job ? { ...data.job } : null;
  const next = await mutator(job);
  if (next) {
    next.updatedAt = new Date().toISOString();
    await storageSet({ job: next });
    await updateBadge(next);
  }
  return next;
}

async function postResult(result) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + "/api/results";
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) {
    return { ok: false, error: "接收地址必须是本机 127.0.0.1 或 localhost" };
  }
  try {
    const response = await fetchWithTimeout(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result)
    }, 5000);
    if (!response.ok) throw new Error("HTTP " + response.status);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function postEvent(event) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + "/api/events";
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) return { ok: false, error: "invalid_local_receiver" };
  try {
    const response = await fetchWithTimeout(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(event)
    }, 3000);
    if (!response.ok) throw new Error("HTTP " + response.status);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function receiverGet(path) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + path;
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) throw new Error("invalid_local_receiver");
  const response = await fetchWithTimeout(url, { cache: "no-store" }, 2000);
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

async function logEvent(request) {
  const data = await storageGet(["job", "recentEventLogs"]);
  const job = data.job || {};
  const item = job.schedule?.[job.cursor] || job.inFlight || {};
  const event = {
    timestamp: new Date().toISOString(), level: request.level || "info", event: request.event || "event",
    run_id: job.id || "", round: Number(item.globalIndex ?? job.cursor ?? 0) + 1,
    prompt: item.prompt || "", page_tab_id: request.tabId || null, details: request.details || {}
  };
  const logs = Array.isArray(data.recentEventLogs) ? data.recentEventLogs : [];
  logs.push(event);
  await storageSet({ recentEventLogs: logs.slice(-RECENT_EVENT_LIMIT) });
  const posted = await postEvent(event);
  return { ok: true, posted: posted.ok, event };
}

async function storeResult(result) {
  const data = await storageGet(["recentResults", "resultCount", "recentResultIds", "pendingSync"]);
  const results = Array.isArray(data.recentResults) ? data.recentResults : [];
  const ids = Array.isArray(data.recentResultIds) ? data.recentResultIds : [];
  const isNew = !ids.includes(result.result_id);
  if (isNew) {
    results.push(result);
    ids.push(result.result_id);
  }
  const pending = Array.isArray(data.pendingSync) ? data.pendingSync.filter((item) => item.result_id !== result.result_id) : [];
  pending.push(result);
  await storageSet({
    recentResults: results.slice(-RECENT_RESULT_LIMIT),
    recentResultIds: ids.slice(-RESULT_ID_LIMIT),
    resultCount: Math.max(0, Number(data.resultCount) || 0) + (isNew ? 1 : 0),
    pendingSync: pending.slice(-PENDING_SYNC_LIMIT)
  });
  const [sync, job] = await Promise.all([postResult(result), mutateJob((current) => {
    if (!current) return current;
    if (current.inFlight && current.inFlight.globalIndex !== result.round - 1) return current;
    current.cursor = Math.max(current.cursor || 0, result.round);
    current.inFlight = null;
    current.lastError = "";
    if (current.cursor >= current.schedule.length) {
      current.state = "completed";
      current.finishedAt = new Date().toISOString();
    }
    return current;
  })]);
  if (job?.state === "completed") await scheduleNextDailyRun(job);
  const latest = await storageGet(["pendingSync"]);
  const remaining = Array.isArray(latest.pendingSync) ? latest.pendingSync.filter((item) => item.result_id !== result.result_id) : [];
  if (!sync.ok) remaining.push(result);
  await storageSet({ pendingSync: remaining.slice(-PENDING_SYNC_LIMIT), lastReceiverError: sync.ok ? "" : sync.error });
  return { ok: true, synced: sync.ok, job };
}

async function retryPending() {
  const data = await storageGet(["pendingSync"]);
  const pending = Array.isArray(data.pendingSync) ? data.pendingSync : [];
  if (!pending.length) return;
  const remaining = [];
  for (const result of pending.slice(0, 50)) {
    const sync = await postResult(result);
    if (!sync.ok) remaining.push(result);
  }
  remaining.push(...pending.slice(50));
  await storageSet({ pendingSync: remaining, lastReceiverError: remaining.length ? "本地接收器暂时不可用" : "" });
}

async function initializeStorage() {
  const data = await storageGet([
    "settings", "results", "recentResults", "resultCount", "recentResultIds", "pendingSync",
    "eventLogs", "recentEventLogs", "settingsSchemaVersion", "job", "lastReceiverError", "nextAllowedSendAt",
    "nextDailyRunAt", "lastDailyError", "nextRunnerWakeAt"
  ]);
  const oldVersion = Number(data.settingsSchemaVersion || 0);
  const resetForRemoteRun = oldVersion < 3;
  const legacyResults = resetForRemoteRun ? [] : (Array.isArray(data.results) ? data.results : []);
  const recentResults = resetForRemoteRun ? [] : (Array.isArray(data.recentResults) ? data.recentResults : legacyResults.slice(-RECENT_RESULT_LIMIT));
  const legacyEvents = resetForRemoteRun ? [] : (Array.isArray(data.eventLogs) ? data.eventLogs : []);
  const recentEventLogs = resetForRemoteRun ? [] : (Array.isArray(data.recentEventLogs) ? data.recentEventLogs : legacyEvents.slice(-RECENT_EVENT_LIMIT));
  const migratedSettings = resetForRemoteRun ? { ...DEFAULT_SETTINGS } : { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
  if (!resetForRemoteRun && oldVersion < 6) Object.assign(migratedSettings, {
    questions: DEFAULT_QUESTIONS,
    rounds: 2,
    dailyEnabled: true,
    intervalMinSeconds: 600,
    intervalMaxSeconds: 660
  });
  if (!resetForRemoteRun && oldVersion < 12) Object.assign(migratedSettings, {
    intervalMinSeconds: 360,
    intervalMaxSeconds: 360
  });
  let migratedJob = !resetForRemoteRun && oldVersion < 9 && ["running", "paused", "error"].includes(data.job?.state)
    ? { ...data.job, state: "stopped", lastError: "扩展已升级并安全停止旧任务，请刷新 DeepSeek 页面后重新开始" }
    : (resetForRemoteRun ? null : (data.job || null));
  const previousOwnerTabId = migratedJob?.ownerTabId || null;
  // Older versions could park an intact round in a synthetic cooldown. Resume
  // it on upgrade; the send-slot gate still enforces the six-minute interval.
  if (oldVersion >= 9 && oldVersion < 12 && migratedJob?.state === "paused" && migratedJob.pauseReason === "silent_submission") {
    migratedJob = {
      ...migratedJob,
      state: "running",
      ownerTabId: null,
      pauseReason: "",
      autoResumeAt: 0,
      lastError: "",
      inFlight: migratedJob.inFlight ? {
        ...migratedJob.inFlight,
        phase: "retry_pending",
        retryCount: 0,
        silentFailureCount: 0
      } : migratedJob.inFlight
    };
  }
  if (!resetForRemoteRun && oldVersion < 12 && ["running", "paused"].includes(migratedJob?.state)) {
    migratedJob = { ...migratedJob, ownerTabId: null };
  }
  if (!resetForRemoteRun && oldVersion < 17 && ["running", "paused"].includes(migratedJob?.state)) {
    // A tab reload/replacement may change Chrome's tab id. Release the stale
    // owner lease before reloading so the same visible DeepSeek page can claim
    // the task immediately after its content script starts.
    migratedJob = { ...migratedJob, ownerTabId: null };
  }
  // v0.2.15 could exhaust three 20-second hydration retries and upload a
  // failed/empty result even though DeepSeek had created the conversation.
  // Requeue only those affected rounds from the currently active job. This
  // repairs today's missing dashboard rows without repeating successful ones.
  if (!resetForRemoteRun && oldVersion < 13 && ["running", "paused"].includes(migratedJob?.state)) {
    const recoverableCodes = new Set(["DM_SUBMISSION_LOST", "DM_GENERATION_NOT_STARTED", "DM_ANSWER_TIMEOUT"]);
    const recoverable = recentResults
      .filter((result) => (
        result?.run_id === migratedJob.id
        && result?.status === "failed"
        && recoverableCodes.has(result?.capture?.failure_code)
      ))
      .sort((left, right) => Number(left.round || 0) - Number(right.round || 0));
    if (recoverable.length) {
      const repairedSchedule = Array.isArray(migratedJob.schedule) ? migratedJob.schedule.slice() : [];
      for (const result of recoverable) {
        const original = repairedSchedule[Number(result.round || 0) - 1];
        if (!original?.prompt) continue;
        repairedSchedule.push({
          ...original,
          globalIndex: repairedSchedule.length,
          recoveryOfRound: Number(result.round || 0)
        });
      }
      migratedJob = {
        ...migratedJob,
        schedule: repairedSchedule,
        ownerTabId: null,
        recoveryRoundsAdded: Math.max(0, repairedSchedule.length - Number(migratedJob.schedule?.length || 0))
      };
    } else {
      migratedJob = { ...migratedJob, ownerTabId: null };
    }
  }
  await storageSet({
    settings: migratedSettings,
    settingsSchemaVersion: SETTINGS_SCHEMA_VERSION,
    recentResults,
    recentResultIds: resetForRemoteRun ? [] : (Array.isArray(data.recentResultIds) ? data.recentResultIds : recentResults.map((item) => item.result_id).filter(Boolean).slice(-RESULT_ID_LIMIT)),
    resultCount: resetForRemoteRun ? 0 : Math.max(Number(data.resultCount) || 0, legacyResults.length, recentResults.length),
    pendingSync: resetForRemoteRun ? [] : (Array.isArray(data.pendingSync) ? data.pendingSync : []),
    recentEventLogs,
    job: migratedJob,
    lastReceiverError: resetForRemoteRun ? "" : (data.lastReceiverError || ""),
    nextAllowedSendAt: resetForRemoteRun ? 0 : (Number(data.nextAllowedSendAt) || 0),
    nextDailyRunAt: resetForRemoteRun ? 0 : (Number(data.nextDailyRunAt) || 0),
    lastDailyError: resetForRemoteRun ? "" : (data.lastDailyError || ""),
    nextRunnerWakeAt: resetForRemoteRun ? 0 : (Number(data.nextRunnerWakeAt) || 0)
  });
  if (oldVersion < SETTINGS_SCHEMA_VERSION) await storageRemove(["results", "eventLogs"]);
  if (oldVersion < 12) await chrome.alarms.clear("deepseek-cooldown-resume");
  chrome.alarms.create("retry-local-sync", { periodInMinutes: 1 });
  chrome.alarms.create("deepseek-runner-watchdog", { periodInMinutes: 1 });
  const currentSettings = safeSettings(migratedSettings);
  if (currentSettings.dailyEnabled && migratedJob?.state === "completed") {
    await restoreDailySchedule(migratedJob, data.nextDailyRunAt);
  } else if (currentSettings.dailyEnabled && Number(data.nextDailyRunAt) > Date.now()) {
    chrome.alarms.create("deepseek-daily-run", { when: Number(data.nextDailyRunAt) });
  }
  if (migratedJob?.state === "paused" && migratedJob.pauseReason === "silent_submission") {
    chrome.alarms.create("deepseek-cooldown-resume", { when: Math.max(Date.now() + 1000, Number(migratedJob.autoResumeAt) || Date.now() + 1000) });
  }
  if (migratedJob?.state === "running" && Number(data.nextRunnerWakeAt) > Date.now()) {
    chrome.alarms.create("deepseek-runner-wake", { when: Number(data.nextRunnerWakeAt) });
  }
  if (oldVersion >= 3 && oldVersion < 17 && ["running", "paused"].includes(migratedJob?.state)) {
    const tabs = await tabsQuery({ url: SUPPORTED });
    const target = tabs.find((tab) => tab.id === previousOwnerTabId) || tabs[0];
    if (target?.id) {
      // Keep using the user's existing signed-in DeepSeek page and return it
      // to the foreground after an upgrade. Leaving the activation popup in
      // front can freeze the background conversation tab and its timers.
      await chrome.tabs.update(target.id, { active: true, autoDiscardable: false });
      if (target.windowId) await chrome.windows.update(target.windowId, { focused: true });
      chrome.tabs.reload(target.id);
    }
  }
  return { resetForRemoteRun };
}

const initialization = initializeStorage();

chrome.runtime.onInstalled.addListener(async () => {
  await initialization;
  const tabs = await tabsQuery({ url: SUPPORTED });
  await Promise.all(tabs.filter((tab) => tab.id).map((tab) => injectIntoTab(tab.id)));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "retry-local-sync") retryPending();
  if (alarm.name === "deepseek-daily-run") runScheduledDailyJob();
  if (alarm.name === "deepseek-cooldown-resume") resumeDeferredJob();
  if (alarm.name === "deepseek-runner-wake") {
    storageSet({ nextRunnerWakeAt: 0 }).then(() => wakeRunner("scheduled_wake"));
  }
  if (alarm.name === "deepseek-runner-watchdog") wakeRunner("watchdog");
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  (async () => {
    await initialization;
    switch (request?.type) {
      case "START_JOB": return startJob(request);
      case "GET_CONTEXT": {
        const data = await storageGet(["settings", "job", "resultCount", "pendingSync", "lastReceiverError", "nextAllowedSendAt", "nextDailyRunAt", "lastDailyError"]);
        let receiverHealth = null;
        try { receiverHealth = await receiverGet("/api/health"); } catch (_) {}
        return { ok: true, settings: { ...DEFAULT_SETTINGS, ...(data.settings || {}) }, job: data.job || null,
          resultCount: receiverHealth ? Number(receiverHealth.result_count || 0) : Number(data.resultCount || 0),
          pendingCount: receiverHealth ? Number(receiverHealth.remote_sync?.pending || 0) : (data.pendingSync || []).length,
          lastReceiverError: data.lastReceiverError || "", nextAllowedSendAt: Number(data.nextAllowedSendAt) || 0,
          nextDailyRunAt: Number(data.nextDailyRunAt) || 0, lastDailyError: data.lastDailyError || "" };
      }
      case "GET_JOB": {
        const data = await storageGet(["settings", "job"]);
        return { ok: true, settings: { ...DEFAULT_SETTINGS, ...(data.settings || {}) }, job: data.job || null };
      }
      case "CLAIM_SEND_SLOT": return claimSendSlot();
      case "SCHEDULE_RUNNER_WAKE": return scheduleRunnerWake(request.when);
      case "GET_ALL_RESULTS": {
        try {
          const local = await receiverGet("/api/results?limit=5000");
          return { ok: true, results: local.results || [], source: "local_receiver" };
        } catch (_) {
          const data = await storageGet(["recentResults"]);
          return { ok: true, results: data.recentResults || [], source: "extension_storage_recent" };
        }
      }
      case "GET_EVENT_LOGS": {
        try {
          const local = await receiverGet("/api/events");
          return { ok: true, events: local.events || [], source: "local_receiver" };
        } catch (_) {
          const data = await storageGet(["recentEventLogs"]);
          return { ok: true, events: data.recentEventLogs || [], source: "extension_storage_recent" };
        }
      }
      case "LOG_EVENT": return logEvent({ ...request, tabId: sender.tab?.id || null });
      case "CLAIM_RUNNER": {
        const tabId = sender.tab?.id;
        const job = await mutateJob((current) => {
          if (!current || !["running", "paused"].includes(current.state)) return current;
          if (!current.ownerTabId) current.ownerTabId = tabId;
          return current;
        });
        return { ok: true, claimed: Boolean(job && tabId && job.ownerTabId === tabId), job, settings: await settings() };
      }
      case "SET_IN_FLIGHT": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, inFlight: request.inFlight }) : job) };
      case "STORE_RESULT": return storeResult(request.result);
      case "PAUSE_JOB": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "paused", lastError: request.error || job.lastError || "" }) : job) };
      case "DEFER_JOB": return deferJob(request);
      case "RESUME_JOB": {
        await chrome.alarms.clear("deepseek-cooldown-resume");
        const job = await mutateJob((current) => current ? ({ ...current, state: "running", pauseReason: "", autoResumeAt: 0, lastError: "" }) : current);
        if (job?.ownerTabId) await sendToTab(job.ownerTabId, { type: "DM_RUN" });
        return { ok: true, job };
      }
      case "STOP_JOB": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "stopped" }) : job) };
      case "JOB_ERROR": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "error", lastError: request.error || "未知错误" }) : job) };
      case "COMPLETE_JOB": return completeJob();
      case "SAVE_SETTINGS": {
        const value = safeSettings(request.settings);
        await storageSet({ settings: value });
        if (!value.dailyEnabled) {
          await chrome.alarms.clear("deepseek-daily-run");
          await storageSet({ nextDailyRunAt: 0 });
        }
        return { ok: true, settings: value };
      }
      case "OPEN_TARGET": {
        const tab = await chrome.tabs.create({ url: request.url || (await settings()).targetUrl || "https://chat.deepseek.com/" });
        return { ok: true, tabId: tab.id };
      }
      case "RETRY_SYNC": await retryPending(); return { ok: true };
      default: return { ok: false, error: "unknown_message" };
    }
  })().then(sendResponse).catch((error) => sendResponse({ ok: false, error: String(error && error.message || error) }));
  return true;
});
