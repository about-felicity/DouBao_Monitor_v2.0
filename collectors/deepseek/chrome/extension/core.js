(function (root, factory) {
  root.DeepSeekMonitorCore = factory();
})(typeof globalThis !== "undefined" ? globalThis : self, function () {
  "use strict";

  const INTERNAL_HOSTS = [
    "deepseek.com", "deepseek.cn", "deepseekstatic.com"
  ];

  function compact(value) {
    return String(value || "").replace(/\s+/g, "").trim();
  }

  function normalizeText(value) {
    return String(value || "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function unwrapUrl(raw) {
    if (!raw) return "";
    let value = String(raw).trim().replace(/&amp;/g, "&");
    try {
      const parsed = new URL(value, "https://chat.deepseek.com/");
      for (const key of ["url", "target", "target_url", "targetUrl", "redirect", "redirect_url", "dest", "destination"]) {
        const nested = parsed.searchParams.get(key);
        if (!nested) continue;
        try {
          const decoded = decodeURIComponent(nested);
          const candidate = new URL(decoded);
          if (/^https?:$/.test(candidate.protocol)) return candidate.href;
        } catch (_) {}
      }
      return /^https?:$/.test(parsed.protocol) ? parsed.href : "";
    } catch (_) {
      return "";
    }
  }

  function isExternalUrl(raw) {
    const value = unwrapUrl(raw);
    if (!value) return false;
    try {
      const host = new URL(value).hostname.toLowerCase().replace(/^www\./, "");
      return !INTERNAL_HOSTS.some((item) => host === item || host.endsWith("." + item));
    } catch (_) {
      return false;
    }
  }

  function sourceDomain(raw) {
    try {
      return new URL(unwrapUrl(raw)).hostname.toLowerCase().replace(/^www\./, "");
    } catch (_) {
      return "";
    }
  }

  function isPlaceholderTitle(value, fallback) {
    const title = normalizeText(value).replace(/\s+/g, " ").trim();
    if (!title || /^(?:标题未获取|未命名信源|未知信源|网页链接)$/i.test(title)) return true;
    const domain = sourceDomain(fallback);
    const compactTitle = title.toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, "");
    if (domain && (compactTitle === domain || compactTitle === `www.${domain}`)) return true;
    if (/^https?:\/\//i.test(title)) return true;
    return /^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/.*)?$/i.test(title);
  }

  function titleScore(value, fallback) {
    const title = normalizeText(value).replace(/\s+/g, " ").trim();
    if (isPlaceholderTitle(title, fallback)) return 0;
    let score = 100 + Math.min(title.length, 120);
    if (/[\u3400-\u9fff]/.test(title)) score += 20;
    if (/^(?:首页|主页|详情|查看详情|打开链接|来源)$/i.test(title)) score -= 90;
    return score;
  }

  function pickSourceTitle(value, fallback) {
    const lines = normalizeText(value).split(/\n+/).map((line) => line
      .replace(/^\s*(?:\[?\d{1,3}\]?|来源)\s*[.、:：-]?\s*/, "")
      .replace(/\s+/g, " ").trim().slice(0, 500)
    ).filter(Boolean);
    let best = "";
    let bestScore = 0;
    lines.forEach((line, index) => {
      if (line.length < 3 || line.length > 500) return;
      const score = titleScore(line, fallback) + Math.max(0, 20 - index * 2);
      if (score > bestScore) { best = line; bestScore = score; }
    });
    return best;
  }

  function cleanTitle(value, fallback) {
    return pickSourceTitle(value, fallback) || "标题未获取";
  }

  function dedupeSources(items) {
    const found = new Map();
    for (const item of items || []) {
      const raw = typeof item === "string" ? item : item && (item.url || item.href);
      const url = unwrapUrl(raw);
      if (!isExternalUrl(url)) continue;
      let canonical = url;
      try {
        const parsed = new URL(url);
        parsed.hash = "";
        canonical = parsed.href;
      } catch (_) {}
      const next = {
        url: canonical,
        title: cleanTitle(typeof item === "string" ? "" : item.title, canonical),
        domain: sourceDomain(canonical),
        via: (typeof item === "string" ? "unknown" : item.via) || "unknown"
      };
      const old = found.get(canonical);
      const nextScore = titleScore(next.title, canonical);
      const oldScore = old ? titleScore(old.title, canonical) : -1;
      if (!old || nextScore > oldScore || (nextScore === oldScore && next.title.length > old.title.length) ||
          (nextScore === oldScore && old.via === "unknown" && next.via !== "unknown")) {
        found.set(canonical, next);
      }
    }
    return Array.from(found.values());
  }

  function isLikelyCitationSource(item) {
    const url = unwrapUrl(typeof item === "string" ? item : item && (item.url || item.href));
    if (!isExternalUrl(url)) return false;
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
      const path = parsed.pathname.toLowerCase();
      if (host === "s2.zimgs.cn" || host.endsWith(".zimgs.cn")) return false;
      if (host === "w3.org" || host.endsWith(".w3.org")) return false;
      if (host.endsWith(".vipserver") || host.endsWith(".alibaba-inc.com")) return false;
      if (host === "space.bilibili.com") return false;
      if (host === "xiaohongshu.com" || host.endsWith(".xiaohongshu.com") && /\/user\/profile\//.test(path)) return false;
      if (/\/(favicon\.ico|[^/]+\.(?:png|jpe?g|gif|webp|svg|avif))$/i.test(path)) return false;
      if (/user-avatar|avatar\//i.test(url)) return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  function selectCitationSources(items, expectedCount) {
    const clean = dedupeSources(items).filter(isLikelyCitationSource);
    const expected = Math.max(0, Number(expectedCount) || 0);
    return expected ? clean.slice(0, expected) : clean;
  }

  function sourceTitleIsAnswerText(title, answer) {
    const value = compact(String(title || "").trim().replace(/[-–—]+$/, ""));
    return value.length >= 18 && compact(answer).includes(value);
  }

  function sanitizeSourceTitles(items, answer) {
    return (items || []).map((item) => sourceTitleIsAnswerText(item?.title, answer)
      ? { ...item, title: "标题未获取" }
      : item);
  }

  function extractUrlsFromText(value, via) {
    const text = String(value || "");
    const matches = text.match(/https?:\\?\/\\?\/[^\s"'<>\\\]\[\u3000]+/gi) || [];
    return dedupeSources(matches.map((item) => ({
      url: item.replace(/\\\//g, "/").replace(/[),.;，。；]+$/, ""),
      title: "",
      via: via || "network"
    })));
  }

  function buildSchedule(questions, rounds, mode) {
    const clean = Array.from(new Set((questions || []).map(normalizeText).filter(Boolean)));
    const count = Math.max(1, Math.min(Number(rounds) || 1, 10000));
    const output = [];
    if (mode === "sequential") {
      clean.forEach((prompt, questionIndex) => {
        for (let round = 1; round <= count; round += 1) output.push({ prompt, questionIndex, questionRound: round });
      });
    } else {
      for (let round = 1; round <= count; round += 1) {
        clean.forEach((prompt, questionIndex) => output.push({ prompt, questionIndex, questionRound: round }));
      }
    }
    return output.map((item, index) => ({ ...item, globalIndex: index }));
  }

  function sourceSummaryCount(value) {
    const text = normalizeText(value).replace(/\s+/g, " ").trim();
    const patterns = [
      /(?:^|\s)(\d{1,3})\s*(?:个)?网页(?:\s|$)/i,
      /(?:^|\s)(\d{1,3})\s*篇来源(?:\s|$)/i,
      /(?:^|\s)(\d{1,3})\s*web\s*pages?(?:\s|$)/i,
      /(?:^|\s)(\d{1,3})\s*sources?(?:\s|$)/i
    ];
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return Math.max(0, Number(match[1]) || 0);
    }
    return 0;
  }

  function conversationIdFromUrl(value) {
    try {
      const path = new URL(String(value || ""), "https://chat.deepseek.com/").pathname;
      return decodeURIComponent(path.match(/^\/a\/chat\/s\/([^/?#]+)/i)?.[1] || "");
    } catch (_) {
      return "";
    }
  }

  function cleanAnswerText(value, prompt) {
    let text = normalizeText(value);
    const compactPrompt = compact(prompt);
    if (compactPrompt && compact(text).startsWith(compactPrompt)) {
      const position = text.indexOf(prompt);
      if (position >= 0) text = text.slice(position + String(prompt).length).trim();
    }
    text = text
      .replace(/-\s*\n\s*\d{1,3}(?=\s*(?:\n|$))/g, "")
      .replace(/\n(?:复制|赞|踩|分享|重新生成|重试|更多)(?:\s+(?:复制|赞|踩|分享|重新生成|重试|更多))*\s*$/g, "")
      .replace(/\n内容由AI生成[^\n]*$/g, "")
      .trim();

    // DeepSeek's smart-search message wraps the assistant prose in two UI
    // controls: "Found N web pages" above it and "N web pages" below it.
    // The controls live inside the same `.ds-message` node as the answer, so
    // reading `innerText` alone cannot distinguish them from model output.
    // Strip only whole boundary lines; a legitimate sentence mentioning web
    // pages/sources in the middle of an answer is intentionally preserved.
    const sourceShellLine = /^(?:found\s+)?\d{1,3}\s*(?:(?:个)?网页|篇来源|web\s*pages?|sources?)$/i;
    const lines = text.split("\n");
    while (lines.length && !lines[0].trim()) lines.shift();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    while (lines.length && sourceShellLine.test(lines[0].trim())) lines.shift();
    while (lines.length && sourceShellLine.test(lines[lines.length - 1].trim())) lines.pop();

    // Inline citations occasionally render as `sentence-\n知乎` (or another
    // short publisher label).  The trailing hyphen is DeepSeek's citation
    // placeholder, not punctuation authored by the model.  Remove that pair
    // without maintaining a brittle hard-coded publisher list.
    const cleanedLines = [];
    for (const line of lines) {
      const current = line.trim();
      const previous = cleanedLines.length ? cleanedLines[cleanedLines.length - 1] : "";
      const citationLabel = previous.endsWith("-") && current.length > 0 && current.length <= 40
        && !/[，。！？；：,.!?;:]/.test(current);
      if (citationLabel) {
        cleanedLines[cleanedLines.length - 1] = previous.slice(0, -1).trimEnd();
        continue;
      }
      if (/^[，。！？；：,.!?;:]+$/.test(current) && cleanedLines.length) {
        while (cleanedLines.length && !cleanedLines[cleanedLines.length - 1].trim()) cleanedLines.pop();
        if (!cleanedLines.length) continue;
        cleanedLines[cleanedLines.length - 1] = cleanedLines[cleanedLines.length - 1].trimEnd() + current;
        continue;
      }
      cleanedLines.push(line);
    }
    text = normalizeText(cleanedLines.join("\n"));
    const mediaCards = text.search(/\n\d{1,2}:\d{2}\n/);
    if (mediaCards >= 0 && mediaCards > text.length * 0.35) text = text.slice(0, mediaCards).trim();
    text = text.replace(/\n\d{1,3}\s*篇来源\s*$/g, "").trim();
    return text;
  }

  function chooseAnswerCandidate(candidates, prompt) {
    const compactPrompt = compact(prompt);
    let best = null;
    for (const raw of candidates || []) {
      const text = cleanAnswerText(raw.text, prompt);
      if (text.length < 20 || compact(text) === compactPrompt) continue;
      let score = Math.min(text.length, 4000) / 100;
      if (raw.afterPrompt) score += 45;
      if (raw.hasCopyAction) score += 35;
      if (raw.hasCitation) score += 20;
      if (raw.isMessageLike) score += 15;
      if (raw.isRootLike) score -= 80;
      if (raw.containsSidebar) score -= 60;
      if (compactPrompt && compact(raw.text).includes(compactPrompt) && !raw.afterPrompt) score -= 25;
      if (!best || score > best.score || (score === best.score && text.length < best.text.length)) {
        best = { ...raw, rawText: raw.text, text, score };
      }
    }
    return best;
  }

  function detectTransientFailure(value) {
    const text = normalizeText(value);
    const patterns = [
      [/(?:抱歉[，,。\s]*)?系统超时|请求超时|响应超时/, "系统超时"],
      [/服务(?:暂时)?繁忙|系统繁忙|当前访问人数较多|服务器开小差/, "服务繁忙"],
      [/网络(?:连接)?异常|网络错误|连接失败|请求失败.*(?:重试|稍后)/, "网络异常"],
      [/生成失败|回答失败|服务异常.*(?:重试|稍后)/, "生成失败"]
    ];
    for (const [pattern, label] of patterns) if (pattern.test(text)) return label;
    return "";
  }

  function detectBlockingState(value) {
    const text = normalizeText(value);
    const patterns = [
      [/操作(?:过于)?频繁|请求(?:过于)?频繁|访问频繁/, "访问频繁"],
      [/人机验证|安全验证|完成验证|验证码/, "需要安全验证"],
      [/登录后(?:即可|继续)|请先登录|登录已失效|重新登录/, "登录状态失效"],
      [/今日.*(?:次数|额度).*(?:用完|已满|上限)|已达到.*(?:次数|额度|上限)/, "额度已达上限"]
    ];
    for (const [pattern, label] of patterns) if (pattern.test(text)) return label;
    return "";
  }

  function answerTextEquivalent(left, right) {
    const a = normalizeText(left).replace(/\s+/g, " ");
    const b = normalizeText(right).replace(/\s+/g, " ");
    if (!a || !b) return false;
    if (a === b) return true;
    const shorter = Math.min(a.length, b.length);
    const lengthDelta = Math.abs(a.length - b.length);
    if (shorter < 60 || lengthDelta > Math.max(40, shorter * 0.02)) return false;
    let prefix = 0;
    while (prefix < shorter && a.charCodeAt(prefix) === b.charCodeAt(prefix)) prefix += 1;
    return prefix >= shorter * 0.94;
  }

  return {
    compact,
    normalizeText,
    unwrapUrl,
    isExternalUrl,
    sourceDomain,
    isPlaceholderTitle,
    titleScore,
    pickSourceTitle,
    dedupeSources,
    isLikelyCitationSource,
    selectCitationSources,
    sourceTitleIsAnswerText,
    sanitizeSourceTitles,
    sourceSummaryCount,
    extractUrlsFromText,
    buildSchedule,
    conversationIdFromUrl,
    cleanAnswerText,
    chooseAnswerCandidate,
    detectTransientFailure,
    detectBlockingState,
    answerTextEquivalent
  };
});
